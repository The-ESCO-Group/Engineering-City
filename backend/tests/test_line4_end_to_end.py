"""The acceptance scenario: Line 4 Packaging Conveyor Upgrade, intake to release.

One project, driven the way an engineer drives it — sign in, create, upload the
eight artifacts, answer what is asked, decide what is raised, carry what cannot
be fixed — and then judged against
``fixtures/line4_packaging_conveyor/README.md``, which states in advance which
seven defects are planted and which eleven plausible-looking things are **not**
defects.

Every human act goes through a real door: the HTTP API where one exists, and
:mod:`app.engineering` where the API has none. Nothing here writes to the
canonical model behind the product's back, because a scenario that arranged its
own answer would be measuring the fixture rather than the platform.

Two rules govern what is asserted, and they matter more than the pass rate.

**The decoys matter more than the positives.** A review that reports everything
scores identically to one that is right, on the positives alone. The eleven
things the README forbids are the only thing that separates them, so each is
asserted individually and by name — a failure says which decoy fired.

**A missed detection is never softened.** Where the product does not find a
planted issue, or fires on a decoy, the assertion stays exactly as strong as the
README demands and the test is marked ``xfail(strict=True)`` with the reason.
The fixture exists to tell the truth; editing an assertion until it passes
destroys the only instrument there is. ``strict=True`` so that fixing the
product turns the marker into a failure and somebody deletes it.

**Scope of a "report".** The product speaks in two voices — findings, which an
engineer reads as "we found a problem", and deterministic validation results,
which gate a release. The README's ground truth is written in the finding
vocabulary and does not care which voice answers, so :class:`Report` normalises
both onto one severity scale. The decoy assertions are scoped to **findings**
and to **error-severity validation results, waived or not**: those are the two
outputs that cost an engineer time or stop a job. Every other validation level
is excluded — ``information`` and ``unsupported`` because they are the platform
declaring the limits of what it checked rather than claims about this plant,
``human_review_required`` because it is a decision put in front of a person, and
``warning`` because it does not stop a job. That last one is easy to miss:
``warning`` maps to ``MEDIUM`` and the judged set keeps only ``HIGH``, so a
warning is outside it however loudly it is worded.

Two consequences follow, stated here so neither is hidden. A defect reported
only at those levels counts as **not found**. And a *decoy* named only at those
levels does not register as fired — so the decoy score below is a score over
findings and error-severity results, not over everything the product said.

**A waiver decides a report; it does not retract one.** The run gets past its
safety gates only because a named reviewer carries risk the documents cannot
resolve, so every snapshot here keeps waived results and marks them. Three
consequences, each deliberate. A planted issue found and then carried still
counts as found, because the product did say it. A *decoy* that fired and was
then waived still counts as fired, because a false claim a human had to read and
sign for cost them exactly as much as one nobody signed. But
:attr:`Scenario.blocking_errors` holds only the *unwaived* ones, because "what
stops a release" is a statement about now and a decided result does not stop
anything.

Runs offline on the deterministic MockProvider: no network, no model, no cost.
That provider is the reason this file still carries release-half xfails, and it
is worth stating plainly because it is not the fixture's fault: it will not
choose which approved template implements a requirement, so a project whose
accepted requirements are claimed by no logic network parks at
``design_plc_architecture`` however clean its I/O is. See
:func:`test_generators_assemble_the_package_and_a_release_candidate_is_sealed`.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

# Importing these registers the task handlers and the agent implementations.
import app.agents.review
import app.orchestration.steps  # noqa: F401
from app.agents.base.plugins import get_plugin_registry
from app.core.clock import utcnow
from app.core.config import Settings, get_settings
from app.core.enums import (
    MAX_OFFLINE_VALIDATION_LEVEL,
    SEVERITY_ORDER,
    VALIDATION_LEVEL_ORDER,
    ApprovalDecision,
    ApprovalGateKey,
    ApprovalKind,
    ApprovalStatus,
    DocumentRole,
    ExportArtifactKind,
    FindingSeverity,
    GateStatus,
    ImportVerificationStatus,
    ProjectRole,
    ProjectStatus,
    RequirementStatus,
    RoleName,
    TaskKind,
    TaskStatus,
    ValidationLevel,
    ValidationSeverity,
)
from app.core.services import users as user_service
from app.core.storage import ProjectStorage
from app.database import session as db_session
from app.models import Base
from app.models.file import UploadedFile
from app.models.finding import Finding
from app.models.plc import LogicRung, Program, Routine
from app.models.profile import ProjectQuestion
from app.models.project import Project
from app.models.release import ReleaseCandidate, ValidationResult, ValidationRun
from app.models.requirements import Clarification, Requirement
from app.models.revision import ApprovalGate
from app.models.tags import Tag
from app.models.task import Task
from app.models.user import User
from app.orchestration import worker
from app.orchestration.queue import DatabaseTaskQueue

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "line4_packaging_conveyor"

#: Fixture-local, and long enough for the password policy.
PASSWORD = "line4-acceptance-password"

PROJECT_NAME = "Line 4 Packaging Conveyor Upgrade"

#: The eight artifacts, with the engineering purpose the uploading engineer
#: declares for each. Same roles as ``backend/tests/test_line4_fixture.py``
#: parses them under, so the two files cannot drift apart.
ARTIFACTS: tuple[tuple[str, DocumentRole], ...] = (
    ("Project_Description.docx", DocumentRole.OTHER),
    ("Functional_Description.docx", DocumentRole.FUNCTIONAL_DESCRIPTION),
    ("IO_List.csv", DocumentRole.IO_LIST),
    ("Interlock_Matrix.xlsx", DocumentRole.INTERLOCK_SCHEDULE),
    ("Conveyor_Line4_Base.L5X", DocumentRole.CONTROL_LOGIC),
    ("HMI_Screens.xml", DocumentRole.HMI_EXPORT),
    ("Alarm_Register.csv", DocumentRole.ALARM_LIST),
    ("Approved_Templates.txt", DocumentRole.OTHER),
)

#: A run that needs more turns than this is going round in circles, and the
#: assertions should say so rather than hang.
MAX_ROUNDS = 20

#: The two severities the product itself lets a reviewer carry.
#: :func:`app.core.services.validation_review.waive` refuses anything else, so
#: this set is read off the product rather than chosen: ``ERROR`` stops a gate and
#: ``HUMAN_REVIEW_REQUIRED`` is a decision the platform declines to make.
WAIVABLE: frozenset[str] = frozenset(
    {ValidationSeverity.ERROR.value, ValidationSeverity.HUMAN_REVIEW_REQUIRED.value}
)

#: How a deterministic validation severity compares with a finding severity.
#: Stated once, here, because the README's ground truth is written in the
#: finding vocabulary and the product answers in both. ``ERROR`` stops a release
#: and so ranks with ``HIGH``; ``WARNING`` and ``HUMAN_REVIEW_REQUIRED`` both put
#: a decision in front of a person and rank with ``MEDIUM``; the rest are the
#: platform describing its own coverage, and rank at the bottom.
VALIDATION_RANK: dict[ValidationSeverity, FindingSeverity] = {
    ValidationSeverity.ERROR: FindingSeverity.HIGH,
    ValidationSeverity.WARNING: FindingSeverity.MEDIUM,
    ValidationSeverity.HUMAN_REVIEW_REQUIRED: FindingSeverity.MEDIUM,
    ValidationSeverity.INFORMATION: FindingSeverity.INFORMATIONAL,
    ValidationSeverity.UNSUPPORTED: FindingSeverity.INFORMATIONAL,
}


# ---------------------------------------------------------------------------
# What the run produced, frozen so the assertions cannot perturb it
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Report:
    """One thing the product said about this plant, in whichever voice.

    ``text`` is every quotable word the report carries, lowercased: title,
    claim, inference, suggested action, limitations and the locations it cites.
    Ground truth is written in terms of what a report must *name*, so a single
    search surface is what the assertions need.
    """

    source: str
    code: str
    severity: FindingSeverity
    title: str
    text: str
    locations: tuple[str, ...]
    #: Whether a named reviewer has decided to carry this one knowingly. Only a
    #: validation result can be waived, so this is always ``False`` on a finding.
    is_waived: bool = False

    @property
    def is_finding(self) -> bool:
        return self.source == "finding"

    def names(self, *subjects: str) -> bool:
        return any(subject.lower() in self.text for subject in subjects)

    def at_least(self, severity: FindingSeverity) -> bool:
        return SEVERITY_ORDER[self.severity] >= SEVERITY_ORDER[severity]

    def __str__(self) -> str:
        carried = " (carried)" if self.is_waived else ""
        return f"[{self.source}/{self.severity.value}]{carried} {self.code}: {self.title}"


@dataclass(frozen=True, slots=True)
class Artifact:
    """One uploaded file as the project recorded it, next to its bytes."""

    filename: str
    sha256: str
    size_bytes: int
    stored_path: Path


@dataclass(frozen=True, slots=True)
class Waiver:
    """One validation risk a named reviewer decided to carry, and why."""

    code: str
    severity: ValidationSeverity
    object_label: str
    reviewer: str
    reason: str

    def __str__(self) -> str:
        return f"{self.code} at {self.object_label} carried by {self.reviewer}"


@dataclass(frozen=True, slots=True)
class GateRecord:
    """One gate as the release record reads it.

    ``decision`` is the distinction the whole audit turns on: ``APPROVE`` and
    ``ACCEPT_RISK`` both leave a gate ``PASSED``, and only the approval row keeps
    them apart.
    """

    key: ApprovalGateKey
    status: GateStatus
    decision: ApprovalDecision | None
    decided_by: str
    comment: str
    unmet: str

    def __str__(self) -> str:
        verdict = self.decision.value if self.decision is not None else "undecided"
        return f"{self.key.value}: {self.status.value}/{verdict} {self.unmet}"


@dataclass(frozen=True, slots=True)
class PackagedArtifact:
    """One generated file inside the release candidate, next to its bytes."""

    kind: ExportArtifactKind
    relative_path: str
    validation_level: ValidationLevel
    sha256: str
    size_bytes: int
    import_order: int
    stored_path: Path


@dataclass(frozen=True, slots=True)
class Readiness:
    """What ``GET /release-readiness`` says, read through the UI's own door.

    The ceiling claims live here rather than on the candidate because they are
    answerable before anything is generated, and because a package that has not
    been built yet is exactly when a product is most tempted to promise more than
    it can prove.
    """

    is_ready: bool
    #: ``BlockCode`` values, so an assertion can name the reason rather than
    #: pattern-matching a sentence written for a person.
    blocking_codes: tuple[str, ...]
    blocking_detail: str
    outstanding_gates: tuple[str, ...]
    validation_error_count: int
    highest_attainable_validation_level: ValidationLevel
    import_verification_possible: bool
    import_verification_detail: str


@dataclass(frozen=True, slots=True)
class Scenario:
    """Everything the acceptance run produced, snapshotted.

    Snapshotted rather than held as live ORM rows so the assertions run against
    one another's untouched view of the same run, and so the expensive drive
    happens once for the whole module.
    """

    settings: Settings
    project_id: uuid.UUID
    project_status: ProjectStatus
    artifacts: tuple[Artifact, ...]
    reports: tuple[Report, ...]
    clarifications: tuple[str, ...]
    project_questions: tuple[str, ...]
    requirements: tuple[str, ...]
    tags: tuple[str, ...]
    programs: tuple[str, ...]
    routines: tuple[str, ...]
    rung_count: int
    tasks: tuple[tuple[TaskKind, TaskStatus, str], ...]
    gate_records: tuple[GateRecord, ...]
    open_high_severity: tuple[Report, ...]
    waivers: tuple[Waiver, ...]
    validation_stops: tuple[str, ...]
    readiness: Readiness
    candidate_count: int
    candidate_levels: tuple[ValidationLevel, ...]
    candidate_import_verification: tuple[tuple[ImportVerificationStatus, str], ...]
    candidate_artifacts: tuple[PackagedArtifact, ...]
    limitations_text: str
    rounds_taken: int
    human_acts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def findings(self) -> tuple[Report, ...]:
        return tuple(item for item in self.reports if item.is_finding)

    @property
    def blocking_errors(self) -> tuple[Report, ...]:
        """Error-severity results still standing in a release's way."""
        return tuple(
            item
            for item in self.reports
            if item.source == "validation"
            and item.severity is FindingSeverity.HIGH
            and not item.is_waived
        )

    @property
    def carried_errors(self) -> tuple[Report, ...]:
        """Error-severity results a named reviewer decided to carry knowingly."""
        return tuple(
            item
            for item in self.reports
            if item.source == "validation"
            and item.severity is FindingSeverity.HIGH
            and item.is_waived
        )

    @property
    def judged(self) -> tuple[Report, ...]:
        """The reports the decoy rules apply to.

        Findings, plus every error-severity result whether or not it was waived.
        A waiver is a decision about a claim and not a retraction of it, so an
        over-reaching error a reviewer had to sign for still counts as fired.
        """
        return self.findings + self.blocking_errors + self.carried_errors

    @property
    def questions_raised(self) -> tuple[str, ...]:
        """Every persisted request for human clarification, at either scope."""
        return self.clarifications + self.project_questions

    @property
    def gates_decided(self) -> tuple[ApprovalGateKey, ...]:
        return tuple(item.key for item in self.gate_records if item.status is GateStatus.PASSED)

    def gate(self, key: ApprovalGateKey) -> GateRecord:
        matches = [item for item in self.gate_records if item.key is key]
        assert len(matches) == 1, f"{key.value} has {len(matches)} gate rows"
        return matches[0]

    def explain_gates(self) -> str:
        return "\n".join(f"  {item}" for item in self.gate_records)


# ---------------------------------------------------------------------------
# Driving the scenario
# ---------------------------------------------------------------------------
def _bootstrap(settings: Settings) -> tuple[uuid.UUID, uuid.UUID]:
    """The engineer who does the work and the reviewer who signs it off.

    Two people, not one: an engineer may not approve their own project's safety
    findings, and a scenario run by a single omnipotent account would never
    exercise the separation the gates exist for.
    """
    from app.core.bootstrap import sync_agent_definitions

    session = db_session.get_session_factory()()
    try:
        user_service.ensure_roles(session)
        engineer = user_service.create_user(
            session,
            email="line4.engineer@test.internal",
            display_name="Line 4 Controls Engineer",
            password=PASSWORD,
            roles=[RoleName.ENGINEER],
            actor_label="acceptance-fixture",
        )
        reviewer = user_service.create_user(
            session,
            email="line4.reviewer@test.internal",
            display_name="Line 4 Senior Reviewer",
            password=PASSWORD,
            roles=[RoleName.REVIEWER, RoleName.ENGINEER],
            actor_label="acceptance-fixture",
        )
        sync_agent_definitions(session)
        session.commit()
        return engineer.id, reviewer.id
    finally:
        session.close()


def _token(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _intake_payload() -> dict[str, Any]:
    """The engineering intake, exactly as the fixture's README describes the plant.

    Every value is quoted from ``fixtures/line4_packaging_conveyor/README.md``.
    Declaring anything else — a controller that is not a 5069-L330ER, or no HMI
    when there is a PanelView on the line — would be arranging the answer.
    """
    return {
        "project_name": PROJECT_NAME,
        "customer_label": "Synthetic Packaging Co.",
        "plant_or_site": "Synthetic Plant, Line 4",
        "project_description": (
            "Upgrade the Line 4 case-packaging conveyors: accumulation control, "
            "reject diverter and stop gate, on the existing CompactLogix controller."
        ),
        "project_type": "upgrade",
        "units": "metric",
        "locale": "en-GB",
        "release_mode": "import_package_only",
        "target": {
            "controller_family": "compactlogix",
            "controller_catalog_number": "5069-L330ER",
            "firmware_revision": "33.011",
            "studio_5000_version": "33.00",
            "hmi_product": "factorytalk_view_me",
            "hmi_version": "14.00",
            "hmi_terminal_model": "PanelView Plus 7 Standard 1000",
            "hmi_resolution": "1024x768",
        },
        "standards": {
            "naming_standard_version": "3.2",
            "alarm_philosophy_version": "1.1",
            "controller_template": "ESCO-TPL-CTRL-COMPACTLOGIX-01",
            "test_template_set": "ESCO-TPL-TEST-CONVEYOR-01",
        },
    }


def _upload(client: TestClient, project_id: uuid.UUID, auth: dict[str, str]) -> None:
    """One request per artifact: a declared role applies to a whole request."""
    for filename, role in ARTIFACTS:
        data = (FIXTURES / filename).read_bytes()
        response = client.post(
            f"/api/projects/{project_id}/files",
            headers=auth,
            files=[("files", (filename, io.BytesIO(data), "application/octet-stream"))],
            data={"document_role": role.value},
        )
        assert response.status_code == 201, response.text
        result = response.json()["results"][0]
        assert result["accepted"] is True, result.get("rejection_reason")


def _resolve_clarifications(
    client: TestClient, project_id: uuid.UUID, auth: dict[str, str]
) -> list[str]:
    """Answer every question that blocks generation, as a named human would.

    The answer is deliberately the same sentence every time. What is being
    exercised is that a question was asked, that a person answered it and that
    the run resumed — not the content of an answer no fixture can supply.
    """
    inbox = client.get(f"/api/projects/{project_id}/clarifications", headers=auth)
    assert inbox.status_code == 200, inbox.text
    body = inbox.json()
    answered: list[str] = []

    for row in body["clarifications"]:
        if not row["blocks_generation"] or row["status"] not in {"open", "deferred"}:
            continue
        response = client.post(
            f"/api/clarifications/{row['id']}/resolve",
            headers=auth,
            json={
                "answer": "Confirmed with the line engineer against the interlock matrix.",
                "resolution": "answered",
            },
        )
        assert response.status_code == 200, response.text
        answered.append(f"resolved clarification: {row['question'][:80]}")

    for row in body["project_questions"]:
        if not row["is_blocking"] or row["status"] not in {"open", "deferred"}:
            continue
        response = client.post(
            f"/api/project-questions/{row['id']}/resolve",
            headers=auth,
            json={
                "answer": "Confirmed with the customer.",
                "resolution": "answered",
            },
        )
        assert response.status_code == 200, response.text
        answered.append(f"resolved question: {row['question'][:80]}")

    return answered


def _decide_requirements(
    client: TestClient, project_id: uuid.UUID, auth: dict[str, str]
) -> list[str]:
    """Accept every requirement an agent proposed.

    An agent may not accept its own reading of the brief, so somebody has to —
    and it has to be the reviewer: the endpoint refuses an engineer-only
    account, which is the separation of duties the gates exist for.

    Accepting rather than rejecting is the harder path for the product: an
    accepted requirement is one the validators then hold the design to.
    """
    response = client.get(f"/api/projects/{project_id}/requirements", headers=auth)
    assert response.status_code == 200, response.text
    decided: list[str] = []
    for row in response.json():
        if row["status"] != RequirementStatus.PROPOSED.value:
            continue
        decision = client.post(
            f"/api/requirements/{row['id']}/decision",
            headers=auth,
            json={
                "decision": "accept",
                "rationale": "Checked against the functional description.",
            },
        )
        assert decision.status_code == 200, decision.text
        decided.append(f"accepted requirement {row['reference']}")
    return decided


def _carried_risk(
    client: TestClient, project_id: uuid.UUID, auth: dict[str, str]
) -> tuple[str, ...]:
    """Which validation risks this project is already knowingly carrying."""
    listing = client.get(f"/api/projects/{project_id}/validation-results", headers=auth)
    assert listing.status_code == 200, listing.text
    return tuple(
        f"{row['code']} at {row['object_label']}" for row in listing.json() if row["is_waived"]
    )


def _decide_approvals(
    client: TestClient,
    project_id: uuid.UUID,
    auth: dict[str, str],
) -> list[str]:
    """Settle eligible human decisions through the same HTTP doors as the UI.

    A gate over a project that is carrying waived risk is decided
    ``accept_risk`` rather than ``approve``, and that is not bookkeeping. Both
    verdicts leave the gate ``PASSED`` and let the work proceed; only the
    approval row remembers which, and a delivery that went out over known defects
    has to stay distinguishable afterwards from one that went out clean.
    Choosing ``accept_risk`` also exercises the rule that makes the distinction
    worth anything — the comment is mandatory, so the reason is on the record.
    """
    listing = client.get(f"/api/projects/{project_id}/approvals", headers=auth)
    assert listing.status_code == 200, listing.text
    acts: list[str] = []

    for row in listing.json():
        if row["status"] != ApprovalStatus.PENDING.value:
            continue
        if not row["decision_allowed"]:
            # Senior review has not finished for this finding. The row is
            # visible and undecidable, which is the product working.
            continue
        if row["kind"] == ApprovalKind.GATE_DECISION.value:
            assert row["gate_id"], f"gate approval {row['id']} has no exact gate binding"
            carried = _carried_risk(client, project_id, auth)
            verdict = (ApprovalDecision.ACCEPT_RISK if carried else ApprovalDecision.APPROVE).value
            comment = (
                "Checked against the functional description, interlock matrix, "
                "and current validation evidence."
            )
            if carried:
                comment += (
                    " Proceeding on accepted risk: this project carries "
                    f"{len(carried)} waived validation result(s) — "
                    + "; ".join(carried)
                    + " — each raised with the customer and scheduled into the upgrade scope."
                )
            decision = client.post(
                f"/api/approval-gates/{row['gate_id']}/approvals/{row['id']}/decision",
                headers=auth,
                json={"decision": verdict, "comment": comment},
            )
            assert decision.status_code == 200, decision.text
            acts.append(f"{verdict} on gate: {row['title'][:80]}")
            continue
        decision = client.post(
            f"/api/approvals/{row['id']}/decision",
            headers=auth,
            json={"approved": True, "notes": "Reviewed against the source documents."},
        )
        assert decision.status_code == 200, decision.text
        acts.append(f"decided approval: {row['title'][:80]}")
    return acts


def _validation_stops(client: TestClient, project_id: uuid.UUID, auth: dict[str, str]) -> list[str]:
    """The sentence cross-discipline validation is currently parked on.

    Captured while the run is parked rather than reconstructed afterwards.
    Waiving the last blocker completes the task and clears ``blocked_reason``, so
    once the run has moved on there is nothing left on the row to prove it ever
    stopped — and the stop is the safety property.
    """
    listing = client.get(f"/api/projects/{project_id}/tasks", headers=auth)
    assert listing.status_code == 200, listing.text
    return [
        row["blocked_reason"]
        for row in listing.json()
        if row["kind"] == TaskKind.VALIDATE_CROSS_DISCIPLINE.value
        and row["status"] == TaskStatus.BLOCKED.value
        and row["blocked_reason"]
    ]


def _carry_the_remaining_risk(
    client: TestClient, project_id: uuid.UUID, auth: dict[str, str]
) -> list[str]:
    """Record a named reviewer's waiver over every blocker nothing else can clear.

    The fixture's duplicate I/O assignment is a real defect in the customer's
    input: ``Local:1:I.Data.10`` is claimed by both ``GD401_CLOSED`` and
    ``MTR403_FAULT``, only one can be wired, and nothing in the eight documents
    says which. The product offers an engineer two ways out — correct the
    assignment, or carry it knowingly — and it has an HTTP door for exactly one
    of them: ``POST /validation-results/{id}/waiver``. There is no API by which a
    human edits an I/O assignment, so the correction is not a path this scenario
    can take without inventing an answer the customer has not given. Carrying it
    is both what the product supports and what an engineer would actually do with
    a channel conflict a drawing set does not resolve.

    Called only when nothing else in the round moved, which is the order an
    engineer works in: answer what can be answered, fix what can be fixed, and
    only then decide what is left. Without that ordering this would waive
    ``requirements.unanswered_question`` — a result whose honest remedy is to
    answer the question, which the round before this one does.

    Scoped to the two severities :func:`app.core.services.validation_review.waive`
    accepts. ``HUMAN_REVIEW_REQUIRED`` is in that set and is not a defect claim:
    the two rows it covers here are the ``OTL``/``OTU`` pair in
    ``Diverter_Control``, which the platform declines to judge rather than
    reporting as wrong, and gate 4 requires each to be settled with a stated
    reason before it will pass.
    """
    listing = client.get(f"/api/projects/{project_id}/validation-results", headers=auth)
    assert listing.status_code == 200, listing.text
    acts: list[str] = []

    for row in listing.json():
        if row["is_waived"] or row["severity"] not in WAIVABLE:
            continue
        waiver = client.post(
            f"/api/validation-results/{row['id']}/waiver",
            headers=auth,
            json={
                "confirm": True,
                "reason": (
                    f"{row['code']} at {row['object_label']} is a defect this upgrade "
                    "inherits from the existing installation, and the documents the "
                    "customer supplied do not resolve it. Raised with the customer, "
                    "carried knowingly into this revision, and scheduled into the "
                    "upgrade scope."
                ),
                "expected_updated_at": row["updated_at"],
            },
        )
        assert waiver.status_code == 200, waiver.text
        body = waiver.json()
        assert body["result"]["is_waived"] is True, body
        acts.append(f"carried validation risk: {row['code']} at {row['object_label']}")
    return acts


def _run_project(
    client: TestClient,
    settings: Settings,
    *,
    project_id: uuid.UUID,
    engineer_auth: dict[str, str],
    reviewer_auth: dict[str, str],
) -> tuple[int, list[str], list[str]]:
    """Work, answer, decide, carry, repeat — until nothing is waiting on a human.

    The remediation comes last in a round and only when nothing else moved.
    ``or approved`` in step 12 of the brief is the last resort, not the first: a
    reviewer who waived on sight would be signing for results that answering the
    open questions would have cleared by itself.
    """
    acts: list[str] = []
    stops: list[str] = []
    rounds = 0
    for _ in range(MAX_ROUNDS):
        rounds += 1
        worker.drain(settings)
        this_round = _resolve_clarifications(client, project_id, engineer_auth)
        this_round += _decide_requirements(client, project_id, reviewer_auth)
        this_round += _decide_approvals(client, project_id, reviewer_auth)
        if not this_round:
            stops.extend(_validation_stops(client, project_id, reviewer_auth))
            this_round += _carry_the_remaining_risk(client, project_id, reviewer_auth)
        acts.extend(this_round)
        if not this_round:
            break
    worker.drain(settings)
    return rounds, acts, stops


# ---------------------------------------------------------------------------
# Snapshotting
# ---------------------------------------------------------------------------
def _finding_report(finding: Finding) -> Report:
    locations = tuple(
        link.anchor.display_location
        for link in finding.evidence_links
        if link.anchor is not None and link.anchor.display_location
    )
    parts = (
        finding.title,
        finding.category.value,
        finding.fact,
        finding.inference,
        finding.suggested_review_action,
        finding.limitations,
        *locations,
    )
    return Report(
        source="finding",
        code=finding.category.value,
        severity=finding.severity,
        title=finding.title,
        text=" \n".join(part for part in parts if part).lower(),
        locations=locations,
    )


def _validation_report(result: ValidationResult) -> Report:
    locations = (result.object_label,) if result.object_label else ()
    parts = (result.code, result.message, result.object_label, result.suggested_action)
    return Report(
        source="validation",
        code=result.code,
        severity=VALIDATION_RANK[result.severity],
        title=result.message[:120],
        text=" \n".join(part for part in parts if part).lower(),
        locations=locations,
        is_waived=result.is_waived,
    )


def _snapshot(
    settings: Settings,
    project_id: uuid.UUID,
    rounds: int,
    acts: list[str],
    stops: list[str],
    readiness: Readiness,
) -> Scenario:
    """Freeze what the run produced, as an engineer would read it at the end.

    Findings accumulate across the whole run, so all of them are taken. Only the
    **newest** validation run is: a production run validates several times as
    the model grows, and an earlier run describes a revision that no longer
    exists. Judging the fixture's ground truth against superseded results would
    report defects the project had already moved past.

    Waived results are kept and marked. They are the reason this run gets past
    its safety gates at all, and a snapshot that dropped them would report the
    project as having found fewer defects than it did while also hiding the
    signatures that let it proceed.
    """
    session = db_session.get_session_factory()()
    try:
        project = session.get(Project, project_id)
        assert project is not None
        storage = ProjectStorage(project_id, settings)

        artifacts = tuple(
            Artifact(
                filename=row.original_filename,
                sha256=row.sha256,
                size_bytes=row.size_bytes,
                stored_path=storage.absolute_path(row.relative_path),
            )
            for row in session.execute(
                select(UploadedFile)
                .where(UploadedFile.project_id == project_id)
                .order_by(UploadedFile.created_at)
            ).scalars()
        )

        findings = [
            _finding_report(row)
            for row in session.execute(
                select(Finding).where(Finding.project_id == project_id)
            ).scalars()
        ]

        newest_run = (
            session.execute(
                select(ValidationRun)
                .where(ValidationRun.project_id == project_id)
                .order_by(ValidationRun.created_at.desc(), ValidationRun.id.desc())
            )
            .scalars()
            .first()
        )
        current_results = (
            list(
                session.execute(
                    select(ValidationResult).where(
                        ValidationResult.validation_run_id == newest_run.id
                    )
                ).scalars()
            )
            if newest_run is not None
            else []
        )
        validations = [_validation_report(row) for row in current_results]
        waivers = tuple(
            Waiver(
                code=row.code,
                severity=row.severity,
                object_label=row.object_label,
                reviewer=_display_name(session, row.waived_by_id),
                reason=row.waiver_reason,
            )
            for row in current_results
            if row.is_waived
        )

        open_high = tuple(
            _finding_report(row)
            for row in session.execute(
                select(Finding).where(
                    Finding.project_id == project_id,
                    Finding.severity.in_([FindingSeverity.HIGH, FindingSeverity.CRITICAL]),
                )
            ).scalars()
            if row.requires_approval
        )

        from app.engineering import gates

        gate_records = tuple(
            GateRecord(
                key=row.gate_key,
                status=row.status,
                decision=gates.decision_of(session, row),
                decided_by=_display_name(session, row.decided_by_id),
                comment=row.decision_comment,
                unmet=str(row.unmet_criteria)[:400],
            )
            for row in session.execute(
                select(ApprovalGate).where(ApprovalGate.project_id == project_id)
            ).scalars()
        )

        candidates = list(
            session.execute(
                select(ReleaseCandidate).where(ReleaseCandidate.project_id == project_id)
            ).scalars()
        )
        candidate_artifacts: tuple[PackagedArtifact, ...] = ()
        limitations = ""
        for candidate in candidates:
            for item in candidate.artifacts:
                candidate_artifacts += (
                    PackagedArtifact(
                        kind=item.kind,
                        relative_path=item.relative_path,
                        validation_level=item.validation_level,
                        sha256=item.sha256,
                        size_bytes=item.size_bytes,
                        import_order=item.import_order,
                        stored_path=storage.absolute_path(item.relative_path),
                    ),
                )
                if item.kind is ExportArtifactKind.LIMITATIONS:
                    limitations = _read_artifact(storage, item.relative_path)

        return Scenario(
            settings=settings,
            project_id=project_id,
            project_status=project.status,
            artifacts=artifacts,
            reports=tuple(findings + validations),
            clarifications=tuple(
                row.question
                for row in session.execute(
                    select(Clarification).where(Clarification.project_id == project_id)
                ).scalars()
            ),
            project_questions=tuple(
                row.question
                for row in session.execute(
                    select(ProjectQuestion).where(ProjectQuestion.project_id == project_id)
                ).scalars()
            ),
            requirements=tuple(
                f"{row.reference}: {row.title} — {row.statement}"
                for row in session.execute(
                    select(Requirement).where(Requirement.project_id == project_id)
                ).scalars()
            ),
            tags=tuple(
                row.name
                for row in session.execute(
                    select(Tag).where(Tag.project_id == project_id)
                ).scalars()
            ),
            programs=tuple(
                row.name
                for row in session.execute(
                    select(Program).where(Program.project_id == project_id)
                ).scalars()
            ),
            routines=tuple(
                row.name
                for row in session.execute(
                    select(Routine).where(Routine.project_id == project_id)
                ).scalars()
            ),
            rung_count=len(
                list(
                    session.execute(
                        select(LogicRung).where(LogicRung.project_id == project_id)
                    ).scalars()
                )
            ),
            tasks=tuple(
                (row.kind, row.status, (row.blocked_reason or row.error_message or "")[:300])
                for row in session.execute(
                    select(Task).where(Task.project_id == project_id).order_by(Task.priority)
                ).scalars()
            ),
            gate_records=gate_records,
            open_high_severity=open_high,
            waivers=waivers,
            validation_stops=tuple(stops),
            readiness=readiness,
            candidate_count=len(candidates),
            candidate_levels=tuple(item.validation_level for item in candidates),
            candidate_import_verification=tuple(
                (item.import_verification_status, item.import_verification_detail)
                for item in candidates
            ),
            candidate_artifacts=candidate_artifacts,
            limitations_text=limitations,
            rounds_taken=rounds,
            human_acts=tuple(acts),
        )
    finally:
        session.close()


def _display_name(session: Session, user_id: uuid.UUID | None) -> str:
    """Who decided it. An unnamed signature is the thing the gates exist to stop."""
    if user_id is None:
        return ""
    user = session.get(User, user_id)
    return user.display_name if user is not None else ""


def _read_artifact(storage: ProjectStorage, relative_path: str) -> str:
    try:
        return storage.absolute_path(relative_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def _read_readiness(client: TestClient, project_id: uuid.UUID, auth: dict[str, str]) -> Readiness:
    """What the product promises about the package it has not built yet."""
    response = client.get(f"/api/projects/{project_id}/release-readiness", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    return Readiness(
        is_ready=bool(body["is_ready"]),
        blocking_codes=tuple(str(item["code"]) for item in body["blocking"]),
        blocking_detail="; ".join(
            f"{item['code']}: {item['summary']} {item['detail']}" for item in body["blocking"]
        ),
        outstanding_gates=tuple(str(item) for item in body["outstanding_gates"]),
        validation_error_count=int(body["validation_error_count"]),
        highest_attainable_validation_level=ValidationLevel(
            body["highest_attainable_validation_level"]
        ),
        import_verification_possible=bool(body["import_verification_possible"]),
        import_verification_detail=str(body["import_verification_detail"]),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _settings(root: Path, name: str) -> Settings:
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        environment="test",
        database_url=f"sqlite+pysqlite:///{(root / name).as_posix()}",
        data_dir=data_dir,
        log_json=False,
        log_level="WARNING",
        worker_enabled=False,
        bootstrap_demo_users=False,
        jwt_secret="line4-acceptance-secret-not-used-outside-tests",
    )


def _client(settings: Settings) -> TestClient:
    from app.main import create_app

    application = create_app(settings, run_bootstrap=False)
    application.dependency_overrides[get_settings] = lambda: settings
    return TestClient(application)


def _create_project(client: TestClient, auth: dict[str, str], reviewer_id: uuid.UUID) -> uuid.UUID:
    created = client.post(
        "/api/projects",
        headers=auth,
        json={
            "name": PROJECT_NAME,
            "description": (
                "Acceptance scenario built from fixtures/line4_packaging_conveyor. "
                "Synthetic data only."
            ),
            "client_reference": "LINE4-ACCEPT-001",
        },
    )
    assert created.status_code == 201, created.text
    project_id = uuid.UUID(created.json()["id"])

    member = client.post(
        f"/api/projects/{project_id}/members",
        headers=auth,
        json={"user_id": str(reviewer_id), "role": ProjectRole.REVIEWER.value},
    )
    assert member.status_code in {200, 201}, member.text
    return project_id


@pytest.fixture(scope="module")
def scenario(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Scenario]:
    """The whole acceptance run, once, shared by every assertion in this file.

    Module-scoped because the run costs several seconds and re-running it per
    assertion would buy nothing: the snapshot is frozen, so no test can see
    another's writes. It builds its own engine and settings rather than taking
    conftest's function-scoped ones, which would be torn down between tests.
    """
    root = tmp_path_factory.mktemp("line4-acceptance")
    settings = _settings(root, "line4.sqlite3")
    engine = db_session.build_engine(settings)
    Base.metadata.create_all(engine)
    db_session.configure(engine)
    try:
        # The engineer is identified by the account they sign in as; only the
        # reviewer's id is needed directly, to add them to the project.
        _, reviewer_id = _bootstrap(settings)
        with _client(settings) as client:
            engineer_auth = _token(client, "line4.engineer@test.internal")
            reviewer_auth = _token(client, "line4.reviewer@test.internal")

            project_id = _create_project(client, engineer_auth, reviewer_id)
            _upload(client, project_id, engineer_auth)

            profile = client.put(
                f"/api/projects/{project_id}/engineering-profile",
                headers=engineer_auth,
                json=_intake_payload(),
            )
            assert profile.status_code == 200, profile.text

            run = client.post(f"/api/projects/{project_id}/runs", headers=engineer_auth, json={})
            assert run.status_code == 201, run.text

            rounds, acts, stops = _run_project(
                client,
                settings,
                project_id=project_id,
                engineer_auth=engineer_auth,
                reviewer_auth=reviewer_auth,
            )
            readiness = _read_readiness(client, project_id, reviewer_auth)
        yield _snapshot(settings, project_id, rounds, acts, stops, readiness)
    finally:
        db_session.reset()
        engine.dispose()


# ---------------------------------------------------------------------------
# The scenario, step by step
# ---------------------------------------------------------------------------
def test_the_engineer_signs_in_creates_the_project_and_uploads_eight_artifacts(
    scenario: Scenario,
) -> None:
    """Steps 1 to 3. Everything after this depends on these eight files."""
    names = {item.filename for item in scenario.artifacts}

    assert names == {filename for filename, _ in ARTIFACTS}, f"the project holds {sorted(names)}"
    assert len(scenario.artifacts) == 8


def test_every_artifacts_recorded_hash_matches_the_bytes_on_disk(scenario: Scenario) -> None:
    """A hash nobody re-computes is a hash that proves nothing.

    The whole traceability claim rests on this: a finding cites an anchor, the
    anchor cites a file, and the file is only the file the customer sent if the
    bytes still hash to what intake recorded.
    """
    mismatches: list[str] = []
    for item in scenario.artifacts:
        assert item.stored_path.is_file(), f"{item.filename} is not on disk"
        data = item.stored_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != item.sha256 or len(data) != item.size_bytes:
            mismatches.append(
                f"{item.filename}: recorded {item.sha256[:12]}/{item.size_bytes}, "
                f"on disk {digest[:12]}/{len(data)}"
            )
        source = FIXTURES / item.filename
        if source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            mismatches.append(f"{item.filename}: stored bytes differ from the fixture's")

    assert not mismatches, "; ".join(mismatches)


def test_requirements_are_extracted_and_at_least_one_clarification_is_raised(
    scenario: Scenario,
) -> None:
    """Steps 4 and 5. The refusal to guess, expressed as a question.

    Asserted on ``Clarification`` and not on :attr:`Scenario.questions_raised`,
    and that distinction is the whole content of the test. The two tables are
    different questions put to different people: a ``ProjectQuestion`` is project
    scope — which controller family, which naming standard — while a
    clarification hangs off the one requirement, device or sentence that can be
    read two ways. What this document set plants is a *local* ambiguity, so the
    question it has to provoke is a clarification. Widening the assertion to the
    ten project questions the run does raise would let "which accepted
    requirement do the tags for MTR401_SPEED_REF come from" stand in for having
    read the customer's brief.
    """
    assert scenario.requirements, "nothing was extracted from eight engineering documents"
    assert scenario.clarifications, (
        "the run reached generation without asking a single question about a "
        "document set that contains a deliberate ambiguity"
    )
    assert any(act.startswith("resolved clarification:") for act in scenario.human_acts), (
        "no clarification was answered by a human; the run cannot have been gated on one"
    )


def test_the_dependency_plan_is_built(scenario: Scenario) -> None:
    """Step 6. A production run plans the whole chain up front, not step by step."""
    planned = {kind for kind, _, _ in scenario.tasks}
    for kind in (
        TaskKind.EXTRACT_REQUIREMENTS,
        TaskKind.RESOLVE_CLARIFICATIONS,
        TaskKind.DESIGN_TAGS,
        TaskKind.DESIGN_PLC_ARCHITECTURE,
        TaskKind.GENERATE_LOGIC,
        TaskKind.VALIDATE_CROSS_DISCIPLINE,
        TaskKind.EVALUATE_APPROVAL_GATE,
        TaskKind.DELIVERY,
        TaskKind.GENERATE_RELEASE_CANDIDATE,
        TaskKind.VERIFY_IMPORT,
        TaskKind.ASSEMBLE_PACKAGE,
        TaskKind.FINALIZE_PROJECT,
    ):
        assert kind in planned, f"the production plan is missing {kind.value}"

    gate_tasks = [item for item in scenario.tasks if item[0] is TaskKind.EVALUATE_APPROVAL_GATE]
    assert len(gate_tasks) == len(ApprovalGateKey), (
        "every gate has to be a task, or the run walks past the ones that are missing"
    )


def test_canonical_tags_are_proposed_from_the_source_documents(scenario: Scenario) -> None:
    """Step 7. The controller's tags reach the canonical model, named as written."""
    assert len(scenario.tags) >= 38, f"only {len(scenario.tags)} tags reached the model"
    for expected in ("ESTOP_OK", "CYL401_SOL", "CYL401_EXT_PROX", "MTR403_RUN_CMD"):
        assert expected in scenario.tags, f"{expected} is not in the canonical tag set"


def test_program_routine_and_rung_ir_exists(scenario: Scenario) -> None:
    """Step 9. The base export becomes logic the platform can reason about."""
    assert "Line4_Control" in scenario.programs, f"programs are {sorted(scenario.programs)}"
    for routine in ("Conveyor_Control", "Diverter_Control", "Permissives", "Alarms"):
        assert routine in scenario.routines, f"routines are {sorted(scenario.routines)}"
    assert scenario.rung_count == 24, (
        f"the README states 24 rungs; the model holds {scenario.rung_count}"
    )


def test_the_delivery_plugins_are_installed(scenario: Scenario) -> None:
    """Step 10. Five delivery agents and twenty-one report providers, discovered."""
    registry = get_plugin_registry()

    assert not registry.errors, [f"{item.module}: {item.message}" for item in registry.errors]
    assert len(registry.delivery_agents) == 5, sorted(registry.delivery_agents)
    assert len(registry.report_providers) == 21, len(registry.report_providers)


def test_senior_review_ran_and_every_finding_was_reviewed(scenario: Scenario) -> None:
    """Step 11. Nothing reaches a human decision without an independent pass."""
    senior = [item for item in scenario.tasks if item[0] is TaskKind.SENIOR_REVIEW]

    assert senior, "no senior review was planned"
    assert any(status is TaskStatus.COMPLETED for _, status, _ in senior), (
        f"senior review never completed: {senior}"
    )
    assert scenario.findings, "a defective document set produced no findings at all"


def test_cross_discipline_validation_blocks_the_release_until_a_human_decides(
    scenario: Scenario,
) -> None:
    """Step 12, both halves of it: "resolved **or approved**".

    The stop is still the point, and it is asserted from the sentence the run was
    parked on rather than from the task row. Waiving the last blocker completes
    that task and clears ``blocked_reason``, so by the end there is nothing on
    the row to show it ever stopped — which is exactly why the stop is captured
    live, mid-run, by :func:`_validation_stops`.

    What replaces the old task-status assertion is stronger, because a waiver
    cannot exist by accident. :func:`app.core.services.validation_review.waive`
    refuses any result that neither blocks a gate nor requires a human decision,
    so a recorded waiver over an error-severity result *is* the proof that
    validation stopped the release and that a named person had to act before it
    moved.
    """
    assert scenario.validation_stops, (
        "cross-discipline validation never parked the run in a document set with "
        "seven planted defects"
    )
    assert any("Local:1:I.Data.10" in reason for reason in scenario.validation_stops), (
        "validation parked the run without naming the duplicated channel:\n"
        + "\n".join(f"  {reason}" for reason in scenario.validation_stops)
    )
    assert scenario.carried_errors, (
        "no error-severity result was carried, so nothing was ever decided rather "
        f"than merely found. Reported:\n{_explain(scenario)}"
    )
    assert not scenario.blocking_errors, (
        "the run reached its end state with release-blocking errors neither fixed nor "
        "waived:\n" + "\n".join(f"  {report}" for report in scenario.blocking_errors)
    )


def test_accepting_a_requirement_does_not_invalidate_its_own_approval(
    scenario: Scenario,
) -> None:
    """A human decision must not read as tampering with the thing it decided.

    An approved object is protected so that an engineer's signature stays
    attached to what they read. When the act of signing is itself recorded as a
    later edit, every accepted requirement carries a name against content
    nobody changed, and the release stops for a reason that never happened.

    This was a race and is no longer one: ``ApprovalIntegrityValidator`` compares
    ``object_revision`` against the ``approved_object_revision`` the decision
    recorded, so the verdict no longer depends on where two reads of the same
    clock landed inside one flush. Unmarked deliberately — a run that passes
    intermittently proved nothing, and this one has to pass every time.

    Scanned over :attr:`Scenario.judged` and not over the unwaived blockers.
    This run waives whatever it cannot fix, so scoping it to what still blocks
    would let the bug come back and pass, hidden behind the reviewer's signature.
    """
    self_invalidated = [
        report for report in scenario.judged if report.code == "cross.approved_object_modified"
    ]

    assert not self_invalidated, "\n".join(f"  {report}" for report in self_invalidated)


def test_a_high_severity_interlock_finding_halts_the_project(scenario: Scenario) -> None:
    """Step 13, and the safety property underneath it.

    A high-severity interlock claim must stop the run in front of a person. The
    project may be blocked, or awaiting review, or parked in an engineering
    phase — what it may never be is finished.
    """
    assert scenario.open_high_severity, (
        "no high-severity finding required a human decision, so nothing was gated on one"
    )
    assert scenario.project_status is not ProjectStatus.COMPLETED, (
        "the project completed with a high-severity interlock finding outstanding"
    )


# ---------------------------------------------------------------------------
# The seven planted issues
# ---------------------------------------------------------------------------
def _matching(
    scenario: Scenario, *subjects: str, severity: FindingSeverity, codes: tuple[str, ...] = ()
) -> list[Report]:
    return [
        report
        # Positive planted-issue checks must see review-level validation too.
        # ``scenario.judged`` deliberately contains only findings and release
        # blockers for the false-positive controls, so using it here hid a
        # correctly persisted WARNING (mapped to MEDIUM) for an unresolved HMI
        # read binding.
        for report in scenario.reports
        if report.names(*subjects)
        and report.at_least(severity)
        and (not codes or any(code in report.code for code in codes))
    ]


def _explain(scenario: Scenario) -> str:
    return "\n".join(f"  {report}" for report in scenario.judged) or "  (nothing was reported)"


def test_planted_issue_1_the_missing_accumulation_interlock_is_reported(
    scenario: Scenario,
) -> None:
    """ILK-404 / PE402_BLOCKED: documented, wired, implemented nowhere. High."""
    matches = _matching(
        scenario,
        "pe402_blocked",
        "pe402",
        severity=FindingSeverity.HIGH,
        codes=("missing_logic", "interlock_discrepancy", "interlock_not_implemented"),
    )

    assert matches, (
        "no high-severity report says the accumulation-zone-full interlock is "
        f"missing from the logic. Reported instead:\n{_explain(scenario)}"
    )
    assert any(report.names("ilk-404") for report in matches), (
        "the absence was reported without citing the matrix row that documents it"
    )


def test_planted_issue_2_the_duplicate_io_assignment_is_reported(scenario: Scenario) -> None:
    """Two safety-relevant devices on one physical channel. High."""
    matches = _matching(
        scenario,
        "local:1:i.data.10",
        severity=FindingSeverity.HIGH,
    )

    assert matches, (
        "no high-severity report says Local:1:I.Data.10 carries two devices. "
        f"Reported instead:\n{_explain(scenario)}"
    )
    assert any(
        report.names("gd401_closed") and report.names("mtr403_fault") for report in matches
    ), "the duplicate was reported without naming both devices that claim the channel"


def test_planted_issue_3_the_inconsistent_run_command_name_is_reported(
    scenario: Scenario,
) -> None:
    """MTR401_RUN_CMD in the I/O list, CV401_Run_Output in the logic.

    Found — by the deterministic logic validator rather than by a review agent,
    which is the product working: the two names do not normalise together, so
    what identifies them as one signal is the terminal they share, and that is a
    model-level comparison rather than a textual one.
    """
    matches = _matching(scenario, "mtr401_run_cmd", severity=FindingSeverity.LOW)

    assert matches, (
        "nothing reported that the CV401 run command carries two names. "
        f"Reported instead:\n{_explain(scenario)}"
    )
    # An engineer acts on the location, not the sentence. A report that names
    # the signal but cites nowhere is one nobody can check.
    cited = [location for report in matches for location in report.locations]
    assert any("MTR401_RUN_CMD" in location for location in cited), (
        f"the mismatch was reported but cites nowhere an engineer can follow: {cited}"
    )


def test_planted_issue_3_is_not_inflated_into_a_release_blocker(scenario: Scenario) -> None:
    """A naming inconsistency must not outrank a missing protection.

    Was ``xfail``: the run raised ``logic.output_without_driving_rung`` at ERROR,
    which said the output could never energise. It can — the rung drives it under
    the name the logic uses — so the claim was false as well as release-blocking.
    ``OutputCoverageValidator`` now separates the two cases and this one is
    ``logic.output_name_mismatch`` at WARNING, which is where the README's
    low-to-medium rating puts it.
    """
    inflated = [
        report
        for report in scenario.blocking_errors
        if report.names("mtr401_run_cmd", "cv401_run_output")
    ]

    assert not inflated, "; ".join(str(report) for report in inflated)


def test_planted_issue_4_the_alarm_that_can_never_annunciate_is_reported(
    scenario: Scenario,
) -> None:
    """ALM_CYL402_FAIL: defined, declared, bound, never written. Medium."""
    matches = _matching(scenario, "alm_cyl402_fail", severity=FindingSeverity.MEDIUM)

    assert matches, (
        "no report of medium severity or above says the CV404 stop-gate alarm can never "
        f"annunciate. Reported instead:\n{_explain(scenario)}"
    )


def test_planted_issue_5_the_hmi_binding_to_a_missing_tag_is_reported(
    scenario: Scenario,
) -> None:
    """``{::[Line4]Accum_Case_Count}`` resolves against nothing. Medium."""
    matches = _matching(scenario, "accum_case_count", severity=FindingSeverity.MEDIUM)

    assert matches, (
        "no report says num_CasesAccumulated is bound to a tag the controller does not "
        f"have. Reported instead:\n{_explain(scenario)}"
    )


def test_planted_issue_6_the_ambiguous_accumulation_release_is_raised_with_the_customer(
    scenario: Scenario,
) -> None:
    """Two defensible readings, and the product must ask rather than choose.

    Found by the clarification the deterministic provider raises for a passage a
    *companion* document records as unsettled: ``Functional_Description.docx``
    section 8 says the release behaviour asked for in section 3 of the project
    description has not been settled, and that sentence names both the document
    and the section, so the passage it defers can be quoted rather than guessed
    at. The question states that the problem is missing information — neither
    document writes the two readings down, and a rule that composed them would be
    inventing the requirement it is asking about.
    """
    asked = [
        question
        for question in scenario.questions_raised
        if "accumulat" in question.lower() and "releas" in question.lower()
    ]
    reported = _matching(
        scenario,
        "release the accumulated cases",
        "accumulation release",
        severity=FindingSeverity.MEDIUM,
        codes=("documentation_gap", "ambiguous"),
    )

    assert asked or reported, (
        "nothing asked which reading of the accumulation release applies. Questions "
        "raised:\n" + ("\n".join(f"  {item}" for item in scenario.questions_raised) or "  (none)")
    )


def test_planted_issue_7_the_correct_cyl401_diverter_produces_no_findings(
    scenario: Scenario,
) -> None:
    """The most important of the seven: over-reach is as wrong as under-reach."""
    fired = [
        report for report in scenario.judged if report.names("cyl401", "ilk-403", "alm-401-06")
    ]

    assert not fired, "findings raised against a correctly implemented device:\n" + "\n".join(
        f"  {report}" for report in fired
    )


# ---------------------------------------------------------------------------
# The eleven decoys
# ---------------------------------------------------------------------------
#: The decoys this build currently reports. Kept as a separate list rather than
#: a flag on each entry so that the eleven below read as the README's own list,
#: unedited: what the fixture forbids is permanent, and which of them a given
#: build fires on is not. Deleting a name here is how a fix gets recorded.
_DECOY_FIRES_TODAY: tuple[str, ...] = ()

#: Each entry is ``(id, subjects, statement)``, in the README's own order. A
#: decoy fires when any finding or release-blocking error names one of its
#: subjects; the subjects are the words a report about that decoy would have to
#: use, and no others.
DECOYS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "cyl401",
        ("cyl401", "ilk-403"),
        "Anything about CYL401. It is implemented consistently end to end.",
    ),
    (
        "ilk_405_missing_from_the_base_export",
        ("ilk-405", "low plant air pressure"),
        "ILK-405 is scoped 'New (this upgrade)'. The base export is not supposed to have it.",
    ),
    (
        "ilk_406_missing_from_the_base_export",
        ("ilk-406", "case jam at the cv402"),
        "ILK-406 is scoped 'New (this upgrade)' for the same reason.",
    ),
    (
        "air_press_psi_has_no_controller_tag",
        ("air_press_psi",),
        "A new point for a new interlock; the 5069-IF8 in slot 3 is described as spare.",
    ),
    (
        "unimplemented_scope_of_work",
        ("jog", "pressure transmitter", "jam detection"),
        "The jog request, the transmitter and the jam detection are the scope of work.",
    ),
    (
        "automode_written_but_never_read",
        ("automode",),
        "A mode indication bit, bound on Line4_Overview as ind_ModeAuto.",
    ),
    (
        "ilk_401_and_ilk_402",
        ("ilk-401", "ilk-402"),
        "Both are documented and implemented consistently and drop LINE_PERMISSIVE.",
    ),
    (
        "ilk_407",
        ("ilk-407",),
        "All three drive fault contacts are in one rung, each with an alarm rung.",
    ),
    (
        "cause_and_effect_scope_cells",
        ("new alarm to be added",),
        "Those cells mark scope, not a missing alarm definition.",
    ),
    (
        "approved_template_identifiers",
        ("esco-tpl-",),
        "Approved_Templates.txt records what the customer approved; it is an input.",
    ),
    (
        "runtime_or_firmware_behaviour",
        ("firmware", "majorrev", "minorrev", "downloaded to the controller"),
        "An L5X is an offline export and says nothing about what a controller runs.",
    ),
)


def _decoy_params() -> list[Any]:
    params: list[Any] = []
    for key, subjects, statement in DECOYS:
        marks = (
            [
                pytest.mark.xfail(
                    strict=True,
                    reason=(
                        f"Decoy '{key}' fires. The README states: {statement} The cause the "
                        "six original entries shared — the Cause and Effect worksheet read "
                        "as seven more interlocks, and a 'New (this upgrade)' scope read as a "
                        "current one — is fixed, so a name reappearing here is a new cause "
                        "and the run that put it back has to be diagnosed afresh."
                    ),
                )
            ]
            if key in _DECOY_FIRES_TODAY
            else []
        )
        params.append(pytest.param(subjects, statement, id=key, marks=marks))
    return params


@pytest.mark.parametrize(("subjects", "statement"), _decoy_params())
def test_no_decoy_is_reported(
    scenario: Scenario, subjects: tuple[str, ...], statement: str
) -> None:
    """None of the eleven plausible non-defects may be reported.

    Asserted one decoy at a time so a failure names the one that fired. A review
    that flags everything scores as well as one that is right on the positives
    alone; these eleven are the only thing that tells them apart.
    """
    fired = [report for report in scenario.judged if report.names(*subjects)]

    assert not fired, f"{statement}\nReported anyway:\n" + "\n".join(
        f"  {report}" for report in fired
    )


# ---------------------------------------------------------------------------
# Gates, release and the end state
# ---------------------------------------------------------------------------
#: Why every package assertion below is still unreached. Written once because
#: they all fail for the identical reason and a reader comparing five paraphrases
#: of it would spend the effort deciding whether they said the same thing. The
#: full diagnosis is on
#: :func:`test_generators_assemble_the_package_and_a_release_candidate_is_sealed`.
_NO_CANDIDATE = (
    "No release candidate exists to assert against. Gate 2 now passes on a carried risk, but "
    "design_plc_architecture parks permanently on the deterministic MockProvider, which "
    "refuses to choose which approved template implements a requirement no LogicNetwork "
    "claims. Nothing downstream of generate_logic ever runs. Full diagnosis on "
    "test_generators_assemble_the_package_and_a_release_candidate_is_sealed."
)


def test_a_gate_decision_has_an_http_door(scenario: Scenario) -> None:
    """The six gates are reachable through an exact gate/request HTTP binding."""
    from app.main import create_app

    application = create_app(scenario.settings, run_bootstrap=False)
    # FastAPI 0.116+ keeps included routers lazy, so ``application.routes``
    # contains ``_IncludedRouter`` placeholders until route resolution.  The
    # generated OpenAPI paths are the stable public surface this test means to
    # inspect.
    paths = set(application.openapi()["paths"])
    expected = "/api/approval-gates/{gate_id}/approvals/{approval_id}/decision"
    assert expected in paths, f"{expected} is absent from {len(paths)} paths"


def test_a_human_approves_gate_two(scenario: Scenario) -> None:
    """Step 8. Devices, I/O and tags, signed off by a named person.

    Reached only because the reviewer settled the channel conflict. The gate's
    ``no_open_tag_or_io_errors`` criterion counts *unwaived* I/O errors, so
    ``Local:1:I.Data.10`` held this gate shut until somebody put their name to
    carrying it — which is the gate working, not a loophole in it: the criterion
    is recomputed from the model on every read, so there is no way to reach a
    decision here with the conflict neither fixed nor decided.
    """
    assert ApprovalGateKey.DEVICES_IO_TAGS in scenario.gates_decided, (
        "gate 2 was never passed:\n" + scenario.explain_gates()
    )
    gate = scenario.gate(ApprovalGateKey.DEVICES_IO_TAGS)
    assert gate.decided_by, "gate 2 passed without recording who decided it"
    assert gate.comment.strip(), "gate 2 passed without a covering comment"


def test_a_release_on_accepted_risk_stays_distinguishable_from_a_clean_one(
    scenario: Scenario,
) -> None:
    """The most important assertion in this file.

    ``APPROVE`` and ``ACCEPT_RISK`` both leave a gate ``PASSED``, because both let
    the work proceed. If that were the whole record, a delivery that went out
    over a double-booked safety input would be indistinguishable, six months
    later, from one that went out clean — and a release record that cannot tell
    them apart is worse than no release record, because it reassures.

    Three places keep the distinction, and all three are asserted here because
    any one of them alone can be lost to a refactor: the gate is ``PASSED`` while
    its approval carries ``ACCEPT_RISK`` (:func:`app.engineering.gates.decision_of`
    is the product's own way of asking); the decision carries the mandatory
    comment that ``accept_risk`` obliges; and every waived result names the
    reviewer who carried it and states why.

    The negative half matters as much. Gate 1 was decided before anything was
    waived and is recorded as a plain ``APPROVE``, so this asserts a distinction
    the run actually makes rather than a label it applies to everything.
    """
    carried = scenario.gate(ApprovalGateKey.DEVICES_IO_TAGS)

    assert carried.status is GateStatus.PASSED, scenario.explain_gates()
    assert carried.decision is ApprovalDecision.ACCEPT_RISK, (
        f"the gate that proceeded over {len(scenario.waivers)} waived validation result(s) "
        f"is recorded as {carried.decision.value if carried.decision else 'undecided'}, "
        "which reads afterwards as a release with nothing carried"
    )
    assert carried.comment.strip(), (
        "accept_risk without a comment is a signature with no statement behind it"
    )
    assert "Local:1:I.Data.10" in carried.comment, (
        f"the accepted risk is not named in what was signed: {carried.comment!r}"
    )

    clean = scenario.gate(ApprovalGateKey.PROJECT_PROFILE)
    assert clean.status is GateStatus.PASSED, scenario.explain_gates()
    assert clean.decision is ApprovalDecision.APPROVE, (
        "the gate decided before anything was waived is not recorded as a plain "
        f"approval but as {clean.decision.value if clean.decision else 'undecided'}, "
        "so 'accepted risk' has stopped meaning anything"
    )

    assert scenario.waivers, "nothing was carried, so this asserts nothing"
    unattributed = [item for item in scenario.waivers if not item.reviewer or not item.reason]
    assert not unattributed, (
        "a carried risk with no named reviewer or no stated reason:\n"
        + "\n".join(f"  {item}" for item in unattributed)
    )
    assert all("Line 4 Senior Reviewer" == item.reviewer for item in scenario.waivers), (
        "a waiver was recorded against somebody other than the reviewer:\n"
        + "\n".join(f"  {item}" for item in scenario.waivers)
    )


def test_carried_risk_is_still_counted_and_no_longer_blocks(scenario: Scenario) -> None:
    """The other half of "resolved **or approved**", read at the release door.

    Two claims that pull in opposite directions, which is the point. The carried
    errors are still *counted* — ``validation_error_count`` is the total the run
    found, and it is the number the package manifest records, so a project that
    reported zero here after a reviewer carried three would look afterwards
    exactly like one that never had any. And they no longer *block*: an error a
    named reviewer decided has been resolved by decision, which is what gates 2 to
    4 already assumed and what the release door did not.

    This is the product change this session made, and it was not reachable from a
    test before it: nothing had ever waived a Line 4 validation result, so nothing
    had ever asked the release door what it thought of one. ``release_readiness``
    read ``ValidationRun.error_count``, which is computed when the run finishes
    and counts waived and unwaived errors alike, so a project could pass every
    gate on accepted risk and then be refused a package for the very errors those
    gates had settled — the waiver mechanism blocking the thing it exists to
    permit.

    What is left in the way is only the two undecided gates, and those are
    undecided because ``design_plc_architecture`` is parked; see
    :data:`_NO_CANDIDATE`.
    """
    assert scenario.readiness.validation_error_count >= len(
        [item for item in scenario.waivers if item.severity is ValidationSeverity.ERROR]
    ), (
        "readiness reports fewer errors than were carried, so the carried ones have "
        f"vanished from the record: {scenario.readiness.validation_error_count} reported, "
        f"{len(scenario.waivers)} waived"
    )
    assert "validation_errors" not in scenario.readiness.blocking_codes, (
        "every error-severity result carries a named reviewer's waiver and the release "
        "door still refuses on them:\n  " + scenario.readiness.blocking_detail
    )
    assert scenario.readiness.blocking_codes == ("gate_undecided",), (
        "something other than the open gates is holding the release:\n  "
        + scenario.readiness.blocking_detail
    )


def test_the_platform_never_claims_more_than_it_can_prove_offline(
    scenario: Scenario,
) -> None:
    """Where a product like this lies to itself first.

    Asserted on ``GET /release-readiness`` rather than on a candidate, because
    the promise is made before anything is generated and that is precisely when
    it is cheapest to overstate. Three claims, and each is a place the honest
    answer is the unflattering one: the ceiling is ``ROUND_TRIP_PARSED`` and not
    ``ROCKWELL_IMPORT_VERIFIED``; import verification is impossible here rather
    than merely un-run; and the impossibility comes with the sentence that says
    what would change it.
    """
    assert scenario.readiness.highest_attainable_validation_level is MAX_OFFLINE_VALIDATION_LEVEL, (
        "the platform claims it can reach "
        f"{scenario.readiness.highest_attainable_validation_level.value} with no licensed "
        "Rockwell workstation configured"
    )
    assert scenario.readiness.import_verification_possible is False, (
        "import verification is reported as possible on a machine with no licensed "
        "workstation adapter"
    )
    assert scenario.readiness.import_verification_detail.strip(), (
        "import verification is impossible and the product does not say why, which is a "
        "blank an engineer reads as a pass"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Gates 1 and 2 now pass and the release door no longer refuses the carried errors, "
        "so what stops the run is a step further on and is not the fixture's fault. "
        "design_plc_architecture parks permanently on the deterministic MockProvider. "
        "app.agents.base.providers.mock._build_plc_logic asks which approved template "
        "implements every accepted requirement that no LogicNetwork claims, and refuses to "
        "choose one itself: 'matching one to a requirement is the judgement this provider "
        "does not make'. This project has no LogicNetwork rows at all — the L5X importer "
        "records controller tasks, programs, routines, rungs, instructions and operands, "
        "and creates no networks, so nothing claims anything — and all seven accepted "
        "REQ-ILK-401..407 come back as unimplemented. "
        "app.agents.review.handlers._design then blocks, correctly, because a question was "
        "asked and no change set was produced. Answering does not clear it: the question "
        "reference is uuid5 over the task id and the payload, so the requeued task asks the "
        "identical question, the executor returns the already-answered row, and the handler "
        "blocks on the same sentence for ever. Without generate_logic there is no delivery, "
        "no candidate and no package. "
        "backend/tests/orchestration/test_production_workflow.py reaches a sealed candidate "
        "offline only because its fixture seeds a LogicNetwork carrying "
        "requirement_reference='REQ-001' — and says so in a comment. Reaching this offline "
        "needs either a way for a human to record which existing rung implements a "
        "requirement, or a provider that selects a pattern. Neither is a change to this "
        "test, and completing the handler instead would be worse: it would mark a design "
        "task done that designed nothing."
    ),
)
def test_generators_assemble_the_package_and_a_release_candidate_is_sealed(
    scenario: Scenario,
) -> None:
    """Steps 14 and 15. One candidate, carrying files, one of them LIMITATIONS.md."""
    assert scenario.candidate_count == 1, f"{scenario.candidate_count} release candidates"
    assert scenario.candidate_artifacts, "the candidate carries no artifacts"

    kinds = {item.kind for item in scenario.candidate_artifacts}
    assert ExportArtifactKind.LIMITATIONS in kinds, sorted(item.value for item in kinds)
    assert "import-ready candidate" in scenario.limitations_text.lower(), (
        "LIMITATIONS.md does not call the package an import-ready candidate"
    )


@pytest.mark.xfail(
    strict=True,
    reason=_NO_CANDIDATE,
)
def test_every_packaged_artifacts_recorded_hash_matches_the_bytes_on_disk(
    scenario: Scenario,
) -> None:
    """A checksum nobody re-computes is a checksum that proves nothing.

    The customer imports these bytes into a controller. The manifest's whole
    claim is that the file they open is the file that was signed for, and that
    claim is worth exactly one re-computation.
    """
    assert scenario.candidate_artifacts, "there are no packaged artifacts to hash"

    mismatches: list[str] = []
    for item in scenario.candidate_artifacts:
        if not item.stored_path.is_file():
            mismatches.append(f"{item.relative_path}: not on disk")
            continue
        data = item.stored_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != item.sha256 or len(data) != item.size_bytes:
            mismatches.append(
                f"{item.relative_path}: recorded {item.sha256[:12]}/{item.size_bytes}, "
                f"on disk {digest[:12]}/{len(data)}"
            )

    assert not mismatches, "; ".join(mismatches)


@pytest.mark.xfail(
    strict=True,
    reason=_NO_CANDIDATE,
)
def test_the_candidate_claims_the_weakest_level_among_its_artifacts(
    scenario: Scenario,
) -> None:
    """A package is only as proved as its least-proved file.

    Two separate claims, and the second is the one that goes wrong quietly. The
    ceiling is ``ROUND_TRIP_PARSED``: offline we can prove our own output parses
    back into the same model, which is not proving Studio 5000 will import it. And
    rolling a set of files up to one level has to take the **minimum** — a tag CSV
    that round-tripped does not make a partner plugin's HMI export any more
    verified than it was, and taking the maximum would promote the weakest file to
    the strength of the strongest.
    """
    assert scenario.candidate_artifacts, "there is no package to take a level from"
    assert scenario.candidate_levels, "no candidate to compare against its artifacts"

    over_claimed = [
        (item.kind.value, item.relative_path, item.validation_level.value)
        for item in scenario.candidate_artifacts
        if VALIDATION_LEVEL_ORDER[item.validation_level]
        > VALIDATION_LEVEL_ORDER[MAX_OFFLINE_VALIDATION_LEVEL]
    ]
    assert not over_claimed, (
        f"artifacts claim more than this backend can prove offline: {over_claimed}"
    )

    weakest = min(
        VALIDATION_LEVEL_ORDER[item.validation_level] for item in scenario.candidate_artifacts
    )
    for level in scenario.candidate_levels:
        assert VALIDATION_LEVEL_ORDER[level] <= weakest, (
            f"the candidate claims {level.value} while its weakest artifact reached only "
            f"{[item.validation_level.value for item in scenario.candidate_artifacts]}"
        )


@pytest.mark.xfail(
    strict=True,
    reason=_NO_CANDIDATE,
)
def test_the_candidate_reports_import_verification_as_unavailable_with_a_reason(
    scenario: Scenario,
) -> None:
    """``NOT_ATTEMPTED`` and ``UNAVAILABLE`` are not the same admission.

    ``NOT_ATTEMPTED`` is the column default and reads as "we have not got to it
    yet". On a machine with no licensed workstation nobody will ever get to it,
    and the candidate has to say so in its own words rather than leaving a
    default an engineer will read as fine.
    """
    assert scenario.candidate_import_verification, "no candidate to ask"
    for status, detail in scenario.candidate_import_verification:
        assert status is ImportVerificationStatus.UNAVAILABLE, (
            f"import verification is recorded as {status.value} with no licensed "
            "workstation configured"
        )
        assert detail.strip(), "an unavailable verification with no reason is a blank"


@pytest.mark.xfail(
    strict=True,
    reason=_NO_CANDIDATE,
)
def test_the_import_order_puts_data_types_before_the_tags_that_use_them(
    scenario: Scenario,
) -> None:
    """Getting this wrong costs the customer an hour and names nothing.

    Studio 5000 refuses a tag whose UDT it has not seen, and the error it gives
    does not name the missing type. The order is therefore part of the
    deliverable, not a convenience, and it is asserted on ``import_order``
    because that integer is what the manifest and ``IMPORT_ORDER.md`` are both
    rendered from.
    """
    types = [
        item.import_order
        for item in scenario.candidate_artifacts
        if item.kind in {ExportArtifactKind.UDT_L5X, ExportArtifactKind.AOI_L5X}
    ]
    tags = [
        item.import_order
        for item in scenario.candidate_artifacts
        if item.kind is ExportArtifactKind.TAG_CSV
    ]

    assert types, "the package defines no data types, so this asserts nothing"
    assert tags, "the package carries no tag table, so this asserts nothing"
    assert max(types) < min(tags), (
        "tags are imported before the structured types they reference: "
        f"types at {sorted(types)}, tags at {sorted(tags)}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Unreachable for the reason on "
        "test_generators_assemble_the_package_and_a_release_candidate_is_sealed: no "
        "candidate is generated, so there is nothing to be ready to export. Separately, "
        "and worth keeping recorded here: a genuinely finalized archive converges to "
        "COMPLETED, so READY_FOR_EXPORT is the pre-finalization state rather than the "
        "durable post-delivery one, and a run that reached it and stayed there would "
        "itself be worth investigating."
    ),
)
def test_the_project_reaches_ready_for_export(scenario: Scenario) -> None:
    """Step 16. The end state the whole scenario is for."""
    assert scenario.project_status is ProjectStatus.READY_FOR_EXPORT, (
        f"the project finished as {scenario.project_status.value}. Tasks not terminal:\n"
        + "\n".join(
            f"  {kind.value}: {status.value} {reason}"
            for kind, status, reason in scenario.tasks
            if status in {TaskStatus.BLOCKED, TaskStatus.AWAITING_APPROVAL, TaskStatus.FAILED}
        )
    )


def test_the_run_settles_rather_than_looping(scenario: Scenario) -> None:
    """A run that never settles would make every assertion above meaningless."""
    assert scenario.rounds_taken < MAX_ROUNDS, (
        f"the run was still moving after {scenario.rounds_taken} turns of the human loop"
    )
    failed = [
        (kind.value, reason)
        for kind, status, reason in scenario.tasks
        if status is TaskStatus.FAILED
    ]
    assert not failed, f"tasks failed rather than blocking with a reason: {failed}"


# ---------------------------------------------------------------------------
# Step 17: restarting preserves everything
# ---------------------------------------------------------------------------
def test_restarting_preserves_everything(scenario: Scenario) -> None:
    """Step 17. A fresh process sees the run exactly as the old one left it.

    Read through an engine built from scratch against the same database file,
    with no reference to the session the scenario ran on: this is what a
    restarted backend does, and nothing in the run may have lived only in
    memory.
    """
    engine = db_session.build_engine(scenario.settings)
    try:
        with Session(engine) as session:
            project = session.get(Project, scenario.project_id)
            assert project is not None, "the project did not survive a restart"
            assert project.name == PROJECT_NAME
            assert project.status is scenario.project_status

            counts = {
                "files": _count(session, UploadedFile, scenario.project_id),
                "findings": _count(session, Finding, scenario.project_id),
                "requirements": _count(session, Requirement, scenario.project_id),
                "clarifications": _count(session, Clarification, scenario.project_id),
                "project_questions": _count(session, ProjectQuestion, scenario.project_id),
                "tags": _count(session, Tag, scenario.project_id),
                "rungs": _count(session, LogicRung, scenario.project_id),
                "tasks": _count(session, Task, scenario.project_id),
                "gates": _count(session, ApprovalGate, scenario.project_id),
            }

        assert counts["files"] == len(scenario.artifacts)
        assert counts["findings"] == len(scenario.findings)
        assert counts["requirements"] == len(scenario.requirements)
        assert counts["clarifications"] == len(scenario.clarifications)
        assert counts["project_questions"] == len(scenario.project_questions)
        assert counts["tags"] == len(scenario.tags)
        assert counts["rungs"] == scenario.rung_count
        assert counts["tasks"] == len(scenario.tasks)
        assert counts["gates"] == len(ApprovalGateKey)
    finally:
        engine.dispose()


def _count(session: Session, model: Any, project_id: uuid.UUID) -> int:
    return len(list(session.execute(select(model).where(model.project_id == project_id)).scalars()))


@pytest.fixture
def interrupted(tmp_path: Path) -> Iterator[tuple[Settings, uuid.UUID, Engine]]:
    """A Line 4 run whose worker died holding a lease on a task.

    Its own database, because it deliberately leaves a project in a damaged
    state and the shared scenario must not see it. The crash is simulated the
    way ``backend/tests/orchestration/test_worker.py`` simulates it — the lease
    is expired rather than the process actually killed — because a test that
    slept and hoped would prove only that the machine was fast that day.
    """
    settings = _settings(tmp_path / "restart", "restart.sqlite3")
    engine = db_session.build_engine(settings)
    Base.metadata.create_all(engine)
    # ``db_session`` is process-global, and the module-scoped scenario holds it
    # for every other test in this file. Put its engine back rather than
    # resetting to none, so this fixture cannot decide the outcome of a test
    # that merely happens to run after it.
    previous = db_session.get_engine()
    db_session.configure(engine)
    try:
        _, reviewer_id = _bootstrap(settings)
        with _client(settings) as client:
            auth = _token(client, "line4.engineer@test.internal")
            project_id = _create_project(client, auth, reviewer_id)
            _upload(client, project_id, auth)
            profile = client.put(
                f"/api/projects/{project_id}/engineering-profile",
                headers=auth,
                json=_intake_payload(),
            )
            assert profile.status_code == 200, profile.text
            run = client.post(f"/api/projects/{project_id}/runs", headers=auth, json={})
            assert run.status_code == 201, run.text
        yield settings, project_id, engine
    finally:
        db_session.configure(previous)
        engine.dispose()


def test_a_worker_that_dies_mid_run_does_not_strand_the_project(
    interrupted: tuple[Settings, uuid.UUID, Engine],
) -> None:
    """Step 17's other half: a fresh process picks the run back up.

    Without the sweep the task sits in ``running`` for ever and the upgrade
    stops needing a controls engineer and starts needing somebody to notice.
    """
    settings, project_id, _ = interrupted

    session = db_session.get_session_factory()()
    try:
        queue = DatabaseTaskQueue(session, settings)
        claimed = queue.claim("worker-that-died", limit=1)
        assert len(claimed) == 1
        task_id = claimed[0].id
        assert claimed[0].status is TaskStatus.RUNNING
        attempts_before = claimed[0].attempts
        # The process died here; its lease simply ran out.
        claimed[0].lease_expires_at = utcnow() - timedelta(hours=1)
        session.commit()
    finally:
        session.close()

    assert worker.recover(settings) == 1, "the abandoned lease was not swept"

    session = db_session.get_session_factory()()
    try:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status is TaskStatus.QUEUED
        assert task.locked_by is None
        assert task.attempts == attempts_before, "a crash cost more than one attempt"
        assert task.error_code == "lease_expired"
    finally:
        session.close()

    worker.drain(settings)

    session = db_session.get_session_factory()()
    try:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status is TaskStatus.COMPLETED, task.blocked_reason or task.error_message
        parsed = list(
            session.execute(
                select(Task).where(Task.project_id == project_id, Task.kind == TaskKind.PARSE_FILE)
            ).scalars()
        )
        assert len(parsed) == 8, f"{len(parsed)} parse tasks after recovery"
        assert all(row.status is TaskStatus.COMPLETED for row in parsed), (
            "the recovered run stalled instead of parsing the artifacts"
        )
        graph = session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.kind == TaskKind.BUILD_EVIDENCE_GRAPH,
            )
        ).scalar_one()
        assert graph.status is TaskStatus.COMPLETED, (
            "after recovery the run should carry on into the evidence graph, not stall"
        )
    finally:
        session.close()
