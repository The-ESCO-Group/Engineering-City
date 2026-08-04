"""Persistence, recovery, tamper detection, and HTTP policy for release ZIPs."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.api.routes.releases as release_routes
import app.engineering.release_archive as archive_service
from app.api.routes.releases import router as release_router
from app.core.clock import utcnow
from app.core.config import Settings, get_settings
from app.core.enums import (
    ApprovalGateKey,
    GateStatus,
    ImportVerificationStatus,
    ObjectApprovalState,
    ReleaseCandidateStatus,
)
from app.database import session as db_session
from app.engineering.release_archive import (
    ReleaseArchiveRefused,
    finalize_release_archive,
    open_verified_archive,
)
from app.generators.rockwell import build_release_package
from app.models.project import Project
from app.models.release import ReleaseCandidate
from app.models.release_archive import ReleaseArchive
from app.models.revision import ApprovalGate
from app.models.user import User
from tests.conftest import TEST_PASSWORD, login
from tests.generators.rockwell.conftest import build_demo_model, build_options


@pytest.fixture
def approved_release(
    db: Session,
    project: Project,
    seeded: dict[str, User],
    settings: Settings,
) -> ReleaseCandidate:
    """A real seven-folder package with exact candidate/artifact/manifest rows."""

    approver = seeded["reviewer"]
    generated = build_release_package(build_demo_model(), build_options())
    release_dir = settings.project_dir(project.id) / "releases" / "RC-0001"
    release_dir.mkdir(parents=True)
    package_root = generated.write(release_dir)
    frozen_at = utcnow()

    candidate = ReleaseCandidate(
        project_id=project.id,
        revision_number=7,
        candidate_number=1,
        name="RC 1",
        status=ReleaseCandidateStatus.APPROVED,
        validation_level=generated.validation_level,
        import_verification_status=ImportVerificationStatus.UNAVAILABLE,
        import_verification_detail="No licensed workstation adapter is configured.",
        package_path=package_root.relative_to(settings.project_dir(project.id)).as_posix(),
        package_sha256=generated.file_at("00_Manifest/checksums.sha256").sha256,
        package_bytes=sum(file.size_bytes for file in generated.files),
        approval_state=ObjectApprovalState.APPROVED,
        approved_by_id=approver.id,
        approved_at=frozen_at,
        created_in_revision=7,
        updated_in_revision=7,
        created_by_id=seeded["engineer"].id,
    )
    db.add(candidate)
    db.flush()

    artifacts = generated.artifact_rows(
        project_id=project.id,
        release_candidate_id=candidate.id,
    )
    for artifact in artifacts:
        artifact.created_in_revision = 7
        artifact.updated_in_revision = 7
    db.add_all(artifacts)

    manifest = generated.manifest_row(
        project_id=project.id,
        release_candidate_id=candidate.id,
    )
    manifest.frozen_at = frozen_at
    manifest.frozen_by_id = approver.id
    manifest.approval_state = ObjectApprovalState.APPROVED
    manifest.approved_at = frozen_at
    manifest.approved_by_id = approver.id
    manifest.created_in_revision = 7
    manifest.updated_in_revision = 7
    manifest.created_by_id = approver.id
    db.add(manifest)
    db.commit()
    db.refresh(candidate)
    return candidate


@pytest.fixture
def release_app(settings: Settings, engine: object) -> FastAPI:
    from app.main import create_app

    _ = engine
    application = create_app(settings, run_bootstrap=False)
    if not any(
        getattr(route, "path", "") == "/api/release-candidates/{candidate_id}/finalize"
        for route in application.routes
    ):
        application.include_router(release_router, prefix=settings.api_prefix)
    application.dependency_overrides[get_settings] = lambda: settings
    return application


@pytest.fixture
def release_client(release_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(release_app) as client:
        yield client


def _bypass_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive_service, "_assert_release_gate", lambda *_args, **_kwargs: None)


def _archive_path(settings: Settings, candidate: ReleaseCandidate) -> Path:
    return settings.project_dir(candidate.project_id) / "releases" / "RC-0001.zip"


def _headers(client: TestClient, user: User) -> dict[str, str]:
    token = login(client, user.email, TEST_PASSWORD)
    return {"Authorization": f"Bearer {token}"}


def test_finalization_persists_a_sibling_zip_and_survives_restart(
    db: Session,
    approved_release: ReleaseCandidate,
    seeded: dict[str, User],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bypass_gate(monkeypatch)

    archive = finalize_release_archive(
        db,
        candidate=approved_release,
        finalized_by=seeded["reviewer"],
        settings=settings,
    )
    db.commit()

    path = _archive_path(settings, approved_release)
    assert path.is_file()
    assert path.parent == settings.project_dir(approved_release.project_id) / "releases"
    assert not path.is_relative_to(
        settings.project_dir(approved_release.project_id) / approved_release.package_path
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == archive.sha256
    assert path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    assert approved_release.status is ReleaseCandidateStatus.RELEASED
    assert approved_release.package_sha256 != archive.sha256

    restarted = db_session.get_session_factory()()
    try:
        reloaded = restarted.get(ReleaseCandidate, approved_release.id)
        assert reloaded is not None
        same = finalize_release_archive(
            restarted,
            candidate=reloaded,
            finalized_by=seeded["reviewer"],
            settings=settings,
        )
        restarted.commit()
        assert same.id == archive.id
        assert restarted.query(ReleaseArchive).count() == 1
    finally:
        restarted.close()


def test_retry_adopts_an_exact_orphan_zip_after_database_rollback(
    db: Session,
    approved_release: ReleaseCandidate,
    seeded: dict[str, User],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bypass_gate(monkeypatch)
    first = finalize_release_archive(
        db,
        candidate=approved_release,
        finalized_by=seeded["reviewer"],
        settings=settings,
    )
    db.commit()
    expected_sha = first.sha256
    archive_id = first.id

    db.delete(first)
    approved_release.status = ReleaseCandidateStatus.APPROVED
    approved_release.released_at = None
    db.commit()

    adopted = finalize_release_archive(
        db,
        candidate=approved_release,
        finalized_by=seeded["reviewer"],
        settings=settings,
    )
    db.commit()

    assert adopted.id != archive_id
    assert adopted.sha256 == expected_sha
    assert _archive_path(settings, approved_release).is_file()
    assert approved_release.status is ReleaseCandidateStatus.RELEASED


def test_finalization_never_overwrites_a_mismatched_orphan(
    db: Session,
    approved_release: ReleaseCandidate,
    seeded: dict[str, User],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bypass_gate(monkeypatch)
    target = _archive_path(settings, approved_release)
    target.write_bytes(b"untrusted pre-existing bytes")

    with pytest.raises(ReleaseArchiveRefused) as caught:
        finalize_release_archive(
            db,
            candidate=approved_release,
            finalized_by=seeded["reviewer"],
            settings=settings,
        )

    assert caught.value.reason == "archive_existing_mismatch"
    assert target.read_bytes() == b"untrusted pre-existing bytes"
    assert db.query(ReleaseArchive).count() == 0


def test_finalization_refuses_when_the_release_gate_blocks(
    db: Session,
    approved_release: ReleaseCandidate,
    seeded: dict[str, User],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = SimpleNamespace(
        gate_key=ApprovalGateKey.RELEASE,
        status=GateStatus.AWAITING_DECISION,
    )
    monkeypatch.setattr(
        archive_service.gates,
        "gate_blocking",
        lambda *_args, **_kwargs: blocker,
    )

    with pytest.raises(ReleaseArchiveRefused) as caught:
        finalize_release_archive(
            db,
            candidate=approved_release,
            finalized_by=seeded["reviewer"],
            settings=settings,
        )

    assert caught.value.reason == "release_gate_blocked"
    assert not _archive_path(settings, approved_release).exists()


def test_enabled_release_gate_requires_a_named_current_commented_decision(
    db: Session,
    project: Project,
    approved_release: ReleaseCandidate,
    seeded: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_service.gates.ensure_gates(db, project=project)
    monkeypatch.setattr(
        archive_service.gates,
        "gate_blocking",
        lambda *_args, **_kwargs: None,
    )
    gate = db.execute(
        select(ApprovalGate).where(
            ApprovalGate.project_id == project.id,
            ApprovalGate.gate_key == ApprovalGateKey.RELEASE,
        )
    ).scalar_one()
    gate.is_enabled = True
    gate.status = GateStatus.PASSED
    gate.decided_by_id = seeded["reviewer"].id
    gate.decided_at = utcnow()
    gate.decided_revision_number = project.current_revision_number
    gate.decision_comment = ""
    db.flush()

    with pytest.raises(ReleaseArchiveRefused) as caught:
        archive_service._assert_release_gate(
            db,
            project=project,
            candidate=approved_release,
        )
    assert caught.value.reason == "release_gate_incomplete"

    gate.decision_comment = "Reviewed the frozen manifest and customer delivery scope."
    archive_service._assert_release_gate(
        db,
        project=project,
        candidate=approved_release,
    )


def test_disabled_release_gate_requires_a_stated_reason(
    db: Session,
    project: Project,
    approved_release: ReleaseCandidate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_service.gates.ensure_gates(db, project=project)
    monkeypatch.setattr(
        archive_service.gates,
        "gate_blocking",
        lambda *_args, **_kwargs: None,
    )
    gate = db.execute(
        select(ApprovalGate).where(
            ApprovalGate.project_id == project.id,
            ApprovalGate.gate_key == ApprovalGateKey.RELEASE,
        )
    ).scalar_one()
    gate.is_enabled = False
    gate.disabled_reason = ""
    db.flush()

    with pytest.raises(ReleaseArchiveRefused) as caught:
        archive_service._assert_release_gate(
            db,
            project=project,
            candidate=approved_release,
        )
    assert caught.value.reason == "release_gate_unstated_skip"

    gate.disabled_reason = "Customer contract delegates final delivery to the site integrator."
    archive_service._assert_release_gate(
        db,
        project=project,
        candidate=approved_release,
    )


def test_download_preverification_detects_archive_tampering(
    db: Session,
    approved_release: ReleaseCandidate,
    seeded: dict[str, User],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bypass_gate(monkeypatch)
    archive = finalize_release_archive(
        db,
        candidate=approved_release,
        finalized_by=seeded["reviewer"],
        settings=settings,
    )
    db.commit()
    path = _archive_path(settings, approved_release)
    path.chmod(path.stat().st_mode | stat.S_IWUSR)
    path.write_bytes(b"tampered after finalization")

    with pytest.raises(ReleaseArchiveRefused) as caught:
        open_verified_archive(archive, settings=settings)

    assert caught.value.reason == "archive_bytes_mismatch"


def test_release_http_enforces_roles_and_streams_verified_bytes(
    release_client: TestClient,
    db: Session,
    approved_release: ReleaseCandidate,
    seeded: dict[str, User],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bypass_gate(monkeypatch)
    candidate_url = f"/api/release-candidates/{approved_release.id}"
    finalize_url = f"{candidate_url}/finalize"
    download_url = f"{candidate_url}/download"

    outsider = release_client.get(
        candidate_url,
        headers=_headers(release_client, seeded["outsider"]),
    )
    assert outsider.status_code == 403

    engineer_headers = _headers(release_client, seeded["engineer"])
    engineer_finalize = release_client.post(
        finalize_url,
        headers=engineer_headers,
        json={"confirm": True},
    )
    assert engineer_finalize.status_code == 403

    reviewer_headers = _headers(release_client, seeded["reviewer"])
    missing_confirmation = release_client.post(
        finalize_url,
        headers=reviewer_headers,
        json={"confirm": False},
    )
    assert missing_confirmation.status_code == 422

    finalized = release_client.post(
        finalize_url,
        headers=reviewer_headers,
        json={"confirm": True},
    )
    assert finalized.status_code == 200, finalized.text
    metadata = finalized.json()
    assert metadata["relative_path"] == "releases/RC-0001.zip"

    download = release_client.get(download_url, headers=engineer_headers)
    assert download.status_code == 200, download.text
    assert download.headers["content-type"] == "application/zip"
    assert int(download.headers["content-length"]) == len(download.content)
    assert download.headers["x-content-sha256"] == hashlib.sha256(download.content).hexdigest()
    assert 'filename="' in download.headers["content-disposition"]
    assert "filename*=UTF-8''" in download.headers["content-disposition"]


def test_release_http_create_and_generate_require_engineering_write_access(
    release_client: TestClient,
    approved_release: ReleaseCandidate,
    project: Project,
    seeded: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load_candidate(request_db: Session, **_kwargs: object) -> ReleaseCandidate:
        candidate = request_db.get(ReleaseCandidate, approved_release.id)
        assert candidate is not None
        return candidate

    monkeypatch.setattr(
        release_routes.release_service,
        "create_release_candidate",
        load_candidate,
    )
    monkeypatch.setattr(
        release_routes.release_service,
        "generate_artifacts",
        load_candidate,
    )
    create_url = f"/api/projects/{project.id}/release-candidates"
    generate_url = f"/api/release-candidates/{approved_release.id}/artifacts"

    reviewer_headers = _headers(release_client, seeded["reviewer"])
    reviewer_create = release_client.post(create_url, headers=reviewer_headers, json={})
    assert reviewer_create.status_code == 403
    reviewer_generate = release_client.post(generate_url, headers=reviewer_headers)
    assert reviewer_generate.status_code == 403

    engineer_headers = _headers(release_client, seeded["engineer"])
    engineer_create = release_client.post(create_url, headers=engineer_headers, json={})
    assert engineer_create.status_code == 201, engineer_create.text
    engineer_generate = release_client.post(generate_url, headers=engineer_headers)
    assert engineer_generate.status_code == 200, engineer_generate.text

    outsider_create = release_client.post(
        create_url,
        headers=_headers(release_client, seeded["outsider"]),
        json={},
    )
    assert outsider_create.status_code == 403
