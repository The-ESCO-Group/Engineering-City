"""Upload intake validation.

Intake decides what the platform is willing to store and hand to a parser. It is
allowlist-based: a file is accepted because its type is recognised and supported,
never merely because it is not on a blocklist.

Content is sniffed as well as extension-checked, because the extension is
attacker-controlled and because a mislabelled file would otherwise be routed to
the wrong parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.core.enums import DocumentRole, FileKind
from app.core.errors import FileRejectedError

#: Extension -> the kind of parser that should handle it.
EXTENSION_KINDS: dict[str, FileKind] = {
    ".l5x": FileKind.L5X,
    ".xlsx": FileKind.XLSX,
    ".xlsm": FileKind.XLSX,
    ".csv": FileKind.CSV,
    ".tsv": FileKind.CSV,
    ".docx": FileKind.DOCX,
    ".pdf": FileKind.PDF,
    ".xml": FileKind.XML,
    ".zip": FileKind.ZIP,
    ".txt": FileKind.TEXT,
    ".md": FileKind.TEXT,
    ".log": FileKind.TEXT,
}

#: Media types accepted per kind, used only as a weak hint. The sniffed content
#: wins; a browser's guess never does.
MEDIA_TYPE_HINTS: dict[FileKind, tuple[str, ...]] = {
    FileKind.XLSX: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroEnabled.12",
    ),
    FileKind.DOCX: ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
    FileKind.PDF: ("application/pdf",),
    FileKind.ZIP: ("application/zip", "application/x-zip-compressed"),
    FileKind.CSV: ("text/csv", "text/tab-separated-values"),
    FileKind.XML: ("application/xml", "text/xml"),
    FileKind.L5X: ("application/xml", "text/xml"),
    FileKind.TEXT: ("text/plain", "text/markdown"),
}

#: Extensions that are explicitly, permanently unsupported, each with the reason
#: shown to the user. Being explicit beats a silent "unknown type" because these
#: are the files an engineer will actually try to upload.
EXPLICITLY_UNSUPPORTED: dict[str, str] = {
    ".acd": (
        "Studio 5000 .ACD files are a proprietary binary format. This platform "
        "does not parse them and does not claim to. Export the project to L5X "
        "and upload that instead."
    ),
    ".doc": "Legacy .doc is not supported. Save as .docx and upload again.",
    ".xls": "Legacy .xls is not supported. Save as .xlsx and upload again.",
    ".ppt": "PowerPoint files are not analysed by this platform.",
    ".pptx": "PowerPoint files are not analysed by this platform.",
    ".rsp": "RSLogix project files are proprietary and are not parsed. Export to L5X.",
    ".apa": "FactoryTalk archives are not parsed. Upload the exported files instead.",
}

#: Magic-byte prefixes for content that must never be stored, whatever it claims
#: to be. Executables and installers have no place in an engineering review.
EXECUTABLE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "Windows executable (PE)"),
    (b"\x7fELF", "Linux executable (ELF)"),
    (b"\xca\xfe\xba\xbe", "Mach-O / Java class"),
    (b"\xcf\xfa\xed\xfe", "Mach-O executable"),
    (b"\xfe\xed\xfa\xce", "Mach-O executable"),
    (b"#!", "script with a shebang"),
    (b"\xd0\xcf\x11\xe0", "legacy OLE compound document (.doc/.xls/.msi)"),
)

#: Extensions that are refused on sight regardless of content.
DANGEROUS_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe",
        ".dll",
        ".sys",
        ".scr",
        ".com",
        ".pif",
        ".msi",
        ".msp",
        ".bat",
        ".cmd",
        ".ps1",
        ".psm1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".hta",
        ".jar",
        ".sh",
        ".bash",
        ".zsh",
        ".py",
        ".pyc",
        ".so",
        ".dylib",
        ".lnk",
        ".reg",
        ".cpl",
        ".msc",
    }
)

#: ZIP local-file-header magic. OOXML (.xlsx/.docx) is a ZIP container, so this
#: prefix is shared by several accepted kinds and cannot discriminate alone.
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

#: The only kinds PK-container bytes may legitimately be. Deliberately narrower
#: than ``EXTENSION_KINDS``: an archive named ``.csv`` or ``.pdf`` is a
#: mislabelling, not a disambiguation, and must be treated as the archive it is
#: so intake expands it. Falling back to the extension table here would route
#: binary container bytes to a text parser and silently omit the archive's real
#: contents from the review.
_CONTAINER_KINDS: dict[str, FileKind] = {
    ".xlsx": FileKind.XLSX,
    ".xlsm": FileKind.XLSX,
    ".docx": FileKind.DOCX,
    ".zip": FileKind.ZIP,
}

#: Filename patterns that suggest an engineering purpose. Used only to propose a
#: document role; the uploader's declaration always wins.
_ROLE_HINTS: tuple[tuple[DocumentRole, tuple[str, ...]], ...] = (
    (DocumentRole.INTERLOCK_SCHEDULE, ("interlock", "permissive", "trip")),
    (DocumentRole.IO_LIST, ("io list", "iolist", "i-o list", "io_list", "point list")),
    (
        DocumentRole.FUNCTIONAL_DESCRIPTION,
        ("functional", "fds", "control narrative", "narrative", "spec"),
    ),
    (DocumentRole.TESTING_NOTES, ("test", "fat", "sat", "commissioning")),
    (DocumentRole.ALARM_LIST, ("alarm",)),
    (DocumentRole.HMI_EXPORT, ("hmi", "faceplate", "display", "panelview")),
)

#: Bytes read from the head of a file for sniffing.
SNIFF_BYTES = 8192


@dataclass(slots=True)
class ValidatedUpload:
    """The intake verdict for one candidate file."""

    filename: str
    extension: str
    file_kind: FileKind
    media_type: str
    size_bytes: int
    suggested_role: DocumentRole = DocumentRole.UNCLASSIFIED
    #: Non-fatal observations, surfaced to the user rather than dropped.
    warnings: list[str] = field(default_factory=list)


def _extension_of(filename: str) -> str:
    return PurePosixPath(filename.replace("\\", "/")).suffix.lower()


def looks_like_executable(head: bytes) -> str | None:
    """Return a description if the content looks executable, else ``None``."""
    for signature, description in EXECUTABLE_SIGNATURES:
        if head.startswith(signature):
            return description
    return None


def sniff_kind(head: bytes, extension: str) -> FileKind:
    """Infer the kind from content, falling back to the extension.

    OOXML formats and plain ZIPs share a container, so for a ``PK`` header the
    declared extension chooses *among the container kinds* — that is safe because
    the relevant parser re-validates the container's internal structure and fails
    loudly if it is not what the extension claimed. Any other extension on PK
    bytes is treated as a plain archive so that ``validate_upload`` reports the
    mismatch and intake expands it, rather than handing container bytes to a
    parser that has no structure to validate.
    """
    if head.startswith(b"%PDF-"):
        return FileKind.PDF
    if head.startswith(_ZIP_MAGIC):
        return _CONTAINER_KINDS.get(extension, FileKind.ZIP)

    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith(b"<?xml") or stripped.startswith(b"<"):
        # An L5X is XML; only the extension says it is a Studio 5000 export.
        if extension == ".l5x" or b"<RSLogix5000Content" in head:
            return FileKind.L5X
        return FileKind.XML

    return EXTENSION_KINDS.get(extension, FileKind.UNKNOWN)


def suggest_document_role(filename: str, kind: FileKind) -> DocumentRole:
    """Propose an engineering purpose from the filename.

    A guess, and treated as one: it seeds the UI's dropdown and is overridden by
    anything the uploader declares. Nothing downstream treats it as fact.
    """
    if kind is FileKind.L5X:
        return DocumentRole.CONTROL_LOGIC

    lowered = filename.lower()
    for role, needles in _ROLE_HINTS:
        if any(needle in lowered for needle in needles):
            return role
    return DocumentRole.UNCLASSIFIED


def validate_upload(
    *,
    filename: str,
    head: bytes,
    size_bytes: int,
    max_bytes: int,
    declared_media_type: str = "",
) -> ValidatedUpload:
    """Decide whether a candidate file may be stored.

    Raises :class:`FileRejectedError` or
    :class:`~app.core.errors.UnsupportedContentError` with an explanation the
    user can act on. Never accepts a file it cannot route to a parser.
    """
    extension = _extension_of(filename)

    if size_bytes <= 0:
        raise FileRejectedError("File is empty.", details={"filename": filename})
    if size_bytes > max_bytes:
        raise FileRejectedError(
            f"File is {size_bytes:,} bytes, which exceeds the {max_bytes:,} byte per-file limit.",
            details={"filename": filename, "size_bytes": size_bytes, "limit": max_bytes},
        )

    if extension in DANGEROUS_EXTENSIONS:
        raise FileRejectedError(
            f"Files with the {extension} extension are not accepted.",
            details={"filename": filename, "extension": extension},
        )

    if (executable := looks_like_executable(head)) is not None:
        raise FileRejectedError(
            f"File content is a {executable}, which is not accepted regardless of its filename.",
            details={"filename": filename, "detected": executable},
        )

    if (reason := EXPLICITLY_UNSUPPORTED.get(extension)) is not None:
        raise FileRejectedError(reason, code="unsupported_content", details={"filename": filename})

    kind = sniff_kind(head, extension)
    if kind is FileKind.UNKNOWN:
        raise FileRejectedError(
            f"Unrecognised file type '{extension or filename}'. Supported types: "
            + ", ".join(sorted(EXTENSION_KINDS)),
            code="unsupported_content",
            details={"filename": filename, "extension": extension},
        )

    warnings: list[str] = []

    expected_kind = EXTENSION_KINDS.get(extension)
    if expected_kind is not None and expected_kind is not kind:
        # Store it, route it by content, and say so — this is exactly the kind of
        # thing an engineer needs told rather than silently corrected.
        warnings.append(
            f"Content looks like {kind.value} but the extension says "
            f"{expected_kind.value}; parsing as {kind.value}."
        )

    hints = MEDIA_TYPE_HINTS.get(kind, ())
    if declared_media_type and hints and declared_media_type not in hints:
        warnings.append(
            f"Client reported media type '{declared_media_type}', which does not "
            f"match the detected {kind.value} content."
        )

    return ValidatedUpload(
        filename=filename,
        extension=extension,
        file_kind=kind,
        media_type=declared_media_type or (hints[0] if hints else "application/octet-stream"),
        size_bytes=size_bytes,
        suggested_role=suggest_document_role(filename, kind),
        warnings=warnings,
    )
