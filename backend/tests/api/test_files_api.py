"""File upload, listing and download over HTTP.

Intake is the platform's boundary with untrusted input, so the tests here are
about what it stores, what it refuses, and what it tells the uploader in either
case. Every payload is built in memory: there are no committed fixtures, and
nothing resembling real customer data.

The authenticated clients are built by minting a token rather than by calling
``POST /api/auth/login``, because that endpoint currently rejects every address
in a ``.local`` domain — which is what both the fixtures and the application's
own seeded accounts use. Delete these three fixtures once that is fixed and the
shared ones in ``conftest`` take over unchanged.
"""

from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import AuditAction, DocumentRole, EventType, FileKind, FileStatus
from app.models.audit import AuditEntry
from app.models.event import Event
from app.models.file import UploadedFile
from app.models.project import Project
from app.models.user import User
from app.security.tokens import create_access_token
from tests.conftest import AuthedClient

IO_LIST_CSV = (
    b"Tag,Description,Type\nSTART_PB,Line 3 start pushbutton,BOOL\nESTOP_1,East guard estop,BOOL\n"
)

INTERLOCKS_CSV = (
    b"Interlock,Cause,Effect\n"
    b"IL-01,Guard door open,Stop conveyor 3\n"
    b"IL-02,Motor overload,Stop conveyor 3\n"
)

LOGIC_L5X = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<RSLogix5000Content SchemaRevision="1.0" TargetName="SyntheticLine3">\n'
    b'  <Controller Name="SyntheticLine3" ProcessorType="Synthetic">\n'
    b'    <Programs><Program Name="MainProgram"/></Programs>\n'
    b"  </Controller>\n"
    b"</RSLogix5000Content>\n"
)

#: A DOS/PE header, which is what makes this content an executable regardless of
#: the name it arrives under. The tail is filler so the file is not merely empty.
WINDOWS_PE = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + b"PE\x00\x00" * 16


def _bearer_client(client: TestClient, user: User, settings: Settings) -> AuthedClient:
    token, _ = create_access_token(user_id=user.id, email=user.email, settings=settings)
    return AuthedClient(client, token, user)


@pytest.fixture
def engineer_client(
    client: TestClient, seeded: dict[str, User], settings: Settings
) -> AuthedClient:
    return _bearer_client(client, seeded["engineer"], settings)


@pytest.fixture
def reviewer_client(
    client: TestClient, seeded: dict[str, User], settings: Settings
) -> AuthedClient:
    return _bearer_client(client, seeded["reviewer"], settings)


@pytest.fixture
def outsider_client(
    client: TestClient, seeded: dict[str, User], settings: Settings
) -> AuthedClient:
    """A valid account with no membership of the test project."""
    return _bearer_client(client, seeded["outsider"], settings)


def _zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    """Build a ZIP in memory, preserving the given member names verbatim."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            archive.writestr(name, payload)
    return buffer.getvalue()


def _post_files(
    client: AuthedClient,
    project_id: uuid.UUID,
    files: list[tuple[str, tuple[str, bytes, str]]],
    **form: str,
) -> Any:
    return client.post(
        f"/api/projects/{project_id}/files",
        files=files,
        data=form or None,
    )


def _upload_one(
    client: AuthedClient,
    project_id: uuid.UUID,
    filename: str,
    payload: bytes,
    media_type: str = "text/csv",
    **form: str,
) -> dict[str, Any]:
    """Upload a single file, require acceptance, and return its JSON body."""
    response = _post_files(client, project_id, [("files", (filename, payload, media_type))], **form)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["accepted_count"] == 1, body
    accepted: dict[str, Any] = body["results"][0]["file"]
    return accepted


def _rows_by_name(db: Session, project_id: uuid.UUID) -> dict[str, UploadedFile]:
    """Persisted file rows, keyed by original filename.

    The rollback ends this session's read snapshot so rows committed by the API's
    own session are visible.
    """
    db.rollback()
    rows = db.execute(select(UploadedFile).where(UploadedFile.project_id == project_id)).scalars()
    return {row.original_filename: row for row in rows}


def _audit_entries(db: Session, project_id: uuid.UUID) -> list[AuditEntry]:
    db.rollback()
    return list(
        db.execute(
            select(AuditEntry)
            .where(AuditEntry.project_id == project_id)
            .order_by(AuditEntry.created_at)
        ).scalars()
    )


def _event_types(db: Session, project_id: uuid.UUID) -> list[str]:
    db.rollback()
    rows = db.execute(
        select(Event).where(Event.project_id == project_id).order_by(Event.seq)
    ).scalars()
    return [str(row.event_type) for row in rows]


def _stored_path(settings: Settings, project_id: uuid.UUID, relative_path: str) -> Path:
    return settings.project_dir(project_id).joinpath(*relative_path.split("/"))


# -- upload -----------------------------------------------------------------


def test_csv_upload_is_accepted_hashed_and_classified(
    engineer_client: AuthedClient, project: Project
) -> None:
    accepted = _upload_one(engineer_client, project.id, "io_list.csv", IO_LIST_CSV)

    assert accepted["file_kind"] == FileKind.CSV
    assert accepted["sha256"] == hashlib.sha256(IO_LIST_CSV).hexdigest()
    assert accepted["size_bytes"] == len(IO_LIST_CSV)
    assert accepted["status"] == FileStatus.UPLOADED
    assert accepted["document_role"] == DocumentRole.IO_LIST
    assert accepted["role_declared_by_user"] is False
    assert accepted["parent_file_id"] is None


def test_uploaded_bytes_are_stored_inside_the_project_directory(
    engineer_client: AuthedClient, project: Project, db: Session, settings: Settings
) -> None:
    """Originals are what every later citation resolves against, so the bytes
    must land intact and under the project's own directory."""
    _upload_one(engineer_client, project.id, "io_list.csv", IO_LIST_CSV)

    row = _rows_by_name(db, project.id)["io_list.csv"]
    stored = _stored_path(settings, project.id, row.relative_path)

    assert stored.is_file()
    assert stored.read_bytes() == IO_LIST_CSV
    assert stored.parent == settings.project_dir(project.id) / "originals"


def test_uploaded_file_appears_in_the_project_listing(
    engineer_client: AuthedClient, project: Project
) -> None:
    accepted = _upload_one(engineer_client, project.id, "io_list.csv", IO_LIST_CSV)

    listing = engineer_client.get(f"/api/projects/{project.id}/files")
    assert listing.status_code == 200
    assert [row["id"] for row in listing.json()] == [accepted["id"]]


def test_declared_document_role_beats_the_inferred_one(
    engineer_client: AuthedClient, project: Project
) -> None:
    """The filename is only a hint; what the uploader declares is the record."""
    accepted = _upload_one(
        engineer_client,
        project.id,
        "io_list.csv",
        IO_LIST_CSV,
        document_role=DocumentRole.TESTING_NOTES.value,
    )

    assert accepted["document_role"] == DocumentRole.TESTING_NOTES
    assert accepted["role_declared_by_user"] is True


def test_one_rejected_file_does_not_discard_the_others(
    engineer_client: AuthedClient, project: Project, db: Session
) -> None:
    """Losing nine good documents because the tenth was an executable would be a
    product failure, so both halves of the outcome are asserted."""
    response = _post_files(
        engineer_client,
        project.id,
        [
            ("files", ("io_list.csv", IO_LIST_CSV, "text/csv")),
            ("files", ("notes.pdf", WINDOWS_PE, "application/pdf")),
            ("files", ("interlocks.csv", INTERLOCKS_CSV, "text/csv")),
        ],
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["accepted_count"] == 2
    assert body["rejected_count"] == 1

    results = body["results"]
    assert [item["original_filename"] for item in results] == [
        "io_list.csv",
        "notes.pdf",
        "interlocks.csv",
    ]
    assert [item["accepted"] for item in results] == [True, False, True]

    refused = results[1]
    assert refused["file"] is None
    assert refused["rejection_code"] == "file_rejected"
    assert "Windows executable" in refused["rejection_reason"]

    rows = _rows_by_name(db, project.id)
    assert sorted(rows) == ["interlocks.csv", "io_list.csv"]
    assert rows["io_list.csv"].sha256 == hashlib.sha256(IO_LIST_CSV).hexdigest()
    assert rows["interlocks.csv"].sha256 == hashlib.sha256(INTERLOCKS_CSV).hexdigest()


# -- archives ---------------------------------------------------------------


def test_zip_members_are_recorded_as_child_files(
    engineer_client: AuthedClient, project: Project, db: Session
) -> None:
    """A member has to be a file in its own right, with provenance back to the
    archive, because findings cite it directly."""
    package = _zip_bytes([("exports/logic.l5x", LOGIC_L5X), ("data/io_list.csv", IO_LIST_CSV)])

    response = _post_files(
        engineer_client, project.id, [("files", ("package.zip", package, "application/zip"))]
    )
    assert response.status_code == 201, response.text
    result = response.json()["results"][0]

    archive = result["file"]
    assert archive["file_kind"] == FileKind.ZIP
    assert archive["status"] == FileStatus.PARSED

    extracted = {item["original_filename"]: item for item in result["extracted"]}
    assert sorted(extracted) == ["io_list.csv", "logic.l5x"]
    assert extracted["logic.l5x"]["file_kind"] == FileKind.L5X
    assert extracted["logic.l5x"]["document_role"] == DocumentRole.CONTROL_LOGIC
    assert extracted["io_list.csv"]["file_kind"] == FileKind.CSV

    rows = _rows_by_name(db, project.id)
    assert sorted(rows) == ["io_list.csv", "logic.l5x", "package.zip"]
    archive_id = rows["package.zip"].id
    assert rows["logic.l5x"].parent_file_id == archive_id
    assert rows["io_list.csv"].parent_file_id == archive_id
    assert rows["logic.l5x"].archive_member_path == "exports/logic.l5x"
    assert rows["io_list.csv"].archive_member_path == "data/io_list.csv"
    assert rows["io_list.csv"].sha256 == hashlib.sha256(IO_LIST_CSV).hexdigest()


def test_zip_member_that_escapes_the_project_directory_is_refused(
    engineer_client: AuthedClient, project: Project, db: Session, tmp_path: Path
) -> None:
    """A traversing member disqualifies the whole archive: it is hostile input,
    not a naming quirk to route around."""
    package = _zip_bytes([("docs/notes.csv", INTERLOCKS_CSV), ("../escape.txt", b"pwned\n")])

    response = _post_files(
        engineer_client, project.id, [("files", ("package.zip", package, "application/zip"))]
    )
    assert response.status_code == 201, response.text
    result = response.json()["results"][0]

    assert result["extracted"] == []
    assert result["file"]["status"] == FileStatus.REJECTED
    assert "escape.txt" in result["rejection_reason"]
    assert "outside the project directory" in result["rejection_reason"]

    rows = _rows_by_name(db, project.id)
    assert list(rows) == ["package.zip"]
    assert rows["package.zip"].status == FileStatus.REJECTED
    assert "escape.txt" in rows["package.zip"].status_detail

    assert list(tmp_path.rglob("escape.txt")) == []


# -- permissions ------------------------------------------------------------


def test_reviewer_cannot_upload_files(
    reviewer_client: AuthedClient, project: Project, db: Session
) -> None:
    """A review role reads and judges; it must not change what is under review."""
    response = _post_files(
        reviewer_client, project.id, [("files", ("io_list.csv", IO_LIST_CSV, "text/csv"))]
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert _rows_by_name(db, project.id) == {}


def test_outsider_cannot_upload_or_list_files(
    outsider_client: AuthedClient, project: Project
) -> None:
    """Holding the engineer role globally grants nothing on someone else's job."""
    upload = _post_files(
        outsider_client, project.id, [("files", ("io_list.csv", IO_LIST_CSV, "text/csv"))]
    )
    assert upload.status_code == 403

    listing = outsider_client.get(f"/api/projects/{project.id}/files")
    assert listing.status_code == 403
    assert listing.json()["error"]["code"] == "permission_denied"


# -- download ---------------------------------------------------------------


def test_download_returns_the_exact_stored_bytes_as_an_attachment(
    engineer_client: AuthedClient, project: Project
) -> None:
    accepted = _upload_one(engineer_client, project.id, "io_list.csv", IO_LIST_CSV)

    response = engineer_client.get(f"/api/files/{accepted['id']}/content")

    assert response.status_code == 200
    assert response.content == IO_LIST_CSV
    assert response.headers["content-disposition"] == 'attachment; filename="io_list.csv"'
    assert response.headers["x-content-sha256"] == accepted["sha256"]


def test_download_is_never_rendered_as_active_content(
    engineer_client: AuthedClient, project: Project
) -> None:
    """Without nosniff a browser could sniff an uploaded document into script."""
    accepted = _upload_one(engineer_client, project.id, "io_list.csv", IO_LIST_CSV)

    response = engineer_client.get(f"/api/files/{accepted['id']}/content")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "application/octet-stream"


def test_content_of_a_deleted_file_is_no_longer_served(
    engineer_client: AuthedClient, project: Project, db: Session
) -> None:
    """The tombstone alone must block the download: a file an engineer deleted
    cannot keep serving its bytes to anyone holding the id.

    The tombstone is written directly rather than through ``DELETE /api/files``
    so that this test covers only the download endpoint's own check.
    """
    accepted = _upload_one(engineer_client, project.id, "io_list.csv", IO_LIST_CSV)
    row = _rows_by_name(db, project.id)["io_list.csv"]
    row.status = FileStatus.DELETED
    row.deleted_at = utcnow()
    db.commit()

    response = engineer_client.get(f"/api/files/{accepted['id']}/content")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# -- deletion ---------------------------------------------------------------


def test_reviewer_cannot_delete_a_file(
    engineer_client: AuthedClient, reviewer_client: AuthedClient, project: Project, db: Session
) -> None:
    accepted = _upload_one(engineer_client, project.id, "io_list.csv", IO_LIST_CSV)

    response = reviewer_client.delete(f"/api/files/{accepted['id']}")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert _rows_by_name(db, project.id)["io_list.csv"].deleted_at is None


# -- audit and events -------------------------------------------------------


def test_accepted_upload_is_audited_and_broadcast(
    engineer_client: AuthedClient, project: Project, db: Session, seeded: dict[str, User]
) -> None:
    accepted = _upload_one(engineer_client, project.id, "io_list.csv", IO_LIST_CSV)

    uploads = [
        entry
        for entry in _audit_entries(db, project.id)
        if entry.action == AuditAction.FILE_UPLOADED
    ]
    assert len(uploads) == 1
    assert uploads[0].target_id == uuid.UUID(accepted["id"])
    assert uploads[0].actor_user_id == seeded["engineer"].id
    assert "io_list.csv" in uploads[0].summary

    assert _event_types(db, project.id).count(EventType.FILE_UPLOADED) == 1


def test_rejected_upload_is_audited_without_a_file_uploaded_event(
    engineer_client: AuthedClient, project: Project, db: Session
) -> None:
    """A refusal has to be recorded, and must never look like a successful
    upload to anything watching the event stream."""
    response = _post_files(
        engineer_client, project.id, [("files", ("notes.pdf", WINDOWS_PE, "application/pdf"))]
    )
    assert response.status_code == 201, response.text
    assert response.json()["accepted_count"] == 0

    actions = [entry.action for entry in _audit_entries(db, project.id)]
    assert actions.count(AuditAction.FILE_REJECTED) == 1
    assert actions.count(AuditAction.FILE_UPLOADED) == 0

    assert EventType.FILE_UPLOADED not in _event_types(db, project.id)
