"""Deterministic, fail-closed delivery ZIP tests."""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import app.agents.delivery.release_container as release_container
from app.agents.delivery.release_container import (
    CHECKSUM_PATH,
    MANIFEST_PATH,
    REQUIRED_RELEASE_DIRECTORIES,
    ReleaseContainerError,
    ReleaseContainerErrorCode,
    ReleaseFileAnchor,
    ReleaseTrustAnchor,
    ReleaseZipArtifact,
    build_release_zip,
)
from app.core.enums import (
    ImportVerificationStatus,
    ObjectApprovalState,
    ReleaseCandidateStatus,
    ValidationLevel,
)
from app.generators.rockwell.package import build_release_package
from tests.generators.rockwell.conftest import build_demo_model, build_options

RELEASE_NAME = "ESCO_Project_Release_Line_4_R7"
PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000101")
CANDIDATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000102")
MANIFEST_ID = uuid.UUID("00000000-0000-0000-0000-000000000103")
APPROVER_ID = uuid.UUID("00000000-0000-0000-0000-000000000104")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_package(tmp_path: Path) -> Path:
    root = tmp_path / RELEASE_NAME
    for directory in (
        "00_Manifest",
        "01_Controller",
        "02_HMI",
        "03_Alarms",
        "04_Testing",
        "05_Reports",
        "06_Audit",
    ):
        (root / directory).mkdir(parents=True)

    artifacts = {
        "00_Manifest/LIMITATIONS.md": (
            b"# Known limitations\nNo Studio 5000 import was attempted.\n"
        ),
        "01_Controller/Line4.L5X": b'<RSLogix5000Content SchemaRevision="1.0"/>\n',
        "02_HMI/HMI_Tags.csv": b"TagName,DataType\nLine4_Run,Boolean\n",
        "03_Alarms/Alarm_Register.csv": b"AlarmId,Trigger\nALM-1,Line4_Fault\n",
        "04_Testing/IAT_Plan.xlsx": b"synthetic-xlsx-test-bytes",
        "05_Reports/Executive_Summary.pdf": b"%PDF-1.4 synthetic test bytes",
        "06_Audit/evidence_index.json": b'{"evidence":[]}\n',
    }
    for relative, data in artifacts.items():
        target = root / relative
        target.write_bytes(data)

    manifest = {
        "manifest_version": "1.0",
        "project": {
            "name": "Line 4 Packaging Conveyor Upgrade",
            "revision_number": 7,
            "release_directory": RELEASE_NAME,
        },
        "verification": {
            "package_level": "generated",
            "highest_level_attained": "round_trip_parsed",
            "rockwell_import_verified": False,
        },
        "artifacts": [
            {
                "relative_path": relative,
                "size_bytes": len(data),
                "sha256": _digest(data),
            }
            for relative, data in sorted(artifacts.items())
        ],
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    (root / MANIFEST_PATH).write_bytes(manifest_bytes)

    checksummed = {**artifacts, MANIFEST_PATH: manifest_bytes}
    checksums = "".join(
        f"{_digest(data)}  {relative}\n" for relative, data in sorted(checksummed.items())
    ).encode()
    (root / CHECKSUM_PATH).write_bytes(checksums)
    return root


def _reindex_package(package: Path) -> None:
    manifest_path = package / MANIFEST_PATH
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts = {
        path.relative_to(package).as_posix(): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file()
        and path.relative_to(package).as_posix() not in {MANIFEST_PATH, CHECKSUM_PATH}
    }
    manifest["artifacts"] = [
        {
            "relative_path": relative,
            "size_bytes": len(data),
            "sha256": _digest(data),
        }
        for relative, data in sorted(artifacts.items())
    ]
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    manifest_path.write_bytes(manifest_bytes)
    checksummed = {**artifacts, MANIFEST_PATH: manifest_bytes}
    (package / CHECKSUM_PATH).write_bytes(
        "".join(
            f"{_digest(data)}  {relative}\n" for relative, data in sorted(checksummed.items())
        ).encode()
    )


def _trust_anchor(
    package: Path,
    *,
    candidate_status: ReleaseCandidateStatus = ReleaseCandidateStatus.APPROVED,
    approval_state: ObjectApprovalState = ObjectApprovalState.APPROVED,
) -> ReleaseTrustAnchor:
    files = {
        path.relative_to(package).as_posix(): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file()
    }
    manifest = json.loads(files[MANIFEST_PATH])
    import_verified = manifest["verification"]["rockwell_import_verified"]
    return ReleaseTrustAnchor(
        project_id=PROJECT_ID,
        candidate_id=CANDIDATE_ID,
        manifest_id=MANIFEST_ID,
        approved_by_id=APPROVER_ID,
        frozen_at=datetime(2026, 8, 3, 16, 0, tzinfo=UTC),
        candidate_status=candidate_status,
        approval_state=approval_state,
        source_revision=manifest["project"]["revision_number"],
        validation_level=ValidationLevel(manifest["verification"]["package_level"]),
        import_verification_status=(
            ImportVerificationStatus.PASSED
            if import_verified
            else ImportVerificationStatus.UNAVAILABLE
        ),
        import_verification_detail=(
            "Verified by the configured workstation adapter."
            if import_verified
            else "No licensed workstation adapter is configured."
        ),
        manifest_sha256=_digest(files[MANIFEST_PATH]),
        checksum_file_sha256=_digest(files[CHECKSUM_PATH]),
        package_bytes=sum(len(data) for data in files.values()),
        artifact_count=len(files),
        artifacts=tuple(
            ReleaseFileAnchor(relative, len(data), _digest(data))
            for relative, data in sorted(files.items())
        ),
    )


def _build_zip(
    package: Path,
    trust: ReleaseTrustAnchor,
    *,
    project_storage_root: Path | None = None,
) -> ReleaseZipArtifact:
    return build_release_zip(
        package,
        project_storage_root=project_storage_root or package.parent,
        trust_anchor=trust,
    )


def test_release_zip_is_complete_deterministic_and_truthful(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    trust = _trust_anchor(package)

    first = _build_zip(package, trust)
    second = _build_zip(package, trust)

    assert first.data == second.data
    assert first.sha256 == _digest(first.data)
    assert first.filename == f"{RELEASE_NAME}.zip"
    assert first.media_type == "application/zip"
    assert first.file_count == 9
    assert first.source_revision == 7
    assert first.validation_level == "generated"
    assert first.import_verification_status == "unavailable"
    assert first.import_verification_detail == "No licensed workstation adapter is configured."
    assert first.rockwell_import_verified is False
    assert first.candidate_id == CANDIDATE_ID
    assert first.approved_by_id == APPROVER_ID
    assert first.metadata_document()["sha256"] == first.sha256
    assert "data" not in first.metadata_document()

    zip_path = tmp_path / first.filename
    zip_path.write_bytes(first.data)
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        assert f"{RELEASE_NAME}/00_Manifest/manifest.json" in names
        assert f"{RELEASE_NAME}/00_Manifest/checksums.sha256" in names
        assert f"{RELEASE_NAME}/01_Controller/Line4.L5X" in names
        assert f"{RELEASE_NAME}/02_HMI/HMI_Tags.csv" in names
        assert f"{RELEASE_NAME}/03_Alarms/Alarm_Register.csv" in names
        assert f"{RELEASE_NAME}/04_Testing/IAT_Plan.xlsx" in names
        assert f"{RELEASE_NAME}/05_Reports/Executive_Summary.pdf" in names
        assert f"{RELEASE_NAME}/06_Audit/evidence_index.json" in names
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert (
            archive.read(f"{RELEASE_NAME}/01_Controller/Line4.L5X")
            == (package / "01_Controller/Line4.L5X").read_bytes()
        )


def test_release_zip_accepts_the_real_rockwell_core_package_layout(tmp_path: Path) -> None:
    generated = build_release_package(build_demo_model(), build_options())
    package = generated.write(tmp_path / "RC-0001")
    trust = _trust_anchor(package)

    artifact = _build_zip(package, trust, project_storage_root=tmp_path)

    assert artifact.file_count == len(generated.files)
    assert artifact.validation_level == str(generated.validation_level)
    with zipfile.ZipFile(io.BytesIO(artifact.data)) as archive:
        roots = {name.split("/", 2)[1] for name in archive.namelist() if "/" in name}
    assert set(REQUIRED_RELEASE_DIRECTORIES).issubset(roots)


def test_release_zip_rejects_an_artifact_changed_after_manifesting(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    trust = _trust_anchor(package)
    (package / "01_Controller/Line4.L5X").write_bytes(b"tampered")

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, trust)

    assert caught.value.code is ReleaseContainerErrorCode.UNTRUSTED_RELEASE


def test_release_zip_rejects_unindexed_extra_files(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    trust = _trust_anchor(package)
    (package / "05_Reports/unreviewed.txt").write_text("not in the manifest", encoding="utf-8")

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, trust)

    assert caught.value.code is ReleaseContainerErrorCode.UNTRUSTED_RELEASE


def test_release_zip_rejects_unsafe_checksum_paths(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    checksum_file = package / CHECKSUM_PATH
    checksum_file.write_text(f"{'0' * 64}  ../outside.txt\n", encoding="utf-8")
    trust = _trust_anchor(package)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, trust)

    assert caught.value.code is ReleaseContainerErrorCode.UNSAFE_PATH


def test_release_zip_rejects_windows_illegal_checksum_paths(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    checksum_file = package / CHECKSUM_PATH
    checksum_file.write_text(f"{'0' * 64}  05_Reports/a?.txt\n", encoding="utf-8")
    trust = _trust_anchor(package)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, trust)

    assert caught.value.code is ReleaseContainerErrorCode.UNSAFE_PATH


def test_release_zip_rejects_portable_case_collisions() -> None:
    with pytest.raises(ReleaseContainerError) as caught:
        release_container._verify_portable_path_uniqueness(
            ("05_Reports",),
            {
                "05_Reports/Report.pdf": b"first",
                "05_Reports/report.pdf": b"second",
            },
        )

    assert caught.value.code is ReleaseContainerErrorCode.UNSAFE_PATH


def test_release_zip_rejects_a_relabelled_release_directory(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    trust = _trust_anchor(package)
    renamed = package.with_name("Different_Release")
    package.rename(renamed)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(renamed, trust)

    assert caught.value.code is ReleaseContainerErrorCode.MANIFEST_MISMATCH


def test_release_zip_rejects_a_generated_candidate_without_frozen_approval(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path)
    trust = _trust_anchor(package, candidate_status=ReleaseCandidateStatus.GENERATED)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, trust)

    assert caught.value.code is ReleaseContainerErrorCode.UNTRUSTED_RELEASE


def test_release_zip_rejects_a_package_outside_canonical_project_storage(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path)
    trust = _trust_anchor(package)
    other_project_store = tmp_path / "other-project"
    other_project_store.mkdir()

    with pytest.raises(ReleaseContainerError) as caught:
        build_release_zip(
            package,
            project_storage_root=other_project_store,
            trust_anchor=trust,
        )

    assert caught.value.code is ReleaseContainerErrorCode.PACKAGE_OUTSIDE_PROJECT_STORAGE


def test_release_zip_rejects_coordinated_artifact_and_index_tampering(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path)
    frozen_trust = _trust_anchor(package)
    (package / "01_Controller/Line4.L5X").write_bytes(b"coordinated tamper")
    _reindex_package(package)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, frozen_trust)

    assert caught.value.code is ReleaseContainerErrorCode.UNTRUSTED_RELEASE


def test_release_zip_rejects_an_inflated_import_verification_claim(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    frozen_trust = _trust_anchor(package)
    manifest_path = package / MANIFEST_PATH
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["verification"]["package_level"] = "rockwell_import_verified"
    manifest["verification"]["rockwell_import_verified"] = True
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _reindex_package(package)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, frozen_trust)

    assert caught.value.code is ReleaseContainerErrorCode.UNTRUSTED_RELEASE


def test_release_zip_rejects_a_trusted_digest_that_disagrees_with_artifact_rows(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path)
    trust = replace(_trust_anchor(package), manifest_sha256="0" * 64)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, trust)

    assert caught.value.code is ReleaseContainerErrorCode.UNTRUSTED_RELEASE


def test_release_zip_rejects_untracked_empty_directories(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    (package / "05_Reports/untracked-empty").mkdir()
    trust = _trust_anchor(package)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, trust)

    assert caught.value.code is ReleaseContainerErrorCode.PACKAGE_LAYOUT_MISMATCH


def test_release_zip_rejects_a_missing_required_empty_directory(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    (package / "02_HMI/HMI_Tags.csv").unlink()
    (package / "02_HMI").rmdir()
    _reindex_package(package)
    trust = _trust_anchor(package)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, trust)

    assert caught.value.code is ReleaseContainerErrorCode.PACKAGE_LAYOUT_MISMATCH


def test_release_zip_rejects_unsupported_manifest_versions(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    manifest_path = package / MANIFEST_PATH
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manifest_version"] = "99.0"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _reindex_package(package)
    trust = _trust_anchor(package)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, trust)

    assert caught.value.code is ReleaseContainerErrorCode.INVALID_MANIFEST


def test_release_zip_rejects_symbolic_links_before_reading_them(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not release content", encoding="utf-8")
    link = package / "05_Reports/linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symbolic links is unavailable on this Windows host.")
    trust = _trust_anchor(package)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, trust)

    assert caught.value.code is ReleaseContainerErrorCode.UNSUPPORTED_ENTRY


def test_release_zip_caps_all_filesystem_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _write_package(tmp_path)
    trust = _trust_anchor(package)
    monkeypatch.setattr(release_container, "MAX_RELEASE_FILE_COUNT", 8)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, trust)

    assert caught.value.code is ReleaseContainerErrorCode.PACKAGE_TOO_LARGE


def test_release_zip_rejects_a_checksum_that_disagrees_with_package_bytes(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path)
    checksum_path = package / CHECKSUM_PATH
    lines = checksum_path.read_text(encoding="utf-8").splitlines()
    checksum_path.write_text(
        "\n".join(
            f"{'f' * 64}  01_Controller/Line4.L5X"
            if line.endswith("  01_Controller/Line4.L5X")
            else line
            for line in lines
        )
        + "\n",
        encoding="utf-8",
    )
    trust = _trust_anchor(package)

    with pytest.raises(ReleaseContainerError) as caught:
        _build_zip(package, trust)

    assert caught.value.code is ReleaseContainerErrorCode.CHECKSUM_MISMATCH
