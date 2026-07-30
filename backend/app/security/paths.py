"""Filename sanitisation and containment checks.

Every path the application writes is built from a trusted root plus components
that have been through :func:`safe_filename`, and every resolved path is checked
with :func:`resolve_within`. Client-supplied text never reaches the filesystem
unmodified.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath

from app.core.errors import FileRejectedError

#: Characters that are illegal on Windows or meaningful to a shell/path parser.
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_REPEATED_DOTS = re.compile(r"\.{2,}")
_WHITESPACE = re.compile(r"\s+")

#: Reserved device names on Windows. A file called ``CON.txt`` is not creatable,
#: and on some tooling paths it opens a device instead.
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

MAX_FILENAME_LENGTH = 180
MAX_PATH_COMPONENTS = 16

#: Error codes for the two distinct ways a member path can be refused. Callers
#: branch on these so a merely over-nested archive is not reported as hostile.
TRAVERSAL_CODE = "path_traversal"
TOO_DEEP_CODE = "path_too_deep"


def safe_filename(name: str, *, fallback: str = "unnamed") -> str:
    """Reduce an arbitrary client-supplied name to a safe single path component.

    Strips directory components, normalises Unicode, removes control and
    path-significant characters, defuses Windows reserved names, and bounds the
    length while preserving the extension.
    """
    # Take the last component under both separators: a Windows client can send
    # "C:\\Users\\x\\report.pdf" and a POSIX one "../../etc/passwd".
    candidate = str(name).replace("\\", "/")
    candidate = PurePosixPath(candidate).name

    candidate = unicodedata.normalize("NFKD", candidate)
    candidate = _UNSAFE_CHARS.sub("_", candidate)
    candidate = _REPEATED_DOTS.sub(".", candidate)
    candidate = _WHITESPACE.sub(" ", candidate).strip(" .")

    if not candidate:
        return fallback

    stem, dot, suffix = candidate.rpartition(".")
    if not dot:
        stem, suffix = candidate, ""

    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"

    if not stem:
        stem = fallback

    # Bound the total length, keeping the extension intact so type detection and
    # human recognition still work.
    suffix = suffix[:20]
    max_stem = MAX_FILENAME_LENGTH - (len(suffix) + 1 if suffix else 0)
    stem = stem[:max_stem].strip(" .") or fallback

    return f"{stem}.{suffix}" if suffix else stem


def safe_relative_path(raw: str, *, fallback: str = "unnamed") -> PurePosixPath:
    """Sanitise a multi-component relative path (e.g. a ZIP member name).

    Every component is sanitised individually; ``.``, ``..``, absolute prefixes
    and drive letters are dropped rather than escaped, so the result is always
    strictly below the root it is joined to.
    """
    text = str(raw).replace("\\", "/")

    # Drop a drive letter or UNC prefix before splitting.
    if re.match(r"^[a-zA-Z]:", text):
        text = text[2:]
    text = text.lstrip("/")

    parts: list[str] = []
    for component in PurePosixPath(text).parts:
        if component in {"", ".", "/"}:
            continue
        if component == "..":
            # Refuse rather than silently reinterpret: a member that tries to
            # traverse is a signal about the archive, not a formatting quirk.
            raise FileRejectedError(
                "Archive member path attempts directory traversal.",
                code=TRAVERSAL_CODE,
                details={"member": str(raw)},
            )
        parts.append(safe_filename(component, fallback=fallback))

    if not parts:
        return PurePosixPath(fallback)
    if len(parts) > MAX_PATH_COMPONENTS:
        # A distinct code from traversal: deep nesting is a shape problem, not an
        # attack, and reporting it as one would accuse a benign archive.
        raise FileRejectedError(
            f"Archive member path is nested more than {MAX_PATH_COMPONENTS} levels deep.",
            code=TOO_DEEP_CODE,
            details={"member": str(raw)},
        )
    return PurePosixPath(*parts)


def resolve_within(root: Path, *parts: str | PurePosixPath | Path) -> Path:
    """Join ``parts`` onto ``root`` and prove the result stays inside it.

    The final containment check is done on the *resolved* path, so a symlink
    planted inside the data directory cannot be used to write outside it.
    """
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*(str(part) for part in parts)).resolve()

    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise FileRejectedError(
            "Resolved path escapes its project directory.",
            details={"root": str(root_resolved), "resolved": str(candidate)},
        )
    return candidate


def unique_path(directory: Path, filename: str) -> Path:
    """Return a non-colliding path inside ``directory``.

    Two uploads named ``IO List.xlsx`` must both survive; the second becomes
    ``IO List (2).xlsx``. Never overwrites an existing file.
    """
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem, dot, suffix = filename.rpartition(".")
    if not dot:
        stem, suffix = filename, ""

    for counter in range(2, 10_000):
        name = f"{stem} ({counter}).{suffix}" if suffix else f"{stem} ({counter})"
        candidate = directory / name
        if not candidate.exists():
            return candidate

    raise FileRejectedError(
        "Too many files with this name already exist in the project.",
        details={"filename": filename},
    )
