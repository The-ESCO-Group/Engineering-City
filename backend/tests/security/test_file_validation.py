"""Upload intake validation.

Intake is the platform's outer boundary: it decides what may be stored and which
parser a file reaches. The rules under test here are the reason a renamed
executable cannot enter a review, and the reason an engineer is told when a file
is not what its name claimed.
"""

from __future__ import annotations

import pytest

from app.core.enums import DocumentRole, FileKind
from app.core.errors import FileRejectedError
from app.security.file_validation import (
    DANGEROUS_EXTENSIONS,
    ValidatedUpload,
    suggest_document_role,
    validate_upload,
)

#: Synthetic file heads. Every byte string here is invented for the test suite.
L5X_HEAD = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<RSLogix5000Content SchemaRevision="1.0" TargetType="Controller">\n'
    b'  <Controller Name="SYNTH_CONV_3"/>\n'
    b"</RSLogix5000Content>\n"
)
XML_HEAD = b'<?xml version="1.0"?><TagExport><Tag Name="SYNTH_001"/></TagExport>'
CSV_HEAD = b"tag,description\nSYNTH_001,Synthetic conveyor run command\n"
TEXT_HEAD = b"Synthetic notes captured during a fixture run.\n"
PDF_HEAD = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
OOXML_HEAD = b"PK\x03\x04\x14\x00\x06\x00\x08\x00" + bytes(24)

DEFAULT_MAX_BYTES = 5_000_000


def _validate(
    filename: str,
    head: bytes,
    *,
    size_bytes: int | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    declared_media_type: str = "",
) -> ValidatedUpload:
    return validate_upload(
        filename=filename,
        head=head,
        size_bytes=len(head) if size_bytes is None else size_bytes,
        max_bytes=max_bytes,
        declared_media_type=declared_media_type,
    )


def _rejection(filename: str, head: bytes, **kwargs: object) -> FileRejectedError:
    with pytest.raises(FileRejectedError) as excinfo:
        _validate(filename, head, **kwargs)  # type: ignore[arg-type]
    return excinfo.value


# ---------------------------------------------------------------------------
# Accepted kinds
# ---------------------------------------------------------------------------


def test_l5x_export_is_accepted_and_routed_to_the_l5x_parser() -> None:
    verdict = _validate("Conveyor_Line_3.l5x", L5X_HEAD)

    assert verdict.file_kind is FileKind.L5X
    assert verdict.extension == ".l5x"
    assert verdict.size_bytes == len(L5X_HEAD)
    assert verdict.warnings == []


def test_plain_xml_is_accepted_as_xml_not_as_control_logic() -> None:
    """Only an RSLogix5000Content root makes XML a Studio 5000 export."""
    verdict = _validate("tag_export.xml", XML_HEAD)

    assert verdict.file_kind is FileKind.XML
    assert verdict.suggested_role is DocumentRole.UNCLASSIFIED
    assert verdict.warnings == []


def test_l5x_content_named_xml_is_recognised_by_its_root_element() -> None:
    """A renamed export must still reach the logic parser, and say it was renamed."""
    verdict = _validate("controller_dump.xml", L5X_HEAD)

    assert verdict.file_kind is FileKind.L5X
    assert verdict.suggested_role is DocumentRole.CONTROL_LOGIC
    assert len(verdict.warnings) == 1
    assert "l5x" in verdict.warnings[0]
    assert "xml" in verdict.warnings[0]


def test_csv_is_accepted_as_tabular_data() -> None:
    verdict = _validate("line3_points.csv", CSV_HEAD)

    assert verdict.file_kind is FileKind.CSV
    assert verdict.warnings == []


def test_plain_text_is_accepted_as_text() -> None:
    verdict = _validate("handover.txt", TEXT_HEAD)

    assert verdict.file_kind is FileKind.TEXT
    assert verdict.warnings == []


def test_pdf_is_accepted_on_its_magic_bytes() -> None:
    verdict = _validate("mechanical_drawing.pdf", PDF_HEAD)

    assert verdict.file_kind is FileKind.PDF
    assert verdict.media_type == "application/pdf"
    assert verdict.warnings == []


@pytest.mark.parametrize(
    ("filename", "expected_kind"),
    [
        ("io_schedule.xlsx", FileKind.XLSX),
        ("io_macros.xlsm", FileKind.XLSX),
        ("handover_letter.docx", FileKind.DOCX),
        ("project_package.zip", FileKind.ZIP),
    ],
)
def test_ooxml_container_kind_is_decided_by_the_extension(
    filename: str, expected_kind: FileKind
) -> None:
    """OOXML files are all ZIPs, so only the extension can pick the parser."""
    verdict = _validate(filename, OOXML_HEAD)

    assert verdict.file_kind is expected_kind
    assert verdict.warnings == []


def test_windows_style_path_in_the_filename_still_yields_the_extension() -> None:
    """Some browsers submit a full local path; the extension must survive it."""
    verdict = _validate(r"C:\Users\eng\Desktop\Conveyor_Line_3.L5X", L5X_HEAD)

    assert verdict.extension == ".l5x"
    assert verdict.file_kind is FileKind.L5X


def test_suggested_role_is_part_of_the_intake_verdict() -> None:
    verdict = _validate("Interlock Schedule Rev B.csv", CSV_HEAD)

    assert verdict.file_kind is FileKind.CSV
    assert verdict.suggested_role is DocumentRole.INTERLOCK_SCHEDULE


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_empty_file_is_rejected() -> None:
    error = _rejection("empty.csv", b"", size_bytes=0)

    assert error.code == "file_rejected"
    assert "empty" in error.message.lower()
    assert error.details["filename"] == "empty.csv"


def test_file_over_the_size_limit_is_rejected_and_told_both_numbers() -> None:
    """The user needs the limit as well as the overage to act on the refusal."""
    error = _rejection("huge.pdf", PDF_HEAD, size_bytes=2048, max_bytes=1024)

    assert "2,048" in error.message
    assert "1,024" in error.message
    assert error.details["size_bytes"] == 2048
    assert error.details["limit"] == 1024


def test_windows_executable_named_as_a_pdf_is_rejected() -> None:
    """The extension is attacker-controlled, so content decides the refusal."""
    error = _rejection("report.pdf", b"MZ\x90\x00\x03\x00\x00\x00This PE is not a report")

    assert "Windows executable (PE)" in error.message
    assert "regardless" in error.message
    assert error.details["detected"] == "Windows executable (PE)"


def test_elf_binary_is_rejected() -> None:
    error = _rejection("toolchain.zip", b"\x7fELF\x02\x01\x01\x00" + bytes(16))

    assert "Linux executable (ELF)" in error.message


def test_shebang_script_is_rejected_even_with_a_harmless_extension() -> None:
    error = _rejection("readme.txt", b"#!/bin/sh\nrm -rf /\n")

    assert "shebang" in error.message


def test_ole_compound_document_is_rejected_by_content() -> None:
    """A legacy binary Office/MSI container renamed to .docx must not be stored."""
    error = _rejection("specification.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(16))

    assert "OLE compound document" in error.message


@pytest.mark.parametrize(
    "extension", [".exe", ".dll", ".msi", ".bat", ".ps1", ".vbs", ".js", ".py", ".sh", ".lnk"]
)
def test_dangerous_extension_is_refused_on_sight(extension: str) -> None:
    error = _rejection(f"payload{extension}", CSV_HEAD)

    assert extension in error.message
    assert error.details["extension"] == extension


def test_every_dangerous_extension_is_refused_even_with_innocent_content() -> None:
    """The blocklist is only worth having if no member has a benign-content escape."""
    for extension in sorted(DANGEROUS_EXTENSIONS):
        error = _rejection(f"innocent{extension}", CSV_HEAD)
        assert error.details["extension"] == extension


def test_unrecognised_extension_with_unrecognisable_content_is_rejected() -> None:
    error = _rejection("controller.dat", b"\x00\x01\x02\x03\x04\x05not a known format")

    assert error.code == "unsupported_content"
    assert "Unrecognised file type '.dat'" in error.message
    assert ".l5x" in error.message


# ---------------------------------------------------------------------------
# Capability-boundary messages (product promises)
# ---------------------------------------------------------------------------


def test_acd_rejection_says_the_format_is_proprietary_and_not_parsed() -> None:
    """Never implying .ACD support is a product promise, so the wording is tested."""
    error = _rejection("Line3_Controller.acd", bytes(32))

    message = error.message.lower()
    assert error.code == "unsupported_content"
    assert "proprietary" in message
    assert "does not parse them" in message
    assert "l5x" in message


@pytest.mark.parametrize(
    ("filename", "modern_format"),
    [("control_narrative.doc", ".docx"), ("io_list.xls", ".xlsx")],
)
def test_legacy_office_rejection_names_the_modern_format(filename: str, modern_format: str) -> None:
    """A refusal is only actionable if it says what to upload instead."""
    error = _rejection(filename, CSV_HEAD)

    assert error.code == "unsupported_content"
    assert modern_format in error.message
    assert "not supported" in error.message


# ---------------------------------------------------------------------------
# Extension/content mismatch
# ---------------------------------------------------------------------------


def test_pdf_content_named_csv_is_accepted_routed_by_content_and_warned_about() -> None:
    """Silently correcting a mislabelled file would hide it from the engineer."""
    verdict = _validate("conveyor_datasheet.csv", PDF_HEAD)

    assert verdict.file_kind is FileKind.PDF
    assert verdict.extension == ".csv"
    assert len(verdict.warnings) == 1
    assert "pdf" in verdict.warnings[0]
    assert "csv" in verdict.warnings[0]


def test_client_reported_media_type_that_contradicts_the_content_is_warned_about() -> None:
    """A browser's guess never overrides the sniffed content, but it is reported."""
    verdict = _validate("drawing.pdf", PDF_HEAD, declared_media_type="text/csv")

    assert verdict.file_kind is FileKind.PDF
    assert any("text/csv" in warning for warning in verdict.warnings)


def test_matching_media_type_produces_no_warning() -> None:
    verdict = _validate("drawing.pdf", PDF_HEAD, declared_media_type="application/pdf")

    assert verdict.warnings == []


# ---------------------------------------------------------------------------
# Role suggestion
# ---------------------------------------------------------------------------


def test_l5x_is_control_logic_whatever_the_filename_says() -> None:
    role = suggest_document_role("misc_attachment_7.l5x", FileKind.L5X)
    assert role is DocumentRole.CONTROL_LOGIC


@pytest.mark.parametrize(
    ("filename", "kind", "expected_role"),
    [
        ("Interlock Schedule Rev B.xlsx", FileKind.XLSX, DocumentRole.INTERLOCK_SCHEDULE),
        ("Line 3 IO List.csv", FileKind.CSV, DocumentRole.IO_LIST),
        ("Functional Description.docx", FileKind.DOCX, DocumentRole.FUNCTIONAL_DESCRIPTION),
        ("Alarm Summary.csv", FileKind.CSV, DocumentRole.ALARM_LIST),
        ("HMI Faceplate Export.zip", FileKind.ZIP, DocumentRole.HMI_EXPORT),
        ("FAT Test Plan.docx", FileKind.DOCX, DocumentRole.TESTING_NOTES),
    ],
)
def test_filename_keywords_suggest_the_matching_document_role(
    filename: str, kind: FileKind, expected_role: DocumentRole
) -> None:
    """Seeds the uploader's dropdown, so the common naming conventions must hit."""
    assert suggest_document_role(filename, kind) is expected_role


def test_unremarkable_filename_is_left_unclassified() -> None:
    """Guessing a role from nothing would put a wrong label in the record."""
    assert suggest_document_role("Cover Sheet.pdf", FileKind.PDF) is DocumentRole.UNCLASSIFIED
