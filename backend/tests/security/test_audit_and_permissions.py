"""Audit trail writing and the authorisation rule set.

Two things are being pinned down here. First, that an audit entry lands if and
only if the operation it describes lands — an entry for a rolled-back change is a
false record, and a change without its entry is an unattributable one. Second,
the full permission truth table, because these predicates are the only thing
standing between a global ``engineer`` role and someone else's project.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.enums import AuditAction, ProjectRole, RoleName
from app.core.errors import PermissionDeniedError
from app.core.logging import clear_request_context, set_request_context
from app.models.audit import AuditEntry
from app.models.project import Project, ProjectMember
from app.models.user import User
from app.security import audit
from app.security.permissions import (
    READ_ROLES,
    REVIEW_ROLES,
    WRITE_ROLES,
    can_decide_approval,
    can_read_project,
    can_review_project,
    can_write_project,
    project_role_for,
    require_global_role,
    require_project_access,
)

#: Fixed (not random) so these tests stay deterministic, and distinct so the
#: audit rows written here never collide with the ones the fixtures write.
TARGET_ID = uuid.UUID("0d3f6b1a-0000-4000-8000-0000000000a1")
UNKNOWN_PROJECT_ID = uuid.UUID("0d3f6b1a-0000-4000-8000-0000000000b2")


@pytest.fixture(autouse=True)
def _isolated_request_context() -> Iterator[None]:
    """The request id lives in a ContextVar, which would otherwise outlive the test."""
    clear_request_context()
    yield
    clear_request_context()


def _recorded(engine: Engine) -> list[AuditEntry]:
    """Audit rows for ``TARGET_ID``, read through a second session.

    A separate session is used so the assertions describe what is in the
    database, not what is still sitting in the writing session's identity map.
    """
    with Session(engine) as session:
        return list(session.scalars(select(AuditEntry).where(AuditEntry.target_id == TARGET_ID)))


# ---------------------------------------------------------------------------
# audit.record — transaction boundary
# ---------------------------------------------------------------------------


def test_record_leaves_no_row_when_the_caller_rolls_back(
    db: Session, engine: Engine, seeded: dict[str, User]
) -> None:
    """An operation that rolled back must not leave an entry claiming it happened."""
    audit.record(
        db,
        AuditAction.FILE_DELETED,
        actor_user_id=seeded["engineer"].id,
        target_type="uploaded_file",
        target_id=TARGET_ID,
        summary="Deleted a file during an operation that then failed",
    )
    db.flush()
    db.rollback()

    assert _recorded(engine) == []


def test_record_persists_when_the_caller_commits(
    db: Session, engine: Engine, seeded: dict[str, User], project: Project
) -> None:
    entry = audit.record(
        db,
        AuditAction.FILE_DELETED,
        actor_user_id=seeded["engineer"].id,
        project_id=project.id,
        target_type="uploaded_file",
        target_id=TARGET_ID,
        summary="Deleted conveyor-io.csv",
        details={"file_name": "conveyor-io.csv"},
        source_ip="10.0.0.7",
        user_agent="pytest-client",
    )
    db.commit()

    rows = _recorded(engine)
    assert len(rows) == 1
    stored = rows[0]
    assert stored.id == entry.id
    assert stored.action == AuditAction.FILE_DELETED
    assert stored.actor_user_id == seeded["engineer"].id
    assert stored.project_id == project.id
    assert stored.target_type == "uploaded_file"
    assert stored.summary == "Deleted conveyor-io.csv"
    assert stored.details == {"file_name": "conveyor-io.csv"}
    assert stored.source_ip == "10.0.0.7"
    assert stored.user_agent == "pytest-client"


def test_record_does_not_commit_the_callers_other_pending_work(
    db: Session, engine: Engine, seeded: dict[str, User], project: Project
) -> None:
    """record() must not be a hidden commit, or a half-finished operation would persist."""
    project.name = "Renamed But Never Committed"
    audit.record(
        db,
        AuditAction.PROJECT_UPDATED,
        actor_user_id=seeded["engineer"].id,
        project_id=project.id,
        target_type="project",
        target_id=TARGET_ID,
        summary="Renamed project",
    )
    db.rollback()

    assert _recorded(engine) == []
    with Session(engine) as reader:
        reloaded = reader.get(Project, project.id)
        assert reloaded is not None
        assert reloaded.name == "Synthetic Conveyor Line 3"


# ---------------------------------------------------------------------------
# audit.record — redaction and provenance
# ---------------------------------------------------------------------------


def test_credential_shaped_details_are_redacted_and_other_keys_survive(
    db: Session, engine: Engine, seeded: dict[str, User]
) -> None:
    """The audit trail is read far more widely than any credential store."""
    audit.record(
        db,
        AuditAction.USER_UPDATED,
        actor_user_id=seeded["admin"].id,
        target_type="user",
        target_id=TARGET_ID,
        summary="Updated an account",
        details={
            "password": "test-password-123456",
            "token": "header.body.signature",
            "api_key": "not-a-real-key",
            "secret": "not-a-real-secret",
            "email": "engineer@test.internal",
            "changed": ["display_name"],
            "attempt": 2,
        },
    )
    db.commit()

    assert _recorded(engine)[0].details == {
        "password": "[redacted]",
        "token": "[redacted]",
        "api_key": "[redacted]",
        "secret": "[redacted]",
        "email": "engineer@test.internal",
        "changed": ["display_name"],
        "attempt": 2,
    }


@pytest.mark.parametrize(
    "key",
    ["password", "password_hash", "token", "access_token", "secret", "api_key", "jwt"],
)
def test_each_credential_key_is_redacted_whatever_case_the_caller_used(
    db: Session, engine: Engine, key: str
) -> None:
    """Callers pass through whatever casing their payload had; 'Token' must not slip past."""
    audit.record(
        db,
        AuditAction.USER_UPDATED,
        target_type="user",
        target_id=TARGET_ID,
        details={key: "sensitive", key.upper(): "sensitive", key.title(): "sensitive"},
    )
    db.commit()

    stored = _recorded(engine)[0].details
    assert set(stored) == {key, key.upper(), key.title()}
    assert set(stored.values()) == {"[redacted]"}


def test_request_id_is_captured_from_the_logging_context(db: Session, engine: Engine) -> None:
    """Reconstructing an incident means joining audit rows to the request that caused them."""
    set_request_context(request_id="req-0123456789abcdef")

    audit.record(
        db,
        AuditAction.ANALYSIS_CANCELLED,
        target_type="project",
        target_id=TARGET_ID,
        summary="Cancelled the run",
    )
    db.commit()

    assert _recorded(engine)[0].request_id == "req-0123456789abcdef"


def test_a_write_with_no_request_context_still_records(db: Session, engine: Engine) -> None:
    """Worker and startup writes have no request; the columns are NOT NULL regardless."""
    audit.record(
        db,
        AuditAction.ANALYSIS_PAUSED,
        actor_label="worker",
        target_type="project",
        target_id=TARGET_ID,
    )
    db.commit()

    stored = _recorded(engine)[0]
    assert stored.request_id == ""
    assert stored.details == {}
    assert stored.actor_user_id is None
    assert stored.actor_label == "worker"


def test_oversized_provenance_is_truncated_to_fit_its_columns(db: Session, engine: Engine) -> None:
    """A hostile User-Agent header must not be able to fail the operation's own insert."""
    audit.record(
        db,
        AuditAction.USER_LOGIN_FAILED,
        target_type="user",
        target_id=TARGET_ID,
        source_ip="9" * 300,
        user_agent="u" * 4000,
    )
    db.commit()

    stored = _recorded(engine)[0]
    assert len(stored.source_ip) == 64
    assert len(stored.user_agent) == 255


# ---------------------------------------------------------------------------
# permissions — project_role_for
# ---------------------------------------------------------------------------


def test_project_role_for_returns_the_members_own_role(
    db: Session, seeded: dict[str, User], project: Project
) -> None:
    assert project_role_for(db, seeded["engineer"], project.id) is ProjectRole.OWNER
    assert project_role_for(db, seeded["reviewer"], project.id) is ProjectRole.REVIEWER


def test_project_role_for_returns_none_for_a_non_member(
    db: Session, seeded: dict[str, User], project: Project
) -> None:
    """Holding the global engineer role must not open somebody else's project."""
    outsider = seeded["outsider"]
    assert outsider.has_role(RoleName.ENGINEER)
    assert project_role_for(db, outsider, project.id) is None
    assert project_role_for(db, outsider, UNKNOWN_PROJECT_ID) is None


def test_administrator_is_owner_on_a_project_they_are_not_a_member_of(
    db: Session, seeded: dict[str, User], project: Project
) -> None:
    """Someone must be able to recover an orphaned project without editing rows by hand."""
    membership = db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == seeded["admin"].id,
        )
    ).scalar_one_or_none()
    assert membership is None

    assert project_role_for(db, seeded["admin"], project.id) is ProjectRole.OWNER
    assert project_role_for(db, seeded["admin"], UNKNOWN_PROJECT_ID) is ProjectRole.OWNER


# ---------------------------------------------------------------------------
# permissions — require_project_access
# ---------------------------------------------------------------------------


def test_require_project_access_returns_the_role_when_it_is_sufficient() -> None:
    assert (
        require_project_access(ProjectRole.OWNER, WRITE_ROLES, operation="upload files")
        is ProjectRole.OWNER
    )
    assert (
        require_project_access(ProjectRole.ENGINEER, WRITE_ROLES, operation="upload files")
        is ProjectRole.ENGINEER
    )
    assert (
        require_project_access(ProjectRole.VIEWER, READ_ROLES, operation="view findings")
        is ProjectRole.VIEWER
    )


def test_require_project_access_rejects_a_non_member() -> None:
    with pytest.raises(PermissionDeniedError) as raised:
        require_project_access(None, WRITE_ROLES, operation="upload files")

    assert raised.value.status_code == 403
    assert raised.value.code == "permission_denied"
    assert raised.value.message == "You do not have permission to upload files on this project."
    assert raised.value.details == {"required_any_of": ["engineer", "owner"]}


@pytest.mark.parametrize("role", [ProjectRole.REVIEWER, ProjectRole.VIEWER])
def test_require_project_access_rejects_a_role_outside_the_allowed_set(
    role: ProjectRole,
) -> None:
    with pytest.raises(PermissionDeniedError) as raised:
        require_project_access(role, WRITE_ROLES, operation="upload files")

    assert raised.value.status_code == 403


def test_non_member_and_wrong_role_are_reported_identically() -> None:
    """Two different messages would let any account enumerate which project ids exist."""
    with pytest.raises(PermissionDeniedError) as stranger:
        require_project_access(None, WRITE_ROLES, operation="upload files")
    with pytest.raises(PermissionDeniedError) as viewer:
        require_project_access(ProjectRole.VIEWER, WRITE_ROLES, operation="upload files")

    assert stranger.value.message == viewer.value.message
    assert stranger.value.to_payload() == viewer.value.to_payload()


# ---------------------------------------------------------------------------
# permissions — the project-role truth table
# ---------------------------------------------------------------------------


def test_the_truth_tables_cover_every_project_role() -> None:
    """A newly added ProjectRole must be granted access explicitly, never by omission."""
    assert set(ProjectRole) == {
        ProjectRole.OWNER,
        ProjectRole.ENGINEER,
        ProjectRole.REVIEWER,
        ProjectRole.VIEWER,
    }


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (ProjectRole.OWNER, True),
        (ProjectRole.ENGINEER, True),
        (ProjectRole.REVIEWER, False),
        (ProjectRole.VIEWER, False),
        (None, False),
    ],
)
def test_can_write_project_truth_table(role: ProjectRole | None, expected: bool) -> None:
    assert can_write_project(role) is expected


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (ProjectRole.OWNER, True),
        (ProjectRole.ENGINEER, False),
        (ProjectRole.REVIEWER, True),
        (ProjectRole.VIEWER, False),
        (None, False),
    ],
)
def test_can_review_project_truth_table(role: ProjectRole | None, expected: bool) -> None:
    """An engineer may not sign off their own project's findings; that is the reviewer's job."""
    assert can_review_project(role) is expected


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (ProjectRole.OWNER, True),
        (ProjectRole.ENGINEER, True),
        (ProjectRole.REVIEWER, True),
        (ProjectRole.VIEWER, True),
        (None, False),
    ],
)
def test_can_read_project_truth_table(role: ProjectRole | None, expected: bool) -> None:
    assert can_read_project(role) is expected


# ---------------------------------------------------------------------------
# permissions — approval decisions need both layers
# ---------------------------------------------------------------------------


def test_project_owner_without_the_global_reviewer_role_cannot_decide_an_approval(
    seeded: dict[str, User],
) -> None:
    """Owning the project is not an engineering sign-off; the reviewer role is."""
    engineer = seeded["engineer"]
    assert engineer.role_names == {RoleName.ENGINEER}

    assert can_decide_approval(engineer, ProjectRole.OWNER) is False
    assert can_decide_approval(engineer, ProjectRole.REVIEWER) is False


def test_reviewer_with_a_reviewing_project_role_can_decide_an_approval(
    seeded: dict[str, User],
) -> None:
    reviewer = seeded["reviewer"]
    assert reviewer.has_role(RoleName.REVIEWER)

    assert can_decide_approval(reviewer, ProjectRole.REVIEWER) is True
    assert can_decide_approval(reviewer, ProjectRole.OWNER) is True


@pytest.mark.parametrize("role", [ProjectRole.ENGINEER, ProjectRole.VIEWER, None])
def test_the_global_reviewer_role_alone_cannot_decide_an_approval(
    seeded: dict[str, User], role: ProjectRole | None
) -> None:
    """The global role says what kind of work is allowed; membership says where."""
    assert can_decide_approval(seeded["reviewer"], role) is False


def test_administrator_can_decide_an_approval_from_any_project_role(
    seeded: dict[str, User],
) -> None:
    """Administrators are the recovery path, so they are never blocked by REVIEW_ROLES."""
    admin = seeded["admin"]
    assert admin.has_role(RoleName.REVIEWER) is False

    for role in (*ProjectRole, None):
        assert can_decide_approval(admin, role) is True
    assert ProjectRole.ENGINEER not in REVIEW_ROLES


# ---------------------------------------------------------------------------
# permissions — require_global_role
# ---------------------------------------------------------------------------


def test_require_global_role_accepts_a_user_holding_one_of_the_allowed_roles(
    seeded: dict[str, User],
) -> None:
    require_global_role(seeded["engineer"], RoleName.ENGINEER)
    require_global_role(seeded["reviewer"], RoleName.REVIEWER, RoleName.ADMINISTRATOR)


def test_require_global_role_rejects_a_user_without_any_allowed_role(
    seeded: dict[str, User],
) -> None:
    with pytest.raises(PermissionDeniedError) as raised:
        require_global_role(seeded["engineer"], RoleName.REVIEWER)

    assert raised.value.status_code == 403
    assert raised.value.code == "permission_denied"
    assert raised.value.details == {"required_any_of": ["reviewer"]}


def test_administrator_passes_every_global_role_check(seeded: dict[str, User]) -> None:
    """Call sites list the roles they need without having to remember to add 'administrator'."""
    admin = seeded["admin"]
    assert admin.role_names == {RoleName.ADMINISTRATOR}

    for role in RoleName:
        require_global_role(admin, role)
