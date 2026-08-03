"""Deterministic ZIP delivery for a finalized engineering release directory.

The Rockwell release core owns candidate creation, generation, validation,
approval, and the package manifest.  This delivery-owned boundary performs one
smaller job: verify the package already on disk and place those exact bytes in a
portable ZIP container.  It never creates engineering content, changes a
validation level, or infers approval.

The returned metadata is suitable for a future backend-owned download endpoint.
Until that endpoint is contracted, callers can test and integrate this service
without bypassing Project Core or release authorization.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.enums import (
    VALIDATION_LEVEL_ORDER,
    ImportVerificationStatus,
    ObjectApprovalState,
    ReleaseCandidateStatus,
    ValidationLevel,
)

MANIFEST_PATH = "00_Manifest/manifest.json"
CHECKSUM_PATH = "00_Manifest/checksums.sha256"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
MAX_RELEASE_FILE_COUNT = 10_000
MAX_RELEASE_BYTES = 512 * 1024 * 1024
MAX_RELEASE_PATH_DEPTH = 16
MAX_RELEASE_PATH_BYTES = 512
ZIP_MEDIA_TYPE = "application/zip"
SUPPORTED_MANIFEST_VERSION = "1.0"
REQUIRED_RELEASE_DIRECTORIES = (
    "00_Manifest",
    "01_Controller",
    "02_HMI",
    "03_Alarms",
    "04_Testing",
    "05_Reports",
    "06_Audit",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHECKSUM_LINE = re.compile(r"^(?P<sha>[0-9a-fA-F]{64})  (?P<path>.+)$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class ReleaseContainerErrorCode(StrEnum):
    """Stable reasons a release directory cannot be put in a delivery ZIP."""

    PACKAGE_NOT_FOUND = "package_not_found"
    PACKAGE_OUTSIDE_PROJECT_STORAGE = "package_outside_project_storage"
    UNTRUSTED_RELEASE = "untrusted_release"
    UNSAFE_PATH = "unsafe_path"
    UNSUPPORTED_ENTRY = "unsupported_entry"
    PACKAGE_TOO_LARGE = "package_too_large"
    PACKAGE_LAYOUT_MISMATCH = "package_layout_mismatch"
    MISSING_REQUIRED_FILE = "missing_required_file"
    INVALID_MANIFEST = "invalid_manifest"
    MANIFEST_MISMATCH = "manifest_mismatch"
    INVALID_CHECKSUM_FILE = "invalid_checksum_file"
    CHECKSUM_INDEX_MISMATCH = "checksum_index_mismatch"
    CHECKSUM_MISMATCH = "checksum_mismatch"


class ReleaseContainerError(ValueError):
    """A release directory failed closed before any ZIP was returned."""

    def __init__(self, code: ReleaseContainerErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ReleaseFileAnchor:
    """One immutable artifact row captured when the release was frozen."""

    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ReleaseTrustAnchor:
    """Trusted database facts required before packaging customer-facing bytes.

    The backend constructs this only after resolving the candidate beneath its
    canonical project store and loading the frozen manifest plus artifact rows.
    Requiring it here prevents a self-consistent, locally re-signed directory or
    a pre-approval generated package from becoming a delivery ZIP.
    """

    project_id: uuid.UUID
    candidate_id: uuid.UUID
    manifest_id: uuid.UUID
    approved_by_id: uuid.UUID
    frozen_at: datetime
    candidate_status: ReleaseCandidateStatus
    approval_state: ObjectApprovalState
    source_revision: int
    validation_level: ValidationLevel
    import_verification_status: ImportVerificationStatus
    import_verification_detail: str
    manifest_sha256: str
    checksum_file_sha256: str
    package_bytes: int
    artifact_count: int
    artifacts: tuple[ReleaseFileAnchor, ...]


@dataclass(frozen=True, slots=True)
class ReleaseZipArtifact:
    """Exact ZIP bytes and non-inflating metadata for persistence or download."""

    filename: str
    media_type: str
    data: bytes
    size_bytes: int
    sha256: str
    file_count: int
    source_revision: int
    validation_level: str
    import_verification_status: str
    import_verification_detail: str
    rockwell_import_verified: bool
    project_id: uuid.UUID
    candidate_id: uuid.UUID
    manifest_id: uuid.UUID
    approved_by_id: uuid.UUID
    frozen_at: datetime
    manifest_sha256: str
    checksum_file_sha256: str

    def metadata_document(self) -> dict[str, Any]:
        """Serialize metadata without duplicating the potentially large bytes."""

        return {
            "filename": self.filename,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "file_count": self.file_count,
            "source_revision": self.source_revision,
            "validation_level": self.validation_level,
            "import_verification_status": self.import_verification_status,
            "import_verification_detail": self.import_verification_detail,
            "rockwell_import_verified": self.rockwell_import_verified,
            "project_id": str(self.project_id),
            "candidate_id": str(self.candidate_id),
            "manifest_id": str(self.manifest_id),
            "approved_by_id": str(self.approved_by_id),
            "frozen_at": self.frozen_at.isoformat(),
            "manifest_sha256": self.manifest_sha256,
            "checksum_file_sha256": self.checksum_file_sha256,
        }


@dataclass(frozen=True, slots=True)
class _VerifiedPackage:
    root: Path
    directories: tuple[str, ...]
    files: tuple[tuple[str, bytes], ...]
    trust_anchor: ReleaseTrustAnchor
    manifest_sha256: str
    checksum_file_sha256: str


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative_path(raw: str) -> str:
    if (
        not raw
        or "\\" in raw
        or ":" in raw
        or any(character in '<>"|?*' for character in raw)
        or any(ord(character) < 32 for character in raw)
        or unicodedata.normalize("NFC", raw) != raw
        or len(raw.encode("utf-8")) > MAX_RELEASE_PATH_BYTES
    ):
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.UNSAFE_PATH,
            f"Release path {raw!r} is not a canonical POSIX relative path.",
        )
    path = PurePosixPath(raw)
    canonical = path.as_posix()
    if (
        path.is_absolute()
        or canonical != raw
        or len(path.parts) > MAX_RELEASE_PATH_DEPTH
        or any(
            part in {"", ".", ".."}
            or part != part.strip()
            or part.endswith(".")
            or len(part.encode("utf-8")) > 255
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
            for part in path.parts
        )
    ):
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.UNSAFE_PATH,
            f"Release path {raw!r} escapes or aliases the package root.",
        )
    return canonical


def _safe_archive_root(name: str) -> str:
    safe = _safe_relative_path(name)
    if "/" in safe or ":" in safe:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.UNSAFE_PATH,
            f"Release directory name {name!r} is not one safe archive component.",
        )
    return safe


def _verify_portable_path_uniqueness(
    directories: tuple[str, ...], files: dict[str, bytes]
) -> None:
    seen: dict[str, str] = {}
    for relative in (*directories, *files):
        portable_key = "/".join(
            unicodedata.normalize("NFC", part).casefold()
            for part in PurePosixPath(relative).parts
        )
        previous = seen.get(portable_key)
        if previous is not None and previous != relative:
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.UNSAFE_PATH,
                f"Release paths {previous!r} and {relative!r} collide on Windows.",
            )
        seen[portable_key] = relative


def _read_tree(
    package_root: Path, *, project_storage_root: Path
) -> tuple[Path, tuple[str, ...], dict[str, bytes]]:
    try:
        storage_root = project_storage_root.resolve(strict=True)
    except OSError as error:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.PACKAGE_NOT_FOUND,
            f"Canonical project storage {project_storage_root} does not exist.",
        ) from error
    if not storage_root.is_dir():
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.PACKAGE_NOT_FOUND,
            f"Canonical project storage {project_storage_root} is not a directory.",
        )
    try:
        unresolved_root_metadata = package_root.lstat()
    except OSError as error:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.PACKAGE_NOT_FOUND,
            f"Release package directory {package_root} does not exist.",
        ) from error
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(unresolved_root_metadata.st_mode) or bool(
        getattr(unresolved_root_metadata, "st_file_attributes", 0) & reparse_attribute
    ):
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.UNSUPPORTED_ENTRY,
            "The release package root cannot be a link or reparse point.",
        )
    try:
        root = package_root.resolve(strict=True)
    except OSError as error:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.PACKAGE_NOT_FOUND,
            f"Release package directory {package_root} does not exist.",
        ) from error
    if not root.is_dir():
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.PACKAGE_NOT_FOUND,
            f"Release package path {package_root} is not a directory.",
        )
    if root == storage_root or not root.is_relative_to(storage_root):
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.PACKAGE_OUTSIDE_PROJECT_STORAGE,
            "Release package is not a child of the canonical project storage root.",
        )

    directories: list[str] = []
    files: dict[str, bytes] = {}
    total_bytes = 0
    entry_count = 0
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.UNSUPPORTED_ENTRY,
                f"Release directory {current} cannot be enumerated.",
            ) from error
        for entry in children:
            entry_count += 1
            if entry_count > MAX_RELEASE_FILE_COUNT:
                raise ReleaseContainerError(
                    ReleaseContainerErrorCode.PACKAGE_TOO_LARGE,
                    f"Release package exceeds {MAX_RELEASE_FILE_COUNT} filesystem entries.",
                )
            relative = _safe_relative_path(entry.relative_to(root).as_posix())
            try:
                metadata = entry.lstat()
            except OSError as error:
                raise ReleaseContainerError(
                    ReleaseContainerErrorCode.UNSUPPORTED_ENTRY,
                    f"Release entry {relative} cannot be inspected.",
                ) from error
            is_reparse_point = bool(
                getattr(metadata, "st_file_attributes", 0) & reparse_attribute
            )
            if stat.S_ISLNK(metadata.st_mode) or is_reparse_point:
                raise ReleaseContainerError(
                    ReleaseContainerErrorCode.UNSUPPORTED_ENTRY,
                    f"Release packages cannot contain links or reparse points: {relative}.",
                )
            try:
                resolved = entry.resolve(strict=True)
            except OSError as error:
                raise ReleaseContainerError(
                    ReleaseContainerErrorCode.UNSUPPORTED_ENTRY,
                    f"Release entry {relative} cannot be resolved.",
                ) from error
            if not resolved.is_relative_to(root):
                raise ReleaseContainerError(
                    ReleaseContainerErrorCode.UNSAFE_PATH,
                    f"Release entry {relative} resolves outside the package root.",
                )
            if stat.S_ISDIR(metadata.st_mode):
                directories.append(relative)
                pending.append(entry)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ReleaseContainerError(
                    ReleaseContainerErrorCode.UNSUPPORTED_ENTRY,
                    f"Release entry {relative} is neither a regular file nor a directory.",
                )
            size = metadata.st_size
            if size < 0 or total_bytes + size > MAX_RELEASE_BYTES:
                raise ReleaseContainerError(
                    ReleaseContainerErrorCode.PACKAGE_TOO_LARGE,
                    f"Release package exceeds {MAX_RELEASE_BYTES} bytes.",
                )
            data = entry.read_bytes()
            if len(data) != size:
                raise ReleaseContainerError(
                    ReleaseContainerErrorCode.UNSUPPORTED_ENTRY,
                    f"Release entry {relative} changed while it was being read.",
                )
            try:
                after_read = entry.lstat()
            except OSError as error:
                raise ReleaseContainerError(
                    ReleaseContainerErrorCode.UNSUPPORTED_ENTRY,
                    f"Release entry {relative} disappeared while it was being read.",
                ) from error
            identity_before = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_size,
                metadata.st_mtime_ns,
            )
            identity_after = (
                after_read.st_dev,
                after_read.st_ino,
                after_read.st_mode,
                after_read.st_size,
                after_read.st_mtime_ns,
            )
            if identity_after != identity_before:
                raise ReleaseContainerError(
                    ReleaseContainerErrorCode.UNSUPPORTED_ENTRY,
                    f"Release entry {relative} changed while it was being read.",
                )
            total_bytes += len(data)
            files[relative] = data
    return root, tuple(sorted(directories)), files


def _required_bytes(files: dict[str, bytes], relative_path: str) -> bytes:
    try:
        return files[relative_path]
    except KeyError as error:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.MISSING_REQUIRED_FILE,
            f"Release package is missing {relative_path}.",
        ) from error


def _manifest_document(data: bytes, *, release_directory: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError(f"duplicate JSON key: {key}")
            document[key] = value
        return document

    def reject_nonfinite_number(value: str) -> Any:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.INVALID_MANIFEST,
            "Release manifest is not valid UTF-8 JSON.",
        ) from error
    if not isinstance(document, dict):
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.INVALID_MANIFEST,
            "Release manifest root must be an object.",
        )
    project = document.get("project")
    if not isinstance(project, dict) or project.get("release_directory") != release_directory:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.MANIFEST_MISMATCH,
            "Release manifest directory does not match the directory being archived.",
        )
    return document


def _manifest_artifacts(document: dict[str, Any]) -> dict[str, tuple[int, str]]:
    raw_artifacts = document.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.INVALID_MANIFEST,
            "Release manifest must contain an artifacts list.",
        )
    artifacts: dict[str, tuple[int, str]] = {}
    for raw in raw_artifacts:
        if not isinstance(raw, dict):
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.INVALID_MANIFEST,
                "Every manifest artifact must be an object.",
            )
        relative = raw.get("relative_path")
        size = raw.get("size_bytes")
        sha256 = raw.get("sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(sha256, str)
            or not _SHA256.fullmatch(sha256)
        ):
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.INVALID_MANIFEST,
                "A manifest artifact has an invalid path, size, or SHA-256 value.",
            )
        safe = _safe_relative_path(relative)
        if safe in artifacts:
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.INVALID_MANIFEST,
                f"Manifest repeats artifact path {safe}.",
            )
        artifacts[safe] = (size, sha256)
    return artifacts


def _checksum_index(data: bytes) -> dict[str, str]:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.INVALID_CHECKSUM_FILE,
            "Release checksum file is not valid UTF-8 text.",
        ) from error
    if not lines:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.INVALID_CHECKSUM_FILE,
            "Release checksum file is empty.",
        )
    checksums: dict[str, str] = {}
    for line in lines:
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.INVALID_CHECKSUM_FILE,
                "Release checksum file is not canonical sha256sum text.",
            )
        relative = _safe_relative_path(match.group("path"))
        if relative == CHECKSUM_PATH or relative in checksums:
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.INVALID_CHECKSUM_FILE,
                f"Release checksum index contains an invalid duplicate or self-entry: {relative}.",
            )
        checksums[relative] = match.group("sha").lower()
    return checksums


def _verification_facts(document: dict[str, Any]) -> tuple[int, str, bool]:
    project = document.get("project")
    verification = document.get("verification")
    if not isinstance(project, dict) or not isinstance(verification, dict):
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.INVALID_MANIFEST,
            "Release manifest is missing project or verification metadata.",
        )
    revision = project.get("revision_number")
    level = verification.get("package_level")
    import_verified = verification.get("rockwell_import_verified")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
        or not isinstance(level, str)
        or not level
        or not isinstance(import_verified, bool)
    ):
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.INVALID_MANIFEST,
            "Release manifest contains invalid revision or verification metadata.",
        )
    return revision, level, import_verified


def _trusted_artifacts(anchor: ReleaseTrustAnchor) -> dict[str, tuple[int, str]]:
    if (
        not all(
            isinstance(value, uuid.UUID)
            for value in (
                anchor.project_id,
                anchor.candidate_id,
                anchor.manifest_id,
                anchor.approved_by_id,
            )
        )
        or not isinstance(anchor.frozen_at, datetime)
        or anchor.frozen_at.tzinfo is None
        or anchor.frozen_at.utcoffset() is None
        or not isinstance(anchor.candidate_status, ReleaseCandidateStatus)
        or anchor.candidate_status
        not in {ReleaseCandidateStatus.APPROVED, ReleaseCandidateStatus.RELEASED}
        or anchor.approval_state is not ObjectApprovalState.APPROVED
        or not isinstance(anchor.validation_level, ValidationLevel)
        or not isinstance(anchor.import_verification_status, ImportVerificationStatus)
        or not isinstance(anchor.source_revision, int)
        or isinstance(anchor.source_revision, bool)
        or anchor.source_revision < 0
        or not isinstance(anchor.import_verification_detail, str)
        or not isinstance(anchor.manifest_sha256, str)
        or not _SHA256.fullmatch(anchor.manifest_sha256)
        or not isinstance(anchor.checksum_file_sha256, str)
        or not _SHA256.fullmatch(anchor.checksum_file_sha256)
        or not isinstance(anchor.package_bytes, int)
        or isinstance(anchor.package_bytes, bool)
        or anchor.package_bytes < 0
        or not isinstance(anchor.artifact_count, int)
        or isinstance(anchor.artifact_count, bool)
        or anchor.artifact_count < 1
        or not isinstance(anchor.artifacts, tuple)
        or anchor.artifact_count != len(anchor.artifacts)
    ):
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.UNTRUSTED_RELEASE,
            "Release trust data is incomplete, invalid, or does not prove human approval.",
        )

    passed = anchor.import_verification_status is ImportVerificationStatus.PASSED
    reached_import_level = (
        VALIDATION_LEVEL_ORDER[anchor.validation_level]
        >= VALIDATION_LEVEL_ORDER[ValidationLevel.ROCKWELL_IMPORT_VERIFIED]
    )
    if passed != reached_import_level:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.UNTRUSTED_RELEASE,
            "Trusted import-verification status and validation level are inconsistent.",
        )

    artifacts: dict[str, tuple[int, str]] = {}
    for artifact in anchor.artifacts:
        if (
            not isinstance(artifact, ReleaseFileAnchor)
            or not isinstance(artifact.relative_path, str)
            or not isinstance(artifact.size_bytes, int)
            or isinstance(artifact.size_bytes, bool)
            or artifact.size_bytes < 0
            or not isinstance(artifact.sha256, str)
            or not _SHA256.fullmatch(artifact.sha256)
        ):
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.UNTRUSTED_RELEASE,
                "A trusted release artifact has invalid size or SHA-256 metadata.",
            )
        relative = _safe_relative_path(artifact.relative_path)
        if relative in artifacts:
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.UNTRUSTED_RELEASE,
                f"Trusted release data repeats artifact path {relative}.",
            )
        artifacts[relative] = (artifact.size_bytes, artifact.sha256)

    if sum(size for size, _sha in artifacts.values()) != anchor.package_bytes:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.UNTRUSTED_RELEASE,
            "Trusted artifact sizes do not add up to the recorded package byte count.",
        )
    manifest_anchor = artifacts.get(MANIFEST_PATH)
    checksum_anchor = artifacts.get(CHECKSUM_PATH)
    if (
        manifest_anchor is None
        or checksum_anchor is None
        or manifest_anchor[1] != anchor.manifest_sha256
        or checksum_anchor[1] != anchor.checksum_file_sha256
    ):
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.UNTRUSTED_RELEASE,
            "Trusted manifest or checksum-file identity does not match its artifact row.",
        )
    return artifacts


def _verify_directory_layout(directories: tuple[str, ...], files: dict[str, bytes]) -> None:
    required = set(REQUIRED_RELEASE_DIRECTORIES)
    expected = set(required)
    for relative in files:
        path = PurePosixPath(relative)
        if not path.parts or path.parts[0] not in required:
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.PACKAGE_LAYOUT_MISMATCH,
                f"Release file {relative} is outside the required seven-folder layout.",
            )
        parent = path.parent
        while parent != PurePosixPath("."):
            expected.add(parent.as_posix())
            parent = parent.parent
    if set(directories) != expected:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.PACKAGE_LAYOUT_MISMATCH,
            "Release directory tree does not exactly match the required package layout.",
        )


def _verify_package(
    package_root: Path,
    *,
    project_storage_root: Path,
    trust_anchor: ReleaseTrustAnchor,
) -> _VerifiedPackage:
    root, directories, files = _read_tree(
        package_root, project_storage_root=project_storage_root
    )
    trusted_artifacts = _trusted_artifacts(trust_anchor)
    _verify_portable_path_uniqueness(directories, files)
    _verify_directory_layout(directories, files)
    release_directory = _safe_archive_root(root.name)
    manifest_bytes = _required_bytes(files, MANIFEST_PATH)
    checksum_bytes = _required_bytes(files, CHECKSUM_PATH)
    manifest = _manifest_document(manifest_bytes, release_directory=release_directory)

    if manifest.get("manifest_version") != SUPPORTED_MANIFEST_VERSION:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.INVALID_MANIFEST,
            f"Unsupported release manifest version {manifest.get('manifest_version')!r}.",
        )

    if set(trusted_artifacts) != set(files):
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.UNTRUSTED_RELEASE,
            "Package files do not exactly match the frozen artifact rows.",
        )
    for relative, (expected_size, expected_sha) in trusted_artifacts.items():
        data = files[relative]
        if len(data) != expected_size or _digest(data) != expected_sha:
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.UNTRUSTED_RELEASE,
                f"Package file {relative} does not match its frozen artifact row.",
            )

    manifest_artifacts = _manifest_artifacts(manifest)
    expected_manifest_paths = set(files) - {MANIFEST_PATH, CHECKSUM_PATH}
    if set(manifest_artifacts) != expected_manifest_paths:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.MANIFEST_MISMATCH,
            "Manifest artifact paths do not exactly match the release package files.",
        )
    for relative, (expected_size, expected_sha) in manifest_artifacts.items():
        data = files[relative]
        if len(data) != expected_size or _digest(data) != expected_sha:
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.MANIFEST_MISMATCH,
                f"Manifest metadata does not match release artifact {relative}.",
            )

    checksums = _checksum_index(checksum_bytes)
    expected_checksum_paths = set(files) - {CHECKSUM_PATH}
    if set(checksums) != expected_checksum_paths:
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.CHECKSUM_INDEX_MISMATCH,
            "Checksum paths do not exactly cover every release file except the checksum file.",
        )
    for relative, expected_sha in checksums.items():
        if _digest(files[relative]) != expected_sha:
            raise ReleaseContainerError(
                ReleaseContainerErrorCode.CHECKSUM_MISMATCH,
                f"Release artifact {relative} does not match checksums.sha256.",
            )

    revision, level, import_verified = _verification_facts(manifest)
    trusted_import_verified = (
        trust_anchor.import_verification_status is ImportVerificationStatus.PASSED
    )
    if (
        revision != trust_anchor.source_revision
        or level != str(trust_anchor.validation_level)
        or import_verified is not trusted_import_verified
    ):
        raise ReleaseContainerError(
            ReleaseContainerErrorCode.UNTRUSTED_RELEASE,
            "Manifest revision or verification facts differ from the frozen release record.",
        )
    return _VerifiedPackage(
        root=root,
        directories=directories,
        files=tuple(sorted(files.items())),
        trust_anchor=trust_anchor,
        manifest_sha256=_digest(manifest_bytes),
        checksum_file_sha256=_digest(checksum_bytes),
    )


def _zip_info(name: str, *, is_directory: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = ((0o40755 if is_directory else 0o100644) << 16) | (
        0x10 if is_directory else 0
    )
    return info


def build_release_zip(
    package_root: Path,
    *,
    project_storage_root: Path,
    trust_anchor: ReleaseTrustAnchor,
) -> ReleaseZipArtifact:
    """Verify ``package_root`` and return one deterministic, unmodified ZIP.

    ZIP members use a fixed timestamp and stored (uncompressed) bytes so the
    same verified package has the same outer SHA-256 across zlib versions.  The
    package directory is retained as the archive root to make extraction safe.
    ``project_storage_root`` must be the canonical store resolved for the
    trusted project. ``trust_anchor`` must come from the approved candidate's
    frozen database manifest and exact artifact rows; on-disk indexes never
    authenticate themselves.
    """

    verified = _verify_package(
        package_root,
        project_storage_root=project_storage_root,
        trust_anchor=trust_anchor,
    )
    trust = verified.trust_anchor
    archive_root = _safe_archive_root(verified.root.name)
    members: list[tuple[str, bytes, bool]] = [(f"{archive_root}/", b"", True)]
    members.extend((f"{archive_root}/{name}/", b"", True) for name in verified.directories)
    members.extend((f"{archive_root}/{name}", data, False) for name, data in verified.files)

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data, is_directory in sorted(members, key=lambda item: item[0]):
            archive.writestr(_zip_info(name, is_directory=is_directory), data)
    payload = output.getvalue()
    return ReleaseZipArtifact(
        filename=f"{archive_root}.zip",
        media_type=ZIP_MEDIA_TYPE,
        data=payload,
        size_bytes=len(payload),
        sha256=_digest(payload),
        file_count=len(verified.files),
        source_revision=trust.source_revision,
        validation_level=str(trust.validation_level),
        import_verification_status=str(trust.import_verification_status),
        import_verification_detail=trust.import_verification_detail,
        rockwell_import_verified=(
            trust.import_verification_status is ImportVerificationStatus.PASSED
        ),
        project_id=trust.project_id,
        candidate_id=trust.candidate_id,
        manifest_id=trust.manifest_id,
        approved_by_id=trust.approved_by_id,
        frozen_at=trust.frozen_at,
        manifest_sha256=verified.manifest_sha256,
        checksum_file_sha256=verified.checksum_file_sha256,
    )


__all__ = [
    "CHECKSUM_PATH",
    "MANIFEST_PATH",
    "ZIP_MEDIA_TYPE",
    "ReleaseContainerError",
    "ReleaseContainerErrorCode",
    "ReleaseFileAnchor",
    "ReleaseTrustAnchor",
    "ReleaseZipArtifact",
    "build_release_zip",
]
