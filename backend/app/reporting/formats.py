"""Deterministic, local-only report export formats.

Every serializer consumes the same format-neutral :class:`ReportDocument`.
OOXML archives are rewritten with stable member ordering and timestamps so the
same captured project snapshot produces identical bytes and hashes.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import textwrap
import unicodedata
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime

from docx import Document
from docx.document import Document as DocxDocument
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.core.clock import isoformat_z
from app.reporting.types import ReportDocument, ReportSection, ReportTable

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_NAVY = "0A3442"
_CYAN = "16B8D4"
_PALE_CYAN = "E8F7FA"
_PALE_GRAY = "F2F4F7"
_INK = "15252C"
_MUTED = "5A6B72"
_THIN_GRAY = "C7D3D7"


def _format_metadata(
    document: ReportDocument, format_key: str, version: str
) -> tuple[tuple[str, str], ...]:
    return (
        *document.metadata,
        ("Document status", document.document_status),
        ("Artifact validation level", document.validation_level),
        ("Generated at (UTC)", isoformat_z(document.generated_at)),
        ("Source snapshot SHA-256", document.source_digest or "Not available"),
        ("Report generator", f"{document.generator_name} {document.generator_version}"),
        ("Export generator", f"{format_key} {version}"),
    )


def _rows_or_message(table: ReportTable) -> tuple[tuple[str, ...], ...]:
    if table.rows:
        return table.rows
    return ((table.empty_message, *("" for _ in table.columns[1:])),)


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def _markdown_table(table: ReportTable) -> list[str]:
    header = "| " + " | ".join(_markdown_cell(value) for value in table.columns) + " |"
    divider = "| " + " | ".join("---" for _ in table.columns) + " |"
    rows = [
        "| " + " | ".join(_markdown_cell(value) for value in row) + " |"
        for row in _rows_or_message(table)
    ]
    return [f"### {table.heading}", "", header, divider, *rows]


class MarkdownExportFormat:
    """Human-readable legacy format retained for existing review workflows."""

    format_key = "markdown"
    media_type = "text/markdown; charset=utf-8"
    file_extension = ".md"
    version = "2.0.0"

    def serialize(self, document: ReportDocument) -> bytes:
        lines = [
            f"# {document.title}",
            "",
            document.subtitle,
            "",
            f"> **{document.document_status}**",
            "",
            f"> {document.notice}",
            "",
            "## Artifact metadata",
            "",
        ]
        metadata = ReportTable(
            "Metadata",
            ("Field", "Value"),
            _format_metadata(document, self.format_key, self.version),
        )
        lines.extend(_markdown_table(metadata))
        for section in document.sections:
            lines.extend(["", f"## {section.heading}"])
            for paragraph in section.paragraphs:
                lines.extend(["", paragraph])
            for table in section.tables:
                lines.extend(["", *_markdown_table(table)])
        if document.disclaimers:
            lines.extend(["", "## Disclaimers"])
            for disclaimer in document.disclaimers:
                lines.extend(["", f"- {disclaimer}"])
        lines.append("")
        return "\n".join(lines).encode("utf-8")


class HtmlExportFormat:
    format_key = "html"
    media_type = "text/html; charset=utf-8"
    file_extension = ".html"
    version = "2.0.0"

    def serialize(self, document: ReportDocument) -> bytes:
        sections = "".join(self._section(section) for section in document.sections)
        metadata = self._table(
            ReportTable(
                "Artifact metadata",
                ("Field", "Value"),
                _format_metadata(document, self.format_key, self.version),
            )
        )
        disclaimers = "".join(f"<li>{html.escape(item)}</li>" for item in document.disclaimers)
        page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(document.title)}</title>
<style>
:root{{--navy:#0a3442;--cyan:#16b8d4;--ink:#15252c;--muted:#5a6b72;--line:#c7d3d7}}
*{{box-sizing:border-box}}
body{{font:14px/1.5 Arial,sans-serif;color:var(--ink);max-width:1280px;
margin:0 auto;padding:36px 42px;background:#fff}}
header{{border-bottom:2px solid var(--cyan);padding-bottom:18px}}
.kicker{{color:var(--cyan);font-size:12px;font-weight:700;letter-spacing:.12em;
text-transform:uppercase}}
h1{{font-size:30px;line-height:1.15;margin:5px 0;color:var(--navy)}}
h2{{font-size:21px;margin:30px 0 10px;color:var(--navy)}}
h3{{font-size:16px;color:var(--navy)}}
.subtitle,.muted{{color:var(--muted)}}
.status{{display:inline-block;background:#edf2f4;border:1px solid #9fb0b6;color:#263a42;
font-weight:800;padding:7px 10px;margin:12px 0}}
.notice{{background:#e8f7fa;border-left:4px solid var(--cyan);padding:12px 14px}}
section{{break-inside:avoid-page}}
.table-wrap{{overflow:auto;margin:10px 0 26px}}
table{{width:100%;border-collapse:collapse}}
caption{{text-align:left;font-weight:700;color:var(--navy);padding:6px 0}}
th,td{{border:1px solid var(--line);padding:7px;text-align:left;vertical-align:top;
overflow-wrap:anywhere}}
th{{background:#edf3f5}}
tr:nth-child(even) td{{background:#f8fafb}}
.disclaimers{{margin-top:28px;padding:14px;background:#f2f4f7}}
footer{{margin-top:32px;padding-top:12px;border-top:1px solid var(--line);
color:var(--muted)}}
@media print{{body{{max-width:none;padding:12mm}}.table-wrap{{overflow:visible}}
thead{{display:table-header-group}}}}
</style></head><body><header><div class="kicker">ESCO Engineering City</div>
<h1>{html.escape(document.title)}</h1><p class="subtitle">{html.escape(document.subtitle)}</p>
<div class="status">{html.escape(document.document_status)}</div>
<p class="notice">{html.escape(document.notice)}</p></header>{metadata}{sections}
<section class="disclaimers"><h2>Disclaimers</h2><ul>{disclaimers}</ul></section>
<footer>Source snapshot: {html.escape(document.source_digest or "not available")} |
Generated locally</footer>
</body></html>"""
        return page.encode("utf-8")

    def _section(self, section: ReportSection) -> str:
        body = "".join(f"<p>{html.escape(item)}</p>" for item in section.paragraphs)
        tables = "".join(self._table(table) for table in section.tables)
        return f"<section><h2>{html.escape(section.heading)}</h2>{body}{tables}</section>"

    @staticmethod
    def _table(table: ReportTable) -> str:
        headings = "".join(f'<th scope="col">{html.escape(value)}</th>' for value in table.columns)
        rows = "".join(
            "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in row) + "</tr>"
            for row in _rows_or_message(table)
        )
        return (
            f'<div class="table-wrap"><table><caption>{html.escape(table.heading)}</caption>'
            f"<thead><tr>{headings}</tr></thead><tbody>{rows}</tbody></table></div>"
        )


def _document_payload(document: ReportDocument, format_key: str, version: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "report": {
            "type": document.report_type,
            "title": document.title,
            "subtitle": document.subtitle,
            "version": document.report_version,
            "document_status": document.document_status,
            "validation_level": document.validation_level,
            "generated_at": isoformat_z(document.generated_at),
            "source_snapshot_sha256": document.source_digest,
            "notice": document.notice,
        },
        "generator": {
            "name": document.generator_name,
            "version": document.generator_version,
            "export_format": format_key,
            "export_format_version": version,
        },
        "metadata": [{"field": key, "value": value} for key, value in document.metadata],
        "sections": [
            {
                "heading": section.heading,
                "paragraphs": list(section.paragraphs),
                "tables": [
                    {
                        "heading": table.heading,
                        "columns": list(table.columns),
                        "rows": [list(row) for row in table.rows],
                        "empty_message": table.empty_message,
                    }
                    for table in section.tables
                ],
            }
            for section in document.sections
        ],
        "disclaimers": list(document.disclaimers),
    }


class JsonExportFormat:
    format_key = "json"
    media_type = "application/json"
    file_extension = ".json"
    version = "1.0.0"

    def serialize(self, document: ReportDocument) -> bytes:
        payload = _document_payload(document, self.format_key, self.version)
        return (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")


def _safe_formula_text(value: str) -> str:
    if value and value[0] in "=+-@":
        return "'" + value
    return value


class CsvExportFormat:
    """Lossless long-form CSV for reports that contain more than one table."""

    format_key = "csv"
    media_type = "text/csv; charset=utf-8"
    file_extension = ".csv"
    version = "1.0.0"

    def serialize(self, document: ReportDocument) -> bytes:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("record_type", "section", "table", "field", "value", "values_json"))
        for key, value in _format_metadata(document, self.format_key, self.version):
            writer.writerow(
                ("metadata", "Artifact metadata", "", key, _safe_formula_text(value), "")
            )
        writer.writerow(("notice", "Artifact metadata", "", "Notice", document.notice, ""))
        for section in document.sections:
            for paragraph in section.paragraphs:
                writer.writerow(
                    ("paragraph", section.heading, "", "", _safe_formula_text(paragraph), "")
                )
            for table in section.tables:
                writer.writerow(
                    (
                        "table_columns",
                        section.heading,
                        table.heading,
                        "",
                        "",
                        json.dumps(table.columns, ensure_ascii=False),
                    )
                )
                for row in _rows_or_message(table):
                    safe_row = tuple(_safe_formula_text(value) for value in row)
                    writer.writerow(
                        (
                            "table_row",
                            section.heading,
                            table.heading,
                            "",
                            "",
                            json.dumps(safe_row, ensure_ascii=False),
                        )
                    )
        for disclaimer in document.disclaimers:
            writer.writerow(("disclaimer", "Disclaimers", "", "", disclaimer, ""))
        return ("\ufeff" + stream.getvalue()).encode("utf-8")


def _canonical_zip(data: bytes, *, core_time: datetime | None = None) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data), "r")
    output = io.BytesIO()
    with (
        source,
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target,
    ):
        for name in sorted(source.namelist()):
            original = source.getinfo(name)
            info = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = original.external_attr
            info.create_system = 0
            payload = source.read(name)
            if core_time is not None and name == "docProps/core.xml":
                timestamp = core_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ").encode()
                for field in (b"created", b"modified"):
                    pattern = (
                        rb"(<dcterms:"
                        + field
                        + rb"\b[^>]*>)[^<]*(</dcterms:"
                        + field
                        + rb">)"
                    )
                    payload = re.sub(pattern, rb"\g<1>" + timestamp + rb"\g<2>", payload)
            target.writestr(info, payload)
    return output.getvalue()


def _set_repeat_table_header(row: object) -> None:
    tr_pr = row._tr.get_or_add_trPr()  # type: ignore[attr-defined]
    marker = OxmlElement("w:tblHeader")
    marker.set(qn("w:val"), "true")
    tr_pr.append(marker)


def _set_cell_fill(cell: object, color: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()  # type: ignore[attr-defined]
    shading = tc_pr.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        tc_pr.append(shading)
    shading.set(qn("w:fill"), color)


def _set_cell_margins(
    cell: object, *, top: int = 80, start: int = 120, bottom: int = 80, end: int = 120
) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()  # type: ignore[attr-defined]
    margins = tc_pr.first_child_found_in("w:tcMar")
    if margins is None:
        margins = OxmlElement("w:tcMar")
        tc_pr.append(margins)
    for edge, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = margins.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            margins.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def _set_table_geometry(table: object, widths_dxa: tuple[int, ...]) -> None:
    table.autofit = False  # type: ignore[attr-defined]
    tbl_pr = table._tbl.tblPr  # type: ignore[attr-defined]
    layout = tbl_pr.first_child_found_in("w:tblLayout")
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")
    total = sum(widths_dxa)
    width = tbl_pr.first_child_found_in("w:tblW")
    width.set(qn("w:w"), str(total))
    width.set(qn("w:type"), "dxa")
    indent = tbl_pr.first_child_found_in("w:tblInd")
    if indent is None:
        indent = OxmlElement("w:tblInd")
        tbl_pr.append(indent)
    indent.set(qn("w:w"), "120")
    indent.set(qn("w:type"), "dxa")
    grid = table._tbl.tblGrid  # type: ignore[attr-defined]
    for child in list(grid):
        grid.remove(child)
    for value in widths_dxa:
        col = OxmlElement("w:gridCol")
        col.set(qn("w:w"), str(value))
        grid.append(col)
    for row in table.rows:  # type: ignore[attr-defined]
        for index, cell in enumerate(row.cells):
            tc_pr = cell._tc.get_or_add_tcPr()
            tc_width = tc_pr.first_child_found_in("w:tcW")
            tc_width.set(qn("w:w"), str(widths_dxa[index]))
            tc_width.set(qn("w:type"), "dxa")
            _set_cell_margins(cell)


def _column_widths(table: ReportTable, total_dxa: int) -> tuple[int, ...]:
    sample = (table.columns, *_rows_or_message(table)[:30])
    weights = []
    for index in range(len(table.columns)):
        longest = max(len(row[index]) if index < len(row) else 0 for row in sample)
        weights.append(max(6, min(longest, 42)))
    total_weight = sum(weights) or 1
    raw = [max(500, round(total_dxa * value / total_weight)) for value in weights]
    difference = total_dxa - sum(raw)
    raw[-1] += difference
    if raw[-1] < 500:
        need = 500 - raw[-1]
        raw[-1] = 500
        largest = max(range(len(raw) - 1), key=raw.__getitem__)
        raw[largest] -= need
    return tuple(raw)


def _set_docx_run(
    run: object, *, size: float = 10.0, color: str = _INK, bold: bool = False, italic: bool = False
) -> None:
    run.font.name = "Calibri"  # type: ignore[attr-defined]
    run._element.get_or_add_rPr().get_or_add_rFonts().set(  # type: ignore[attr-defined]
        qn("w:ascii"), "Calibri"
    )
    run._element.get_or_add_rPr().get_or_add_rFonts().set(  # type: ignore[attr-defined]
        qn("w:hAnsi"), "Calibri"
    )
    run.font.size = Pt(size)  # type: ignore[attr-defined]
    run.font.color.rgb = RGBColor.from_string(color)  # type: ignore[attr-defined]
    run.bold = bold  # type: ignore[attr-defined]
    run.italic = italic  # type: ignore[attr-defined]


class DocxExportFormat:
    format_key = "docx"
    media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    file_extension = ".docx"
    version = "1.0.0"

    def serialize(self, document: ReportDocument) -> bytes:
        doc = Document()
        max_columns = max(
            (len(table.columns) for section in document.sections for table in section.tables),
            default=2,
        )
        landscape = max_columns > 5
        section = doc.sections[0]
        if landscape:
            section.orientation = WD_ORIENT.LANDSCAPE
            section.page_width = Inches(11)
            section.page_height = Inches(8.5)
            margin = Inches(0.5)
            usable_dxa = 14_400
        else:
            section.page_width = Inches(8.5)
            section.page_height = Inches(11)
            margin = Inches(1)
            usable_dxa = 9_360
        section.top_margin = margin
        section.bottom_margin = margin
        section.left_margin = margin
        section.right_margin = margin
        section.header_distance = Inches(0.35)
        section.footer_distance = Inches(0.35)

        normal = doc.styles["Normal"]
        normal.font.name = "Calibri"
        normal.font.size = Pt(11)
        normal.paragraph_format.space_after = Pt(6)
        normal.paragraph_format.line_spacing = 1.1
        for style_name, size, color, before, after in (
            ("Title", 23, _NAVY, 0, 5),
            ("Subtitle", 11, _MUTED, 0, 12),
            ("Heading 1", 16, _NAVY, 16, 8),
            ("Heading 2", 13, _NAVY, 12, 6),
        ):
            style = doc.styles[style_name]
            style.font.name = "Calibri"
            style.font.size = Pt(size)
            style.font.color.rgb = RGBColor.from_string(color)
            style.font.bold = style_name != "Subtitle"
            style.paragraph_format.space_before = Pt(before)
            style.paragraph_format.space_after = Pt(after)

        header = section.header.paragraphs[0]
        header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _set_docx_run(
            header.add_run("ESCO Engineering City | Generated report"), size=8, color=_MUTED
        )
        footer = section.footer.paragraphs[0]
        footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _set_docx_run(footer.add_run("Page "), size=8, color=_MUTED)
        page_field = OxmlElement("w:fldSimple")
        page_field.set(qn("w:instr"), "PAGE")
        footer._p.append(page_field)

        doc.add_paragraph(document.title, style="Title")
        doc.add_paragraph(document.subtitle, style="Subtitle")
        status_paragraph = doc.add_paragraph()
        status_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        status_paragraph.paragraph_format.space_before = Pt(4)
        status_paragraph.paragraph_format.space_after = Pt(8)
        status_shading = OxmlElement("w:shd")
        status_shading.set(qn("w:fill"), _PALE_GRAY)
        status_paragraph._p.get_or_add_pPr().append(status_shading)
        _set_docx_run(status_paragraph.add_run(document.document_status), size=10, bold=True)
        notice = doc.add_paragraph()
        notice.paragraph_format.space_before = Pt(8)
        notice.paragraph_format.space_after = Pt(10)
        _set_docx_run(notice.add_run(document.notice), size=10, color=_NAVY, bold=True)

        doc.add_heading("Artifact metadata", level=1)
        metadata = ReportTable(
            "Artifact metadata",
            ("Field", "Value"),
            _format_metadata(document, self.format_key, self.version),
        )
        self._add_table(doc, metadata, usable_dxa)
        for item in document.sections:
            doc.add_heading(item.heading, level=1)
            for paragraph in item.paragraphs:
                doc.add_paragraph(paragraph)
            for table in item.tables:
                doc.add_heading(table.heading, level=2)
                self._add_table(doc, table, usable_dxa)
        if document.disclaimers:
            doc.add_heading("Disclaimers", level=1)
            for disclaimer in document.disclaimers:
                doc.add_paragraph(disclaimer, style="List Bullet")

        fixed_time = document.generated_at.astimezone(UTC).replace(tzinfo=None)
        doc.core_properties.title = document.title
        doc.core_properties.subject = document.report_type
        doc.core_properties.author = document.generator_name
        doc.core_properties.category = document.document_status
        doc.core_properties.comments = f"Source snapshot SHA-256: {document.source_digest}"
        doc.core_properties.created = fixed_time
        doc.core_properties.modified = fixed_time
        stream = io.BytesIO()
        doc.save(stream)
        return _canonical_zip(stream.getvalue(), core_time=document.generated_at)

    @staticmethod
    def _add_table(doc: DocxDocument, source: ReportTable, usable_dxa: int) -> None:
        rows = _rows_or_message(source)
        table = doc.add_table(rows=1, cols=len(source.columns))
        table.style = "Table Grid"
        _set_repeat_table_header(table.rows[0])
        for index, value in enumerate(source.columns):
            cell = table.rows[0].cells[index]
            _set_cell_fill(cell, _PALE_GRAY)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            paragraph = cell.paragraphs[0]
            _set_docx_run(paragraph.add_run(value), size=8.5, color=_NAVY, bold=True)
        for row in rows:
            cells = table.add_row().cells
            for index, value in enumerate(row):
                cells[index].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                paragraph = cells[index].paragraphs[0]
                _set_docx_run(paragraph.add_run(value), size=8.5)
        _set_table_geometry(table, _column_widths(source, usable_dxa))
        doc.add_paragraph().paragraph_format.space_after = Pt(2)


def _sheet_name(value: str, used: set[str]) -> str:
    clean = re.sub(r"[\\/*?:\[\]]", " ", value).strip() or "Report"
    clean = re.sub(r"\s+", " ", clean)[:31]
    candidate = clean
    suffix = 2
    while candidate.casefold() in used:
        tail = f" {suffix}"
        candidate = clean[: 31 - len(tail)] + tail
        suffix += 1
    used.add(candidate.casefold())
    return candidate


class XlsxExportFormat:
    format_key = "xlsx"
    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    file_extension = ".xlsx"
    version = "1.0.0"

    def serialize(self, document: ReportDocument) -> bytes:
        workbook = Workbook()
        overview = workbook.active
        overview.title = "Report Overview"
        used = {overview.title.casefold()}
        workbook.properties.creator = document.generator_name
        workbook.properties.title = document.title
        workbook.properties.subject = document.report_type
        workbook.properties.description = (
            f"{document.document_status}; source snapshot SHA-256 {document.source_digest}"
        )
        workbook.properties.category = document.document_status
        fixed_time = document.generated_at.astimezone(UTC).replace(tzinfo=None)
        workbook.properties.created = fixed_time
        workbook.properties.modified = fixed_time

        self._style_sheet(overview)
        overview.merge_cells("A1:F1")
        overview["A1"] = _safe_formula_text(document.title)
        overview["A1"].font = Font(name="Arial", size=20, bold=True, color=_NAVY)
        overview["A2"] = "Document status"
        overview["B2"] = document.document_status
        overview["B2"].font = Font(name="Arial", bold=True, color=_INK)
        overview["B2"].fill = PatternFill("solid", fgColor=_PALE_GRAY)
        overview["A3"] = "Notice"
        overview["B3"] = _safe_formula_text(document.notice)
        overview.merge_cells("B3:F3")
        overview["B3"].alignment = Alignment(wrap_text=True, vertical="top")
        row = 5
        overview.cell(row, 1, "Field")
        overview.cell(row, 2, "Value")
        self._style_header(overview, row, 2)
        for key, value in _format_metadata(document, self.format_key, self.version):
            row += 1
            overview.cell(row, 1, key)
            overview.cell(row, 2, _safe_formula_text(value))
        row += 2
        overview.cell(row, 1, "Section")
        overview.cell(row, 2, "Narrative")
        self._style_header(overview, row, 2)
        for section in document.sections:
            for paragraph in section.paragraphs or ("",):
                row += 1
                overview.cell(row, 1, section.heading)
                overview.cell(row, 2, _safe_formula_text(paragraph))
        row += 2
        overview.cell(row, 1, "Disclaimers")
        overview.cell(row, 1).font = Font(name="Arial", bold=True, color=_NAVY)
        for disclaimer in document.disclaimers:
            row += 1
            overview.cell(row, 1, _safe_formula_text(disclaimer))
            overview.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
        overview.column_dimensions["A"].width = 31
        overview.column_dimensions["B"].width = 76
        for column in range(3, 7):
            overview.column_dimensions[get_column_letter(column)].width = 12
        overview.freeze_panes = "A6"

        for section in document.sections:
            for table in section.tables:
                sheet = workbook.create_sheet(_sheet_name(table.heading, used))
                self._style_sheet(sheet)
                sheet["A1"] = _safe_formula_text(table.heading)
                sheet["A1"].font = Font(name="Arial", size=16, bold=True, color=_NAVY)
                sheet["A2"] = document.document_status
                sheet["A2"].font = Font(name="Arial", bold=True, color=_INK)
                sheet["A3"] = (
                    f"Source snapshot SHA-256: {document.source_digest or 'Not available'}"
                )
                header_row = 5
                for index, value in enumerate(table.columns, 1):
                    sheet.cell(header_row, index, _safe_formula_text(value))
                self._style_header(sheet, header_row, len(table.columns))
                for row_index, values in enumerate(_rows_or_message(table), header_row + 1):
                    for column_index, value in enumerate(values, 1):
                        cell = sheet.cell(row_index, column_index, _safe_formula_text(value))
                        cell.alignment = Alignment(vertical="top", wrap_text=True)
                        cell.font = Font(name="Arial", size=9, color=_INK)
                end_row = header_row + len(_rows_or_message(table))
                end_col = len(table.columns)
                sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(end_col)}{end_row}"
                sheet.freeze_panes = f"A{header_row + 1}"
                for column_index, heading in enumerate(table.columns, 1):
                    column_values = [heading] + [
                        row[column_index - 1] for row in _rows_or_message(table)
                    ]
                    width = min(max(max(len(item) for item in column_values) + 2, 11), 48)
                    sheet.column_dimensions[get_column_letter(column_index)].width = width
                sheet.sheet_properties.pageSetUpPr.fitToPage = True
                sheet.page_setup.fitToWidth = 1
                sheet.page_setup.fitToHeight = 0
                sheet.print_title_rows = f"{header_row}:{header_row}"

        stream = io.BytesIO()
        workbook.save(stream)
        return _canonical_zip(stream.getvalue(), core_time=document.generated_at)

    @staticmethod
    def _style_sheet(sheet: Worksheet) -> None:
        sheet.sheet_view.showGridLines = False
        sheet.sheet_properties.outlinePr.summaryBelow = True
        sheet.sheet_format.defaultRowHeight = 18

    @staticmethod
    def _style_header(sheet: Worksheet, row: int, columns: int) -> None:
        side = Side(style="thin", color=_THIN_GRAY)
        for column in range(1, columns + 1):
            cell = sheet.cell(row, column)
            cell.font = Font(name="Arial", size=9, bold=True, color=_NAVY)
            cell.fill = PatternFill("solid", fgColor=_PALE_CYAN)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cell.border = Border(bottom=side)


def _ascii(value: str) -> str:
    replacements = {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    return unicodedata.normalize("NFKD", value).encode("ascii", "replace").decode("ascii")


def _pdf_escape(value: str) -> str:
    return _ascii(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrapped(value: str, width: int) -> Iterable[str]:
    paragraphs = value.replace("\r", "").split("\n") or [""]
    for paragraph in paragraphs:
        yield from (
            textwrap.wrap(
                _ascii(paragraph),
                width=width,
                replace_whitespace=True,
                drop_whitespace=True,
                break_long_words=True,
                break_on_hyphens=True,
            )
            or [""]
        )


def _pdf_logical_lines(
    document: ReportDocument, format_version: str
) -> list[tuple[str, float, bool]]:
    lines: list[tuple[str, float, bool]] = []

    def add(text: str, size: float = 8.5, bold: bool = False, width: int = 112) -> None:
        for item in _wrapped(text, width):
            lines.append((item, size, bold))

    add("ESCO ENGINEERING CITY", 8.5, True)
    add(document.title, 17, True, 70)
    add(document.subtitle, 9, False)
    add(document.document_status, 10, True)
    add(document.notice, 9, False, 100)
    lines.append(("", 6, False))
    add("Artifact metadata", 13, True)
    for key, value in _format_metadata(document, "pdf", format_version):
        add(f"{key}: {value}", 8, key in {"Document status", "Artifact validation level"})
    for section in document.sections:
        lines.append(("", 6, False))
        add(section.heading, 13, True)
        for paragraph in section.paragraphs:
            add(paragraph, 8.5, False, 110)
        for table in section.tables:
            add(table.heading, 10, True)
            add(" | ".join(table.columns), 7.3, True, 126)
            for row in _rows_or_message(table):
                add(" | ".join(row), 7.1, False, 126)
    if document.disclaimers:
        lines.append(("", 6, False))
        add("Disclaimers", 13, True)
        for item in document.disclaimers:
            add(f"- {item}", 8.5, False, 110)
    return lines


def _build_pdf(document: ReportDocument, version: str) -> bytes:
    logical = _pdf_logical_lines(document, version)
    pages: list[list[tuple[str, float, bool]]] = []
    current: list[tuple[str, float, bool]] = []
    used = 0.0
    for line in logical:
        height = max(10.0, line[1] + 3.0)
        keep_with_next = 70.0 if line[2] and line[1] >= 12 else 35.0 if line[2] else 0.0
        if current and used + height + keep_with_next > 680:
            pages.append(current)
            current = []
            used = 0.0
        current.append(line)
        used += height
    pages.append(current or [("No report content.", 9, False)])

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        5: (
            f"<< /Title ({_pdf_escape(document.title)}) "
            f"/Author ({_pdf_escape(document.generator_name)}) "
            f"/Subject ({_pdf_escape(document.document_status)}) >>"
        ).encode("ascii"),
    }
    kids: list[str] = []
    for index, page_lines in enumerate(pages):
        page_obj = 6 + index * 2
        content_obj = page_obj + 1
        kids.append(f"{page_obj} 0 R")
        commands: list[str] = []
        y = 748.0
        for text, size, bold in page_lines:
            font = "F2" if bold else "F1"
            color = "0.04 0.20 0.26" if bold else "0.08 0.15 0.17"
            commands.append(
                f"BT /{font} {size:.1f} Tf {color} rg 50 {y:.1f} Td ({_pdf_escape(text)}) Tj ET"
            )
            y -= max(10.0, size + 3.0)
        footer = f"Page {index + 1} of {len(pages)} | {document.document_status}"
        commands.append(f"BT /F1 7 Tf 0.35 0.42 0.45 rg 50 28 Td ({_pdf_escape(footer)}) Tj ET")
        stream = "\n".join(commands).encode("ascii")
        objects[content_obj] = (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        objects[page_obj] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_obj} 0 R >>"
        ).encode("ascii")
    objects[2] = f"<< /Type /Pages /Count {len(pages)} /Kids [{' '.join(kids)}] >>".encode("ascii")

    max_object = max(objects)
    result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (max_object + 1)
    for object_id in range(1, max_object + 1):
        offsets[object_id] = len(result)
        result.extend(f"{object_id} 0 obj\n".encode("ascii"))
        result.extend(objects[object_id])
        result.extend(b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {max_object + 1}\n".encode("ascii"))
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    result.extend(
        (
            f"trailer\n<< /Size {max_object + 1} /Root 1 0 R /Info 5 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(result)


class PdfExportFormat:
    format_key = "pdf"
    media_type = "application/pdf"
    file_extension = ".pdf"
    version = "1.0.0"

    def serialize(self, document: ReportDocument) -> bytes:
        return _build_pdf(document, self.version)


EXPORT_FORMATS = (
    HtmlExportFormat(),
    PdfExportFormat(),
    DocxExportFormat(),
    XlsxExportFormat(),
    CsvExportFormat(),
    JsonExportFormat(),
    MarkdownExportFormat(),
)

REQUIRED_EXPORT_FORMATS = ("html", "pdf", "docx", "xlsx", "csv", "json")


def export_format_for(
    key: str,
) -> (
    MarkdownExportFormat
    | HtmlExportFormat
    | PdfExportFormat
    | DocxExportFormat
    | XlsxExportFormat
    | CsvExportFormat
    | JsonExportFormat
):
    for provider in EXPORT_FORMATS:
        if provider.format_key == key:
            return provider
    available = ", ".join(item.format_key for item in EXPORT_FORMATS)
    raise ValueError(f"unsupported export format: {key!r}; available: {available}")
