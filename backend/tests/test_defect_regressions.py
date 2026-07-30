"""Regressions for defects an adversarial review of Milestone 1 confirmed.

Each of these shipped past a fully green test suite once, so each is worth a test
that would have caught it. Grouped here rather than scattered because the reason
they matter is the same: every one of them made the product quietly do the wrong
thing rather than fail loudly.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import AuditAction, FileKind, FileStatus, ProjectStatus
from app.models.audit import AuditEntry
from app.models.file import UploadedFile
from app.models.project import Project
from tests.conftest import TEST_PASSWORD, AuthedClient

# ---------------------------------------------------------------------------
# The seeded administrator must actually be able to sign in.
# ---------------------------------------------------------------------------


def test_bootstrap_admin_can_sign_in_over_http(
    client: TestClient, db: Session, settings: Settings
) -> None:
    """A fresh install with an unusable administrator has no way in at all.

    The seeded address used to be ``admin@esco.local``, and ``LoginRequest.email``
    is an ``EmailStr`` — ``email-validator`` refuses reserved special-use domains,
    so the one account the product provisions for itself was rejected before any
    credential was checked.
    """
    from app.core.services import users as user_service

    user_service.bootstrap(db, settings)
    db.commit()

    response = client.post(
        "/api/auth/login",
        json={
            "email": settings.bootstrap_admin_email,
            "password": settings.bootstrap_admin_password.get_secret_value(),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"]["email"] == settings.bootstrap_admin_email


def test_settings_refuse_a_bootstrap_email_the_login_endpoint_would_reject() -> None:
    """Fail at startup rather than shipping an account nobody can use."""
    with pytest.raises(ValueError, match="could never sign in"):
        Settings(environment="test", bootstrap_admin_email="admin@esco.local")


@pytest.mark.parametrize("domain", ["esco.local", "esco.test", "esco.invalid", "localhost"])
def test_reserved_domains_are_all_refused_for_the_seeded_admin(domain: str) -> None:
    with pytest.raises(ValueError, match="could never sign in"):
        Settings(environment="test", bootstrap_admin_email=f"admin@{domain}")


# ---------------------------------------------------------------------------
# A refused login must survive the request.
# ---------------------------------------------------------------------------


def test_failed_login_is_persisted_to_the_audit_trail(
    client: TestClient, db: Session, seeded: dict[str, object]
) -> None:
    """The record of failed sign-in attempts is the point of auditing them.

    ``authenticate`` adds the entry and then raises; the route used to commit only
    on the success path, so every failure was rolled back when the session closed.
    """
    response = client.post(
        "/api/auth/login",
        json={"email": "engineer@test.internal", "password": "wrong-password-entirely"},
    )
    assert response.status_code == 401

    entries = list(
        db.execute(
            select(AuditEntry).where(AuditEntry.action == AuditAction.USER_LOGIN_FAILED)
        ).scalars()
    )
    assert len(entries) == 1
    assert entries[0].details["reason"] == "bad_password"


def test_failed_login_for_an_unknown_account_is_also_persisted(
    client: TestClient, db: Session, seeded: dict[str, object]
) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "nobody@test.internal", "password": "irrelevant-password"},
    )
    assert response.status_code == 401

    entries = list(
        db.execute(
            select(AuditEntry).where(AuditEntry.action == AuditAction.USER_LOGIN_FAILED)
        ).scalars()
    )
    assert len(entries) == 1
    assert entries[0].details["reason"] == "unknown_account"
    assert entries[0].details["email"] == "nobody@test.internal"


def test_no_audit_entry_ever_records_a_plaintext_password(
    client: TestClient, db: Session, seeded: dict[str, object]
) -> None:
    client.post(
        "/api/auth/login",
        json={"email": "engineer@test.internal", "password": TEST_PASSWORD},
    )
    client.post(
        "/api/auth/login",
        json={"email": "engineer@test.internal", "password": "a-distinctive-wrong-secret"},
    )

    blob = "\n".join(
        f"{entry.summary} {entry.details}" for entry in db.execute(select(AuditEntry)).scalars()
    )
    assert TEST_PASSWORD not in blob
    assert "a-distinctive-wrong-secret" not in blob


# ---------------------------------------------------------------------------
# An archived project is genuinely read-only.
# ---------------------------------------------------------------------------


def test_archived_project_rejects_an_upload(
    engineer_client: AuthedClient, project: Project, db: Session
) -> None:
    """``Project.is_archived`` gates this, and it used to be False for every row
    loaded from the database, leaving archived projects silently writable."""
    archived = engineer_client.post(f"/api/projects/{project.id}/archive")
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == ProjectStatus.ARCHIVED.value

    response = engineer_client.post(
        f"/api/projects/{project.id}/files",
        files={"files": ("IO List.csv", b"Tag,Description\nESTOP_OK,E-stop\n", "text/csv")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"
    assert db.execute(select(UploadedFile)).first() is None


def test_archive_endpoint_succeeds_and_is_idempotent_only_once(
    engineer_client: AuthedClient, project: Project
) -> None:
    """Archiving read ``previous.value`` on a loaded status and 500'd every time."""
    first = engineer_client.post(f"/api/projects/{project.id}/archive")
    assert first.status_code == 200, first.text

    second = engineer_client.post(f"/api/projects/{project.id}/archive")
    assert second.status_code == 409, second.text
    assert "already archived" in second.json()["error"]["message"]


def test_patch_project_applies_a_change(engineer_client: AuthedClient, project: Project) -> None:
    """Any PATCH that actually changed something used to 500 on ``status.value``."""
    response = engineer_client.patch(
        f"/api/projects/{project.id}",
        json={"name": "Synthetic Conveyor Line 3 (Rev B)"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Synthetic Conveyor Line 3 (Rev B)"


# ---------------------------------------------------------------------------
# Deleting a file must not leave a phantom.
# ---------------------------------------------------------------------------


def test_delete_file_removes_bytes_and_marks_the_row_deleted(
    engineer_client: AuthedClient, project: Project, db: Session, settings: Settings
) -> None:
    """This endpoint used to 500 on ``file_kind.value`` *after* unlinking the blob
    and *before* the route committed, leaving a row that claimed the file was
    still there and no bytes behind it."""
    upload = engineer_client.post(
        f"/api/projects/{project.id}/files",
        files={"files": ("IO List.csv", b"Tag,Description\nESTOP_OK,E-stop\n", "text/csv")},
    )
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["results"][0]["file"]["id"]

    stored = db.execute(select(UploadedFile)).scalar_one()
    on_disk: Path = settings.project_dir(project.id) / stored.relative_path
    assert on_disk.is_file()

    response = engineer_client.delete(f"/api/files/{file_id}")
    assert response.status_code == 200, response.text

    assert not on_disk.exists()
    db.expire_all()
    row = db.execute(select(UploadedFile)).scalar_one()
    assert row.status is FileStatus.DELETED
    assert row.deleted_at is not None

    # The row survives so a citation resolves to a tombstone, not to nothing.
    assert engineer_client.get(f"/api/files/{file_id}").status_code == 200
    assert engineer_client.get(f"/api/files/{file_id}/content").status_code == 404


# ---------------------------------------------------------------------------
# A mislabelled archive is still an archive.
# ---------------------------------------------------------------------------


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("documents/IO_List.csv", "Tag,Description\nESTOP_OK,E-stop\n")
    return buffer.getvalue()


@pytest.mark.parametrize("filename", ["package.csv", "package.pdf", "package.txt", "package.xml"])
def test_archive_bytes_under_a_non_container_extension_are_treated_as_an_archive(
    filename: str,
) -> None:
    """Classifying PK bytes by extension routed a real archive to a text parser and
    silently omitted everything inside it from the review."""
    from app.security.file_validation import validate_upload

    data = _zip_bytes()
    result = validate_upload(
        filename=filename,
        head=data[:8192],
        size_bytes=len(data),
        max_bytes=10_000_000,
    )
    assert result.file_kind is FileKind.ZIP
    assert result.warnings, "a mislabelled archive must be reported, not silently re-routed"
    assert "zip" in result.warnings[0].lower()


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("book.xlsx", FileKind.XLSX),
        ("book.xlsm", FileKind.XLSX),
        ("doc.docx", FileKind.DOCX),
        ("pkg.zip", FileKind.ZIP),
    ],
)
def test_legitimate_container_extensions_are_unchanged_and_warning_free(
    filename: str, expected: FileKind
) -> None:
    """The extension still disambiguates *among* container kinds, which is the
    one thing it is entitled to do for PK bytes."""
    from app.security.file_validation import validate_upload

    data = _zip_bytes()
    result = validate_upload(
        filename=filename,
        head=data[:8192],
        size_bytes=len(data),
        max_bytes=10_000_000,
    )
    assert result.file_kind is expected
    assert result.warnings == []


def test_mislabelled_archive_is_expanded_on_upload(
    engineer_client: AuthedClient, project: Project, db: Session
) -> None:
    """End to end: the members must reach the review, not vanish."""
    response = engineer_client.post(
        f"/api/projects/{project.id}/files",
        files={"files": ("package.csv", _zip_bytes(), "text/csv")},
    )
    assert response.status_code == 201, response.text

    result = response.json()["results"][0]
    assert result["accepted"] is True
    assert result["file"]["file_kind"] == FileKind.ZIP.value
    assert [child["original_filename"] for child in result["extracted"]] == ["IO_List.csv"]

    children = list(
        db.execute(select(UploadedFile).where(UploadedFile.parent_file_id.isnot(None))).scalars()
    )
    assert len(children) == 1
    assert children[0].file_kind is FileKind.CSV


# ---------------------------------------------------------------------------
# A rejected archive leaves nothing behind.
# ---------------------------------------------------------------------------


def test_rejected_archive_leaves_no_scratch_files_behind(
    engineer_client: AuthedClient, project: Project, db: Session, settings: Settings
) -> None:
    """A traversing archive is refused from its central directory, and the scratch
    tree is cleaned up unconditionally — otherwise unreferenced bytes accumulate in
    derived/ where nothing accounts for them against the project quota."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("documents/IO_List.csv", "Tag\nESTOP_OK\n")
        archive.writestr("../../escape.txt", "owned")

    response = engineer_client.post(
        f"/api/projects/{project.id}/files",
        files={"files": ("hostile.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 201, response.text

    result = response.json()["results"][0]
    assert result["extracted"] == []
    assert "not expanded" in result["rejection_reason"]

    derived = settings.project_dir(project.id) / "derived"
    assert list(derived.iterdir()) == [], "scratch extraction tree was left behind"

    root = settings.data_dir
    assert not (root / "escape.txt").exists()
    assert not (settings.project_dir(project.id) / "escape.txt").exists()

    archive_row = db.execute(
        select(UploadedFile).where(UploadedFile.file_kind == FileKind.ZIP)
    ).scalar_one()
    assert archive_row.status is FileStatus.REJECTED
