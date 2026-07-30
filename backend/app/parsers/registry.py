"""Parser lookup.

Imports are deferred so that a missing optional dependency (``openpyxl``,
``pypdf``, ``python-docx``) degrades to a clear
:class:`~app.core.errors.CapabilityUnavailableError` for that one file kind
rather than preventing the application from starting.
"""

from __future__ import annotations

import functools
import importlib

from app.core.enums import FileKind
from app.core.errors import CapabilityUnavailableError, UnsupportedContentError
from app.core.logging import get_logger
from app.parsers.base import Parser

logger = get_logger(__name__)

#: The third-party package each kind needs, for a useful error message.
_REQUIRED_PACKAGE: dict[FileKind, str] = {
    FileKind.XLSX: "openpyxl",
    FileKind.DOCX: "python-docx",
    FileKind.PDF: "pypdf",
}

#: ``FileKind -> (module, class)``. Resolved by name at call time rather than by
#: a static import, so a kind whose parser is not present yet reports itself as
#: an unavailable capability instead of breaking the import graph.
_PARSER_CLASSES: dict[FileKind, tuple[str, str]] = {
    FileKind.L5X: ("app.parsers.l5x_parser", "L5XParser"),
    FileKind.XLSX: ("app.parsers.xlsx_parser", "XlsxParser"),
    FileKind.CSV: ("app.parsers.csv_parser", "CsvParser"),
    FileKind.DOCX: ("app.parsers.docx_parser", "DocxParser"),
    FileKind.PDF: ("app.parsers.pdf_parser", "PdfParser"),
    FileKind.XML: ("app.parsers.xml_parser", "XmlParser"),
    FileKind.TEXT: ("app.parsers.text_parser", "TextParser"),
}


@functools.cache
def parser_for(kind: FileKind) -> Parser:
    """Return the parser for a file kind.

    Parsers are stateless, so one cached instance per kind is safe to share
    across worker threads.
    """
    if kind is FileKind.ZIP:
        # Archives are expanded at intake and their members parsed individually;
        # there is nothing to parse about the container itself.
        raise UnsupportedContentError(
            "Archives are expanded during intake; their members are parsed "
            "individually. There is no parser for the container.",
            details={"file_kind": kind.value},
        )

    target = _PARSER_CLASSES.get(kind)
    if target is None:
        raise UnsupportedContentError(
            f"No parser is available for file kind '{kind.value}'.",
            details={"file_kind": kind.value},
        )

    module_name, class_name = target
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or module_name
        if missing == module_name:
            raise CapabilityUnavailableError(
                f"No parser is installed for {kind.value} files ({module_name} is not present).",
                details={"file_kind": kind.value, "module": module_name},
            ) from exc
        package = _REQUIRED_PACKAGE.get(kind, missing)
        raise CapabilityUnavailableError(
            f"Cannot parse {kind.value} files: {package} is not installed. "
            f"Install it with: pip install {package}",
            details={"file_kind": kind.value, "package": package, "error": str(exc)},
        ) from exc

    try:
        parser_cls = getattr(module, class_name)
    except AttributeError as exc:
        raise CapabilityUnavailableError(
            f"{module_name} does not define {class_name}.",
            details={"file_kind": kind.value, "module": module_name},
        ) from exc

    parser: Parser = parser_cls()
    return parser


def supported_kinds() -> list[FileKind]:
    """File kinds for which a parser can actually be loaded right now."""
    available: list[FileKind] = []
    for kind in _PARSER_CLASSES:
        try:
            parser_for(kind)
        except (CapabilityUnavailableError, UnsupportedContentError):
            continue
        available.append(kind)
    return available


def parser_capabilities() -> list[dict[str, object]]:
    """Per-kind availability, for ``/api/system/capabilities``."""
    rows: list[dict[str, object]] = []
    for kind in _PARSER_CLASSES:
        try:
            parser = parser_for(kind)
        except (CapabilityUnavailableError, UnsupportedContentError) as exc:
            rows.append(
                {
                    "name": f"parser:{kind.value}",
                    "available": False,
                    "provider": "",
                    "version": "",
                    "detail": exc.message,
                }
            )
            continue
        rows.append(
            {
                "name": f"parser:{kind.value}",
                "available": True,
                "provider": parser.name,
                "version": parser.version,
                "detail": "",
            }
        )
    return rows
