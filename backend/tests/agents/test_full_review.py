"""The whole product, end to end, over the synthetic project.

Upload nine documents, run the workflow, and check that what comes out matches
the ground truth planted in ``fixtures/README.md`` — including the things that
must *not* appear. A review that invents findings is worse than one that misses
them, because an engineer stops trusting the ones that are real.

Runs entirely on the deterministic MockProvider: no network, no model, no cost.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

import app.agents.review
import app.orchestration  # noqa: F401  (registers the deterministic handlers)
from app.core.config import Settings
from app.core.enums import (
    ApprovalStatus,
    FindingSeverity,
    ProjectStatus,
    TaskKind,
    TaskStatus,
)
from app.models.agent import AgentMessage, AgentRun
from app.models.approval import Approval
from app.models.finding import Finding
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.orchestration import engine, worker
from tests.conftest import AuthedClient


def _run(db: Session, project: Project, settings: Settings, actor: User) -> None:
    engine.start_run(db, project, settings, actor=actor)
    db.commit()
    worker.drain(settings)
    db.expire_all()


def _findings(db: Session, project: Project) -> list[Finding]:
    return list(db.execute(select(Finding).where(Finding.project_id == project.id)).scalars())


def _titled(findings: list[Finding], needle: str) -> Finding | None:
    matches = [item for item in findings if needle in item.title]
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------------
def test_the_whole_workflow_runs_to_the_approval_gate(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    _run(db, ingested_project, settings, seeded["engineer"])

    tasks = {
        task.kind: task
        for task in db.execute(select(Task).where(Task.project_id == ingested_project.id)).scalars()
    }
    for kind in (
        TaskKind.INVENTORY_FILES,
        TaskKind.BUILD_EVIDENCE_GRAPH,
        TaskKind.PLAN_PROJECT,
        TaskKind.REVIEW_PLC,
        TaskKind.REVIEW_INTERLOCKS,
        TaskKind.SENIOR_REVIEW,
    ):
        assert kind in tasks, f"{kind.value} was never created"
        assert tasks[kind].status is TaskStatus.COMPLETED, (
            f"{kind.value} ended as {tasks[kind].status.value}: "
            f"{tasks[kind].blocked_reason or tasks[kind].error_message}"
        )

    assert tasks[TaskKind.REQUEST_APPROVALS].status is TaskStatus.AWAITING_APPROVAL


def test_the_project_does_not_complete_while_an_approval_is_outstanding(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The single most important behaviour in the product.

    Reporting a review complete while a safety-related finding has not been read
    by anyone is the failure this platform exists to prevent.
    """
    _run(db, ingested_project, settings, seeded["engineer"])
    db.refresh(ingested_project)

    pending = list(
        db.execute(
            select(Approval).where(
                Approval.project_id == ingested_project.id,
                Approval.status == ApprovalStatus.PENDING,
            )
        ).scalars()
    )
    assert pending, "no approval was raised for a high-severity interlock finding"
    assert ingested_project.status is ProjectStatus.AWAITING_REVIEW
    assert ingested_project.completed_at is None


# ---------------------------------------------------------------------------
# Ground truth: what must be found
# ---------------------------------------------------------------------------
def test_the_documented_but_unimplemented_interlock_is_found_and_escalated(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """ILK-003 is in the schedule, in the description and wired in the I/O list,
    and no rung implements it. It is the one finding here that could matter to
    someone's safety, so it must reach a human."""
    _run(db, ingested_project, settings, seeded["engineer"])
    finding = _titled(_findings(db, ingested_project), "ILK-003")

    assert finding is not None, "the missing interlock was not reported"
    assert finding.severity is FindingSeverity.HIGH
    assert finding.requires_approval is True
    assert finding.evidence_links, "a finding with no citation cannot be checked"
    assert "CV102_JAM" in finding.fact
    # The limitation is not optional: absence from one export is not absence in
    # the plant, and a report that omitted that would overstate the claim.
    assert finding.limitations

    approval = db.execute(select(Approval).where(Approval.finding_id == finding.id)).scalar_one()
    assert approval.status is ApprovalStatus.PENDING
    assert approval.question


def test_the_naming_mismatch_is_found_and_not_inflated(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """ILK-004 *is* implemented, under a contracted name.

    Rating it as high as a genuinely missing interlock would bury the one that
    matters, so the severity being modest is the assertion.
    """
    _run(db, ingested_project, settings, seeded["engineer"])
    finding = _titled(_findings(db, ingested_project), "ILK-004")

    assert finding is not None
    assert finding.severity is not FindingSeverity.HIGH
    assert finding.severity is not FindingSeverity.CRITICAL
    assert "GUARD_DOOR_CLOSED" in finding.title or "GUARD_DOOR_CLOSED" in finding.fact
    assert "GRD_DR_CLSD" in finding.title or "GRD_DR_CLSD" in finding.fact

    roles = {link.role for link in finding.evidence_links}
    assert "requirement" in roles, "the schedule side was not cited"
    assert "logic" in roles, "the logic side was not cited"


def test_the_underspecified_interlock_is_reported_without_inventing_a_threshold(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """ILK-007 has no tag, no condition and no fail-safe.

    Nothing can be concluded about whether it is implemented, so a finding that
    claimed anything about the logic here would be fabrication.
    """
    _run(db, ingested_project, settings, seeded["engineer"])
    finding = _titled(_findings(db, ingested_project), "ILK-007")

    assert finding is not None
    assert finding.severity in {FindingSeverity.INFORMATIONAL, FindingSeverity.LOW}

    body = f"{finding.title} {finding.fact} {finding.inference}"
    for invented in ("40.0", "60 psi", "not implemented", "missing from the logic"):
        assert invented not in body, f"the finding invented '{invented}' for ILK-007"


def test_the_undeclared_alarm_reference_is_found(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    _run(db, ingested_project, settings, seeded["engineer"])
    finding = _titled(_findings(db, ingested_project), "ALM_HOPPER_HIGH")

    assert finding is not None
    assert finding.evidence_links
    location = finding.evidence_links[0].anchor.display_location
    assert "Alarm_Logic" in location, f"cited {location}"


# ---------------------------------------------------------------------------
# Ground truth: what must NOT be found
# ---------------------------------------------------------------------------
def test_the_correctly_implemented_interlocks_produce_no_findings(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """ILK-001, ILK-002 and ILK-005's tag all match. A finding against any of
    them is a false positive, and false positives are what stop an engineer
    reading the real ones."""
    _run(db, ingested_project, settings, seeded["engineer"])
    findings = _findings(db, ingested_project)

    for identifier in ("ILK-001", "ILK-002"):
        assert _titled(findings, identifier) is None, (
            f"{identifier} is correctly implemented but was reported"
        )


def test_no_finding_rests_only_on_the_ambiguous_site_notes(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The notes file is deliberately vague hearsay with no tags or thresholds.

    A finding citing it alone would be fabrication dressed up as evidence.
    """
    _run(db, ingested_project, settings, seeded["engineer"])

    for finding in _findings(db, ingested_project):
        sources = {link.anchor.source_label for link in finding.evidence_links}
        assert sources != {"Site_Notes_Ambiguous.txt"}, (
            f"'{finding.title}' rests only on the ambiguous notes"
        )


# ---------------------------------------------------------------------------
# Evidence, review and honesty
# ---------------------------------------------------------------------------
def test_every_finding_cites_at_least_one_evidence_anchor(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    _run(db, ingested_project, settings, seeded["engineer"])
    for finding in _findings(db, ingested_project):
        assert finding.evidence_links, f"'{finding.title}' has no evidence"
        for link in finding.evidence_links:
            assert link.anchor is not None
            assert link.anchor.display_location
            assert link.anchor.project_id == ingested_project.id


def test_every_finding_separates_fact_from_inference(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    _run(db, ingested_project, settings, seeded["engineer"])
    for finding in _findings(db, ingested_project):
        assert finding.fact, f"'{finding.title}' states no fact"
        assert finding.fact != finding.inference


def test_every_finding_went_through_senior_review(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    _run(db, ingested_project, settings, seeded["engineer"])
    for finding in _findings(db, ingested_project):
        assert finding.is_reviewed, f"'{finding.title}' was never reviewed"
        assert finding.review_verdict


def test_senior_review_never_raises_a_confidence(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """Review exists to add doubt. A second pass that could raise confidence
    would let a weak claim be laundered into a strong one."""
    _run(db, ingested_project, settings, seeded["engineer"])
    for finding in _findings(db, ingested_project):
        if finding.original_confidence is None:
            continue
        assert finding.confidence <= finding.original_confidence + 1e-9, (
            f"'{finding.title}' had its confidence raised from "
            f"{finding.original_confidence} to {finding.confidence}"
        )


def test_no_agent_run_stores_a_reasoning_trace(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """Only a concise, human-readable rationale is retained, by construction."""
    _run(db, ingested_project, settings, seeded["engineer"])
    runs = list(
        db.execute(select(AgentRun).where(AgentRun.project_id == ingested_project.id)).scalars()
    )
    assert runs
    for run in runs:
        assert len(run.rationale) < 4000
        for column in ("thinking", "reasoning", "chain_of_thought", "transcript"):
            assert not hasattr(run, column), f"AgentRun exposes {column}"


def test_the_run_says_which_document_it_could_not_read(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The scanned drawing has no text layer. A review that stayed silent about
    it would imply it had been examined."""
    _run(db, ingested_project, settings, seeded["engineer"])

    messages = list(
        db.execute(
            select(AgentMessage).where(AgentMessage.project_id == ingested_project.id)
        ).scalars()
    )
    graph_task = db.execute(
        select(Task).where(
            Task.project_id == ingested_project.id,
            Task.kind == TaskKind.BUILD_EVIDENCE_GRAPH,
        )
    ).scalar_one()

    haystack = " ".join(message.body for message in messages) + str(graph_task.result)
    assert "Scanned_Drawing.pdf" in haystack or "no_text_layer" in haystack, (
        "nothing in the run records that a document could not be read"
    )


def test_the_mock_provider_run_is_labelled_as_not_a_model(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """Nobody should be able to mistake a deterministic run for a real review."""
    _run(db, ingested_project, settings, seeded["engineer"])
    runs = list(
        db.execute(select(AgentRun).where(AgentRun.project_id == ingested_project.id)).scalars()
    )
    assert runs
    for run in runs:
        assert run.provider == "mock"
        joined = " ".join(str(item) for item in run.warnings)
        assert "MockProvider" in joined, f"{run.agent_kind} run is not labelled"


def test_running_the_review_twice_does_not_duplicate_findings(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The queue is at-least-once, so agents must converge rather than accumulate."""
    _run(db, ingested_project, settings, seeded["engineer"])
    first = {finding.dedupe_key for finding in _findings(db, ingested_project)}

    for approval in db.execute(
        select(Approval).where(Approval.project_id == ingested_project.id)
    ).scalars():
        approval.status = ApprovalStatus.APPROVED
    db.commit()

    _run(db, ingested_project, settings, seeded["engineer"])
    second = _findings(db, ingested_project)

    assert {finding.dedupe_key for finding in second} == first
    assert len(second) == len(first)


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------
def test_findings_and_approvals_are_reachable_over_http(
    db: Session,
    ingested_project: Project,
    seeded: dict[str, User],
    settings: Settings,
    engineer_client: AuthedClient,
) -> None:
    _run(db, ingested_project, settings, seeded["engineer"])

    tasks = engineer_client.get(f"/api/projects/{ingested_project.id}/tasks")
    assert tasks.status_code == 200
    kinds = {row["kind"] for row in tasks.json()}
    assert TaskKind.REVIEW_INTERLOCKS.value in kinds

    events = engineer_client.get(f"/api/projects/{ingested_project.id}/events")
    assert events.status_code == 200
    types = {row["event_type"] for row in events.json()}
    assert "finding.created" in types
    assert "approval.requested" in types
