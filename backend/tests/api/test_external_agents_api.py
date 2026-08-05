"""External SDK agents enter through the same evidence/action gates as native agents."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AgentStatus, EventType, ProjectRole
from app.models.agent import AgentInstance, AgentMessage, AgentRun
from app.models.event import Event
from app.models.project import Project, ProjectMember
from tests.conftest import AuthedClient


def _deploy(client: AuthedClient, project: Project, *, provider: str = "openai") -> dict:
    response = client.post(
        f"/api/projects/{project.id}/external-agents",
        json={
            "provider": provider,
            "display_name": "Orbit",
            "specialty": "PLC evidence reviewer",
            "mission": "Review the available project evidence and publish only cited findings.",
            "model": "sdk-test-model",
            "initial_location": "project_intake",
            "ttl_seconds": 3600,
            "acknowledge_external_processing": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _agent_headers(deployment: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {deployment['agent_token']}"}


def test_deploy_returns_one_scoped_sdk_mission_and_announces_city_presence(
    db: Session,
    project: Project,
    engineer_client: AuthedClient,
) -> None:
    deployed = _deploy(engineer_client, project)

    assert deployed["provider"] == "openai"
    assert deployed["model"] == "sdk-test-model"
    assert "create_finding" in deployed["allowed_actions"]
    assert "review_changeset" not in deployed["allowed_actions"]
    assert deployed["endpoints"]["context"] == "/api/external-agents/session/context"
    instance = db.get(AgentInstance, uuid.UUID(deployed["instance"]["id"]))
    assert instance is not None
    assert instance.status is AgentStatus.WALKING
    assert instance.kind.startswith("external_openai_")

    event_types = list(
        db.execute(
            select(Event.event_type).where(
                Event.project_id == project.id,
                Event.agent_id == instance.id,
            )
        ).scalars()
    )
    assert EventType.AGENT_STATUS_CHANGED in event_types
    assert EventType.AGENT_LOCATION_CHANGED in event_types


def test_deploy_requires_project_write_access_and_explicit_external_acknowledgement(
    project: Project,
    outsider_client: AuthedClient,
    engineer_client: AuthedClient,
) -> None:
    denied = outsider_client.post(
        f"/api/projects/{project.id}/external-agents",
        json={
            "provider": "openai",
            "display_name": "Orbit",
            "specialty": "Reviewer",
            "mission": "Review evidence",
            "model": "sdk-test-model",
            "acknowledge_external_processing": True,
        },
    )
    assert denied.status_code == 403

    unacknowledged = engineer_client.post(
        f"/api/projects/{project.id}/external-agents",
        json={
            "provider": "anthropic",
            "display_name": "Beacon",
            "specialty": "Requirements reviewer",
            "mission": "Review requirements",
            "model": "sdk-test-model",
            "acknowledge_external_processing": False,
        },
    )
    assert unacknowledged.status_code == 422

    unsupported_provider = engineer_client.post(
        f"/api/projects/{project.id}/external-agents",
        json={
            "provider": "ollama",
            "display_name": "Local worker",
            "specialty": "Reviewer",
            "mission": "Review requirements",
            "model": "local-model",
            "acknowledge_external_processing": True,
        },
    )
    assert unsupported_provider.status_code == 422


def test_capability_token_reads_bounded_context_but_user_tokens_do_not(
    project: Project,
    engineer_client: AuthedClient,
) -> None:
    deployed = _deploy(engineer_client, project, provider="anthropic")

    context = engineer_client.get(
        "/api/external-agents/session/context?evidence_limit=10",
        headers=_agent_headers(deployed),
    )
    assert context.status_code == 200, context.text
    body = context.json()
    assert body["mission"].startswith("Review the available")
    assert body["project"]["id"] == str(project.id)
    assert body["evidence_limit"] == 10
    assert "rules" in body
    assert "actions" in body["response_schema"]["properties"]

    ordinary_session = engineer_client.get("/api/external-agents/session/context")
    assert ordinary_session.status_code == 401


def test_progress_moves_the_visible_agent_and_records_only_a_human_facing_message(
    db: Session,
    project: Project,
    engineer_client: AuthedClient,
) -> None:
    deployed = _deploy(engineer_client, project)
    response = engineer_client.post(
        "/api/external-agents/session/progress",
        headers=_agent_headers(deployed),
        json={
            "status": "working",
            "location": "ladder_logic_lab",
            "activity": "Reviewing cited ladder evidence",
            "message": "Loaded the evidence index; reviewing the interlock routines.",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "working"
    assert response.json()["location"] == "ladder_logic_lab"

    instance_id = uuid.UUID(deployed["instance"]["id"])
    message = db.execute(
        select(AgentMessage).where(AgentMessage.agent_instance_id == instance_id)
    ).scalar_one()
    assert "reviewing the interlock" in message.body.lower()
    assert message.context == {"provider": "openai", "external_sdk": True}


def test_submission_uses_native_action_validation_and_is_exactly_idempotent(
    db: Session,
    project: Project,
    engineer_client: AuthedClient,
) -> None:
    deployed = _deploy(engineer_client, project)
    submission_id = str(uuid.uuid4())
    payload = {
        "submission_id": submission_id,
        "rationale": "The mission completed without storing private reasoning.",
        "actions": [
            {
                "action_type": "post_message",
                "summary": "Publish the external review summary",
                "payload": {"body": "External SDK review completed."},
            }
        ],
        "warnings": [],
        "prompt_tokens": 120,
        "completion_tokens": 40,
        "latency_ms": 250,
    }
    first = engineer_client.post(
        "/api/external-agents/session/submit",
        headers=_agent_headers(deployed),
        json=payload,
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["accepted_actions"] == 1
    assert body["rejected_actions"] == 0
    assert body["run"]["provider"] == "openai_sdk"
    assert body["replayed"] is False

    replay = engineer_client.post(
        "/api/external-agents/session/submit",
        headers=_agent_headers(deployed),
        json=payload,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["run"]["id"] == body["run"]["id"]

    different = {**payload, "rationale": "A different result must not replace the first."}
    conflict = engineer_client.post(
        "/api/external-agents/session/submit",
        headers=_agent_headers(deployed),
        json=different,
    )
    assert conflict.status_code == 409

    instance_id = uuid.UUID(deployed["instance"]["id"])
    runs = db.execute(
        select(AgentRun).where(AgentRun.agent_instance_id == instance_id)
    ).scalars().all()
    assert len(runs) == 1

    late_progress = engineer_client.post(
        "/api/external-agents/session/progress",
        headers=_agent_headers(deployed),
        json={
            "status": "working",
            "location": "qa_campus",
            "activity": "Trying to resume a completed one-shot mission",
        },
    )
    assert late_progress.status_code == 409


def test_pausing_a_project_freezes_an_already_deployed_external_agent(
    db: Session,
    project: Project,
    engineer_client: AuthedClient,
) -> None:
    deployed = _deploy(engineer_client, project)
    project.is_paused = True
    db.commit()

    response = engineer_client.post(
        "/api/external-agents/session/progress",
        headers=_agent_headers(deployed),
        json={
            "status": "working",
            "location": "qa_campus",
            "activity": "This update must be frozen with the project",
        },
    )
    assert response.status_code == 409


def test_external_agent_cannot_submit_review_or_release_authority(
    project: Project,
    engineer_client: AuthedClient,
) -> None:
    deployed = _deploy(engineer_client, project)
    response = engineer_client.post(
        "/api/external-agents/session/submit",
        headers=_agent_headers(deployed),
        json={
            "submission_id": str(uuid.uuid4()),
            "rationale": "Attempting an action outside the mission capability.",
            "actions": [
                {
                    "action_type": "review_changeset",
                    "summary": "Attempt to approve a change set",
                    "payload": {"changeset_id": str(uuid.uuid4()), "verdict": "approve"},
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["accepted_actions"] == 0
    assert response.json()["rejected_actions"] == 1
    assert "not permitted" in response.json()["run"]["rejected_actions"][0][
        "rejection_reason"
    ].lower()


def test_revocation_and_deployer_membership_loss_invalidate_the_agent_token(
    db: Session,
    project: Project,
    engineer_client: AuthedClient,
    seeded: dict,
) -> None:
    first = _deploy(engineer_client, project)
    revoked = engineer_client.delete(
        f"/api/projects/{project.id}/external-agents/{first['instance']['id']}"
    )
    assert revoked.status_code == 200, revoked.text
    after_revoke = engineer_client.get(
        "/api/external-agents/session/context",
        headers=_agent_headers(first),
    )
    assert after_revoke.status_code == 401

    second = _deploy(engineer_client, project)
    owner_row = db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == seeded["engineer"].id,
        )
    ).scalar_one()
    owner_row.role = ProjectRole.VIEWER
    db.commit()

    after_downgrade = engineer_client.get(
        "/api/external-agents/session/context",
        headers=_agent_headers(second),
    )
    assert after_downgrade.status_code == 401
