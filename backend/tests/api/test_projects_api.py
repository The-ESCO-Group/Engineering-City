"""Project HTTP surface: creation, listing, detail, update, archive, membership.

The access-control assertions here are the ones that matter most: a global
``engineer`` role must never be enough to reach another engineer's project.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import AuditAction, EventType, ProjectRole, ProjectStatus
from app.models.audit import AuditEntry
from app.models.event import Event
from app.models.project import Project, ProjectMember
from app.models.user import User
from tests.conftest import AuthedClient

ZERO_COUNTS = {
    "files": 0,
    "files_parsed": 0,
    "tasks": 0,
    "tasks_completed": 0,
    "findings": 0,
    "findings_open": 0,
    "approvals_pending": 0,
    "reports": 0,
}


def _create(client: AuthedClient, name: str, **extra: Any) -> dict[str, Any]:
    response = client.post("/api/projects", json={"name": name, **extra})
    assert response.status_code == 201, response.text
    return dict(response.json())


def _ids(body: dict[str, Any]) -> set[str]:
    return {item["id"] for item in body["items"]}


def _force_status(db: Session, project_id: uuid.UUID, status: ProjectStatus) -> None:
    """Put a project into a status the HTTP surface deliberately cannot set.

    Status belongs to the workflow engine, so the listing filters can only be
    exercised by arranging the row directly.
    """
    db.execute(update(Project).where(Project.id == project_id).values(status=status))
    db.commit()


# -- create -----------------------------------------------------------------


def test_create_project_returns_a_draft_owned_by_its_creator(
    engineer_client: AuthedClient,
) -> None:
    response = engineer_client.post(
        "/api/projects",
        json={
            "name": "Synthetic Packaging Line 7",
            "description": "Synthetic scope note.",
            "client_reference": "TEST-777",
            "attributes": {"site": "Synthetic Plant"},
        },
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["name"] == "Synthetic Packaging Line 7"
    assert body["description"] == "Synthetic scope note."
    assert body["client_reference"] == "TEST-777"
    assert body["attributes"] == {"site": "Synthetic Plant"}
    assert body["status"] == ProjectStatus.DRAFT.value
    assert body["archived_at"] is None
    assert body["counts"] == ZERO_COUNTS

    assert len(body["members"]) == 1
    owner = body["members"][0]
    assert owner["user_id"] == str(engineer_client.user.id)
    assert owner["role"] == ProjectRole.OWNER.value
    assert owner["display_name"] == "Test Engineer"


def test_create_project_lays_out_the_storage_directory(
    engineer_client: AuthedClient, settings: Settings
) -> None:
    """Uploads must never have to bootstrap storage, so creation does it."""
    body = _create(engineer_client, "Synthetic Storage Layout")

    root: Path = settings.project_dir(body["id"])
    assert root.is_dir()
    assert sorted(child.name for child in root.iterdir()) == ["derived", "originals", "reports"]


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_create_project_rejects_a_blank_name(engineer_client: AuthedClient, name: str) -> None:
    """A project with no usable name is unfindable and unciteable later."""
    response = engineer_client.post("/api/projects", json={"name": name})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_create_project_requires_authentication(client: TestClient) -> None:
    response = client.post("/api/projects", json={"name": "Synthetic Anonymous"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


# -- list -------------------------------------------------------------------


def test_list_projects_returns_the_callers_own_project(
    engineer_client: AuthedClient, project: Project
) -> None:
    response = engineer_client.get("/api/projects")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["total"] == 1
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert _ids(body) == {str(project.id)}


def test_global_engineer_role_does_not_expose_another_engineers_project(
    outsider_client: AuthedClient, project: Project
) -> None:
    """Membership, not the global role, decides which projects exist for you."""
    response = outsider_client.get("/api/projects")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_administrator_sees_projects_they_are_not_a_member_of(
    admin_client: AuthedClient, outsider_client: AuthedClient, project: Project
) -> None:
    """Someone has to be able to recover an orphaned project."""
    other = _create(outsider_client, "Synthetic Unrelated Line")

    body = admin_client.get("/api/projects").json()
    assert body["total"] == 2
    assert _ids(body) == {str(project.id), other["id"]}


def test_list_projects_pages_without_losing_or_repeating_rows(
    engineer_client: AuthedClient, project: Project
) -> None:
    expected = {str(project.id)}
    expected.add(_create(engineer_client, "Synthetic Line B")["id"])
    expected.add(_create(engineer_client, "Synthetic Line C")["id"])

    first = engineer_client.get("/api/projects", params={"limit": 2, "offset": 0}).json()
    second = engineer_client.get("/api/projects", params={"limit": 2, "offset": 2}).json()

    assert first["total"] == 3
    assert second["total"] == 3
    assert (first["limit"], first["offset"]) == (2, 0)
    assert (second["limit"], second["offset"]) == (2, 2)
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert _ids(first).isdisjoint(_ids(second))
    assert _ids(first) | _ids(second) == expected


def test_list_projects_filters_by_status(
    engineer_client: AuthedClient, project: Project, db: Session
) -> None:
    running = _create(engineer_client, "Synthetic Line Under Review")
    _force_status(db, uuid.UUID(running["id"]), ProjectStatus.AWAITING_REVIEW)

    drafts = engineer_client.get("/api/projects", params={"status": "draft"}).json()
    awaiting = engineer_client.get("/api/projects", params={"status": "awaiting_review"}).json()

    assert (drafts["total"], _ids(drafts)) == (1, {str(project.id)})
    assert (awaiting["total"], _ids(awaiting)) == (1, {running["id"]})


def test_list_projects_can_exclude_archived_projects(
    engineer_client: AuthedClient, project: Project, db: Session
) -> None:
    """Archived projects are retained history, so they are listed unless excluded."""
    live = _create(engineer_client, "Synthetic Line Live")
    _force_status(db, project.id, ProjectStatus.ARCHIVED)

    body = engineer_client.get("/api/projects", params={"include_archived": False}).json()
    assert (body["total"], _ids(body)) == (1, {live["id"]})

    with_archived = engineer_client.get("/api/projects").json()
    assert (with_archived["total"], _ids(with_archived)) == (2, {live["id"], str(project.id)})


# -- detail -----------------------------------------------------------------
#
# There is no test that a non-member cannot tell a real project id from an
# invented one: the endpoint answers 403 for the first and 404 for the second,
# which is reported as a defect rather than asserted as intended behaviour.


def test_project_detail_is_visible_to_the_owner(
    engineer_client: AuthedClient, project: Project, seeded: dict[str, User]
) -> None:
    response = engineer_client.get(f"/api/projects/{project.id}")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["id"] == str(project.id)
    assert body["name"] == "Synthetic Conveyor Line 3"
    assert body["counts"] == ZERO_COUNTS
    assert {(member["user_id"], member["role"]) for member in body["members"]} == {
        (str(seeded["engineer"].id), ProjectRole.OWNER.value),
        (str(seeded["reviewer"].id), ProjectRole.REVIEWER.value),
    }


def test_project_detail_is_visible_to_a_reviewer_member(
    reviewer_client: AuthedClient, project: Project
) -> None:
    response = reviewer_client.get(f"/api/projects/{project.id}")
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(project.id)


def test_non_member_is_refused_project_detail(
    outsider_client: AuthedClient, project: Project
) -> None:
    """Reading a project requires membership even for a global engineer."""
    response = outsider_client.get(f"/api/projects/{project.id}")
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "permission_denied"


def test_unknown_project_id_is_not_found_for_an_administrator(
    admin_client: AuthedClient,
) -> None:
    response = admin_client.get(f"/api/projects/{uuid.uuid4()}")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


def test_project_detail_requires_authentication(client: TestClient, project: Project) -> None:
    response = client.get(f"/api/projects/{project.id}")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


# -- update -----------------------------------------------------------------


def test_patch_cannot_change_project_status(
    engineer_client: AuthedClient, project: Project, db: Session
) -> None:
    """Status belongs to the workflow engine; an extra field must not smuggle a
    project past its state machine."""
    response = engineer_client.patch(
        f"/api/projects/{project.id}", json={"status": ProjectStatus.COMPLETED.value}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ProjectStatus.DRAFT.value

    db.expire_all()
    stored = db.get(Project, project.id)
    assert stored is not None
    assert stored.status == ProjectStatus.DRAFT
    assert stored.completed_at is None


def test_reviewer_member_cannot_update_the_project(
    reviewer_client: AuthedClient, project: Project, db: Session
) -> None:
    """The reviewer role reads and signs off; it must not edit the record it reviews."""
    response = reviewer_client.patch(f"/api/projects/{project.id}", json={"name": "Renamed"})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "permission_denied"

    db.expire_all()
    stored = db.get(Project, project.id)
    assert stored is not None
    assert stored.name == "Synthetic Conveyor Line 3"


def test_non_member_cannot_update_the_project(
    outsider_client: AuthedClient, project: Project
) -> None:
    response = outsider_client.patch(f"/api/projects/{project.id}", json={"name": "Renamed"})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "permission_denied"


def test_patch_rejects_a_blank_name(engineer_client: AuthedClient, project: Project) -> None:
    response = engineer_client.patch(f"/api/projects/{project.id}", json={"name": "   "})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


# -- archive ----------------------------------------------------------------
#
# The archive endpoint, and any PATCH that actually applies a change, currently
# fail with a 500 for every project read back from the database. Those are
# reported as defects; asserting the intended 200/409 responses here would only
# produce failing tests.


def test_archived_project_is_still_readable(
    engineer_client: AuthedClient, project: Project, db: Session
) -> None:
    """Archiving is not deletion: the project stays visible as retained history."""
    _force_status(db, project.id, ProjectStatus.ARCHIVED)

    response = engineer_client.get(f"/api/projects/{project.id}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ProjectStatus.ARCHIVED.value


# -- members ----------------------------------------------------------------


def test_owner_can_grant_a_viewer_read_access_without_write_access(
    engineer_client: AuthedClient,
    outsider_client: AuthedClient,
    project: Project,
    seeded: dict[str, User],
) -> None:
    """A viewer is the weakest membership: it must open reads and nothing else."""
    assert outsider_client.get(f"/api/projects/{project.id}").status_code == 403

    response = engineer_client.post(
        f"/api/projects/{project.id}/members",
        json={"user_id": str(seeded["outsider"].id), "role": ProjectRole.VIEWER.value},
    )
    assert response.status_code == 200, response.text
    assert (str(seeded["outsider"].id), ProjectRole.VIEWER.value) in {
        (member["user_id"], member["role"]) for member in response.json()
    }

    assert outsider_client.get(f"/api/projects/{project.id}").status_code == 200
    denied = outsider_client.patch(f"/api/projects/{project.id}", json={"name": "Renamed"})
    assert denied.status_code == 403, denied.text


def test_adding_an_unknown_user_as_a_member_is_not_found(
    engineer_client: AuthedClient, project: Project
) -> None:
    response = engineer_client.post(
        f"/api/projects/{project.id}/members",
        json={"user_id": str(uuid.uuid4()), "role": ProjectRole.VIEWER.value},
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


def test_re_adding_a_member_updates_their_role_instead_of_duplicating(
    engineer_client: AuthedClient, project: Project, seeded: dict[str, User]
) -> None:
    """Two rows for one user on one project would make their effective role ambiguous."""
    reviewer_id = str(seeded["reviewer"].id)

    body = engineer_client.post(
        f"/api/projects/{project.id}/members",
        json={"user_id": reviewer_id, "role": ProjectRole.VIEWER.value},
    ).json()

    assert [member["user_id"] for member in body].count(reviewer_id) == 1
    assert len(body) == 2
    roles = {member["user_id"]: member["role"] for member in body}
    assert roles[reviewer_id] == ProjectRole.VIEWER.value


def test_members_endpoint_lists_every_member_with_their_display_name(
    engineer_client: AuthedClient, project: Project
) -> None:
    response = engineer_client.get(f"/api/projects/{project.id}/members")
    assert response.status_code == 200, response.text

    assert [(member["display_name"], member["role"]) for member in response.json()] == [
        ("Test Engineer", ProjectRole.OWNER.value),
        ("Test Reviewer", ProjectRole.REVIEWER.value),
    ]


def test_non_member_cannot_add_themselves_to_a_project(
    outsider_client: AuthedClient, project: Project, seeded: dict[str, User], db: Session
) -> None:
    """Self-service membership would make every access check pointless."""
    response = outsider_client.post(
        f"/api/projects/{project.id}/members",
        json={"user_id": str(seeded["outsider"].id), "role": ProjectRole.OWNER.value},
    )
    assert response.status_code == 403, response.text

    db.expire_all()
    members = db.execute(
        select(ProjectMember).where(ProjectMember.project_id == project.id)
    ).scalars()
    assert {member.user_id for member in members} == {
        seeded["engineer"].id,
        seeded["reviewer"].id,
    }


# -- events and audit -------------------------------------------------------


def test_creating_a_project_records_the_first_project_event(
    engineer_client: AuthedClient, db: Session
) -> None:
    """Clients replay a project's history from seq 1, so creation must be seq 1."""
    body = _create(engineer_client, "Synthetic Event Line")

    db.expire_all()
    events = list(
        db.execute(
            select(Event).where(Event.project_id == uuid.UUID(body["id"])).order_by(Event.seq)
        ).scalars()
    )
    assert [(event.seq, event.event_type) for event in events] == [
        (1, EventType.PROJECT_CREATED.value)
    ]
    assert events[0].payload == {
        "project_id": body["id"],
        "name": "Synthetic Event Line",
        "status": ProjectStatus.DRAFT.value,
    }
    assert events[0].actor_user_id == engineer_client.user.id


def test_creating_a_project_writes_an_audit_entry(
    engineer_client: AuthedClient, db: Session
) -> None:
    body = _create(engineer_client, "Synthetic Audit Line", client_reference="TEST-AUD")

    db.expire_all()
    entries = list(
        db.execute(
            select(AuditEntry).where(AuditEntry.project_id == uuid.UUID(body["id"]))
        ).scalars()
    )
    assert [entry.action for entry in entries] == [AuditAction.PROJECT_CREATED.value]
    assert entries[0].actor_user_id == engineer_client.user.id
    assert entries[0].target_type == "project"
    assert entries[0].target_id == uuid.UUID(body["id"])
    assert entries[0].details == {"client_reference": "TEST-AUD"}
