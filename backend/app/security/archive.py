"""Safe ZIP expansion.

An uploaded project package is untrusted input. Extraction here is defensive on
five axes, each of which is a real, documented attack:

* **path traversal** — a member named ``../../etc/passwd`` (Zip Slip);
* **absolute paths and drive letters** — ``/etc/passwd``, ``C:\\Windows\\...``;
* **zip bombs** — a small archive that expands to gigabytes;
* **entry-count floods** — hundreds of thousands of tiny members;
* **symlink and device members** — entries that are not regular files.

Nothing is written to disk until the whole archive has been proved acceptable
from its central directory, so a bomb is rejected before it costs anything.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.core.config import Settings
from app.core.errors import FileRejectedError
from app.core.logging import get_logger
from app.security.paths import TOO_DEEP_CODE, resolve_within, safe_relative_path

logger = get_logger(__name__)

#: Unix mode bits marking a symlink, in the high half of ``external_attr``.
_S_IFLNK = 0o120000
_S_IFMT = 0o170000
_S_IFREG = 0o100000
_S_IFDIR = 0o040000


@dataclass(slots=True)
class ExtractedMember:
    """One file successfully written out of an archive."""

    #: Sanitised path relative to the extraction root.
    relative_path: PurePosixPath
    #: The member name exactly as recorded in the archive, for provenance.
    original_name: str
    absolute_path: Path
    size_bytes: int


@dataclass(slots=True)
class ExtractionResult:
    members: list[ExtractedMember] = field(default_factory=list)
    #: Members deliberately not extracted, each with the reason.
    skipped: list[dict[str, str]] = field(default_factory=list)
    total_uncompressed_bytes: int = 0


def _member_is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & _S_IFMT == _S_IFLNK


def _member_is_special(info: zipfile.ZipInfo) -> bool:
    """True for anything that is neither a regular file nor a directory.

    Zero mode bits mean the archive was created by a tool that did not record
    Unix permissions (most Windows zippers), which is normal and not special.
    """
    mode = (info.external_attr >> 16) & _S_IFMT
    return mode not in (0, _S_IFREG, _S_IFDIR)


def inspect_archive(path: Path, settings: Settings) -> list[zipfile.ZipInfo]:
    """Validate an archive's central directory without extracting anything.

    Raises :class:`FileRejectedError` on the first disqualifying property and
    returns the members that should be extracted. Nothing is written and nothing
    is decompressed until every whole-archive property has been proved
    acceptable, so a bomb costs only a central-directory read.

    Order matters here. The cheap declared-size and entry-count limits come
    first; the CRC verification, which actually inflates every member, comes last
    — checking integrity first would decompress the very bomb the ratio limit
    exists to refuse.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()

            if len(infos) > settings.max_archive_entries:
                raise FileRejectedError(
                    f"Archive contains {len(infos):,} entries, above the "
                    f"{settings.max_archive_entries:,} limit.",
                    details={"entries": len(infos)},
                )

            total_uncompressed = sum(info.file_size for info in infos)
            total_compressed = sum(info.compress_size for info in infos)

            if total_uncompressed > settings.max_archive_uncompressed_bytes:
                raise FileRejectedError(
                    f"Archive expands to {total_uncompressed:,} bytes, above the "
                    f"{settings.max_archive_uncompressed_bytes:,} limit.",
                    details={"uncompressed_bytes": total_uncompressed},
                )

            if total_compressed > 0:
                ratio = total_uncompressed / total_compressed
                if ratio > settings.max_archive_compression_ratio:
                    raise FileRejectedError(
                        f"Archive compression ratio is {ratio:.0f}:1, above the "
                        f"{settings.max_archive_compression_ratio:.0f}:1 limit. This is "
                        "characteristic of a zip bomb.",
                        details={"ratio": round(ratio, 1)},
                    )

            # Prove every member name is containable before any of them is
            # written, so an archive whose last member traverses cannot leave the
            # first twenty behind while the error claims nothing was extracted.
            _reject_uncontainable_members(infos, archive_name=path.name)

            # Only now pay for inflating every member. The declared sizes are
            # already bounded, and zipfile caps each member's inflate at its
            # declared file_size, so this cannot exceed the limits above.
            if archive.testzip() is not None:
                raise FileRejectedError("Archive contains a corrupt entry.")
    except zipfile.BadZipFile as exc:
        raise FileRejectedError(f"File is not a readable ZIP archive: {exc}") from exc

    return infos


def _reject_uncontainable_members(infos: list[zipfile.ZipInfo], *, archive_name: str) -> None:
    """Refuse the whole archive if any member name cannot be safely contained.

    A traversing member disqualifies the archive: it is hostile input, not a
    naming quirk to route around. Over-nesting is reported separately, because
    it is a shape problem rather than an attack and saying otherwise would
    accuse a benign package.
    """
    for info in infos:
        if info.is_dir():
            continue
        try:
            safe_relative_path(info.filename)
        except FileRejectedError as exc:
            if exc.code == TOO_DEEP_CODE:
                raise FileRejectedError(
                    f"Archive member '{info.filename}' is nested too deeply to "
                    "extract. The archive was not extracted.",
                    details={"member": info.filename, "archive": archive_name},
                ) from exc
            logger.warning(
                "archive member attempted traversal",
                extra={"archive": archive_name, "member": info.filename},
            )
            raise FileRejectedError(
                f"Archive member '{info.filename}' attempts to write outside the "
                "project directory. The archive was not extracted.",
                details={"member": info.filename, "archive": archive_name},
            ) from exc


def extract_archive(
    archive_path: Path,
    destination: Path,
    settings: Settings,
    *,
    max_bytes_per_member: int | None = None,
) -> ExtractionResult:
    """Expand an archive into ``destination``, safely.

    ``destination`` must already be inside the project tree; every member is
    re-checked against it after resolution, so neither a crafted member name nor
    a symlink already present on disk can redirect a write.

    :func:`inspect_archive` runs first and raises before ``destination`` is even
    created, so a rejected archive never leaves a partial tree behind.
    """
    infos = inspect_archive(archive_path, settings)
    destination.mkdir(parents=True, exist_ok=True)
    per_member_limit = max_bytes_per_member or settings.max_upload_bytes

    result = ExtractionResult()
    seen: set[str] = set()

    with zipfile.ZipFile(archive_path) as archive:
        for info in infos:
            name = info.filename

            if info.is_dir():
                continue

            if _member_is_symlink(info):
                result.skipped.append(
                    {"member": name, "reason": "symlink members are not extracted"}
                )
                continue

            if _member_is_special(info):
                result.skipped.append({"member": name, "reason": "member is not a regular file"})
                continue

            if info.file_size > per_member_limit:
                result.skipped.append(
                    {
                        "member": name,
                        "reason": (
                            f"member is {info.file_size:,} bytes, above the "
                            f"{per_member_limit:,} per-file limit"
                        ),
                    }
                )
                continue

            # Already proved containable by inspect_archive; kept as defence in
            # depth so this function is safe to call on its own.
            relative = safe_relative_path(name)

            key = str(relative).lower()
            if key in seen:
                result.skipped.append(
                    {"member": name, "reason": "duplicate path after sanitisation"}
                )
                continue
            seen.add(key)

            target = resolve_within(destination, relative)
            target.parent.mkdir(parents=True, exist_ok=True)

            written = _copy_member(archive, info, target, per_member_limit)
            result.members.append(
                ExtractedMember(
                    relative_path=relative,
                    original_name=name,
                    absolute_path=target,
                    size_bytes=written,
                )
            )
            result.total_uncompressed_bytes += written

    logger.info(
        "archive extracted",
        extra={
            "archive": archive_path.name,
            "extracted": len(result.members),
            "skipped": len(result.skipped),
            "bytes": result.total_uncompressed_bytes,
        },
    )
    return result


def _copy_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    limit: int,
) -> int:
    """Stream one member to disk, enforcing the size limit as it goes.

    The declared ``file_size`` is attacker-controlled, so the real byte count is
    capped during the copy rather than trusted beforehand.
    """
    written = 0
    with archive.open(info) as source, target.open("wb") as sink:
        while chunk := source.read(64 * 1024):
            written += len(chunk)
            if written > limit:
                sink.close()
                target.unlink(missing_ok=True)
                raise FileRejectedError(
                    f"Archive member '{info.filename}' expanded past the "
                    f"{limit:,} byte limit; its declared size was understated.",
                    details={"member": info.filename},
                )
            sink.write(chunk)
    return written
