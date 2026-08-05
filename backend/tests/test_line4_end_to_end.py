"""The acceptance scenario: Line 4 Packaging Conveyor Upgrade, intake to release.

One project, driven the way an engineer drives it — sign in, create, upload the
eight artifacts, answer what is asked, decide what is raised — and then judged
against ``fixtures/line4_packaging_conveyor/README.md``, which states in advance
which seven defects are planted and which eleven plausible-looking things are
**not** defects.

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
and to **release-blocking validation errors**: those are the two outputs that
cost an engineer time or stop a job. Standing capability disclosures
(``information``, ``unsupported``, ``human_review_required``) are the platform
declaring the limits of what it checked, not claims about this plant, and are
excluded — with the consequence, stated here so it is not hidden, that a defect
reported only at that level counts as **not found**.

Runs offline on the deterministic MockProvider: no network, no model, no cost.
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
    SEVERITY_ORDER,
    VALIDATION_LEVEL_ORDER,
    ApprovalGateKey,
    ApprovalKind,
    ApprovalStatus,
    DocumentRole,
    ExportArtifactKind,
    FindingSeverity,
    GateStatus,
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
from app.models.project import Project
from app.models.release import ReleaseCandidate, ValidationResult, ValidationRun
from app.models.requirements import Clarification, Requirement
from app.models.revision import ApprovalGate
from app.models.tags import Tag
from app.models.task import Task
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

    @property
    def is_finding(self) -> bool:
        return self.source == "finding"

    def names(self, *subjects: str) -> bool:
        return any(subject.lower() in self.text for subject in subjects)

    def at_least(self, severity: FindingSeverity) -> bool:
        return SEVERITY_ORDER[self.severity] >= SEVERITY_ORDER[severity]

    def __str__(self) -> str:
        return f"[{self.source}/{self.severity.value}] {self.code}: {self.title}"


@dataclass(frozen=True, slots=True)
class Artifact:
    """One uploaded file as the project recorded it, next to its bytes."""

    filename: str
    sha256: str
    size_bytes: int
    stored_path: Path


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
    requirements: tuple[str, ...]
    tags: tuple[str, ...]
    programs: tuple[str, ...]
    routines: tuple[str, ...]
    rung_count: int
    tasks: tuple[tuple[TaskKind, TaskStatus, str], ...]
    gate_states: tuple[tuple[ApprovalGateKey, GateStatus, str], ...]
    gates_decided: tuple[ApprovalGateKey, ...]
    open_high_severity: tuple[Report, ...]
    candidate_count: int
    candidate_artifacts: tuple[tuple[ExportArtifactKind, str, ValidationLevel], ...]
    limitations_text: str
    rounds_taken: int
    human_acts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def findings(self) -> tuple[Report, ...]:
        return tuple(item for item in self.reports if item.is_finding)

    @property
    def blocking_errors(self) -> tuple[Report, ...]:
        return tuple(
            item
            for item in self.reports
            if item.source == "validation" and item.severity is FindingSeverity.HIGH
        )

    @property
    def judged(self) -> tuple[Report, ...]:
        """The reports the decoy rules apply to: findings and release blockers."""
        return self.findings + self.blocking_errors


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


def _decide_approvals(
    client: TestClient,
    project_id: uuid.UUID,
    auth: dict[str, str],
) -> list[str]:
    """Settle eligible human decisions through the same HTTP doors as the UI."""
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
            decision = client.post(
                f"/api/approval-gates/{row['gate_id']}/approvals/{row['id']}/decision",
                headers=auth,
                json={
                    "decision": "approve",
                    "comment": (
                        "Checked against the functional description, interlock matrix, "
                        "and current validation evidence."
                    ),
                },
            )
        else:
            decision = client.post(
                f"/api/approvals/{row['id']}/decision",
                headers=auth,
                json={"approved": True, "notes": "Reviewed against the source documents."},
            )
        assert decision.status_code == 200, decision.text
        acts.append(f"decided approval: {row['title'][:80]}")
    return acts


def _run_project(
    client: TestClient,
    settings: Settings,
    *,
    project_id: uuid.UUID,
    engineer_auth: dict[str, str],
    reviewer_auth: dict[str, str],
) -> tuple[int, list[str]]:
    """Work, answer, decide, repeat — until nothing is waiting on a human."""
    acts: list[str] = []
    rounds = 0
    for _ in range(MAX_ROUNDS):
        rounds += 1
        worker.drain(settings)
        this_round = _resolve_clarifications(client, project_id, engineer_auth)
        this_round += _decide_requirements(client, project_id, reviewer_auth)
        this_round += _decide_approvals(client, project_id, reviewer_auth)
        acts.extend(this_round)
        if not this_round:
            break
    worker.drain(settings)
    return rounds, acts


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
    )


def _snapshot(settings: Settings, project_id: uuid.UUID, rounds: int, acts: list[str]) -> Scenario:
    """Freeze what the run produced, as an engineer would read it at the end.

    Findings accumulate across the whole run, so all of them are taken. Only the
    **newest** validation run is: a production run validates several times as
    the model grows, and an earlier run describes a revision that no longer
    exists. Judging the fixture's ground truth against superseded results would
    report defects the project had already moved past.

    Waived results are excluded for the same reason — a waiver is a named
    human's decision that the finding is carried knowingly, and re-reporting it
    would count somebody's signature as a defect.
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
        validations = (
            [
                _validation_report(row)
                for row in session.execute(
                    select(ValidationResult).where(
                        ValidationResult.validation_run_id == newest_run.id,
                        ValidationResult.is_waived.is_(False),
                    )
                ).scalars()
            ]
            if newest_run is not None
            else []
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

        gate_rows = session.execute(
            select(ApprovalGate).where(ApprovalGate.project_id == project_id)
        ).scalars()
        gate_states = tuple(
            (row.gate_key, row.status, str(row.unmet_criteria)[:400]) for row in gate_rows
        )

        candidates = list(
            session.execute(
                select(ReleaseCandidate).where(ReleaseCandidate.project_id == project_id)
            ).scalars()
        )
        candidate_artifacts: tuple[tuple[ExportArtifactKind, str, ValidationLevel], ...] = ()
        limitations = ""
        for candidate in candidates:
            for item in candidate.artifacts:
                candidate_artifacts += ((item.kind, item.relative_path, item.validation_level),)
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
            gate_states=gate_states,
            gates_decided=tuple(
                key for key, status, _ in gate_states if status is GateStatus.PASSED
            ),
            open_high_severity=open_high,
            candidate_count=len(candidates),
            candidate_artifacts=candidate_artifacts,
            limitations_text=limitations,
            rounds_taken=rounds,
            human_acts=tuple(acts),
        )
    finally:
        session.close()


def _read_artifact(storage: ProjectStorage, relative_path: str) -> str:
    try:
        return storage.absolute_path(relative_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


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

            rounds, acts = _run_project(
                client,
                settings,
                project_id=project_id,
                engineer_auth=engineer_auth,
                reviewer_auth=reviewer_auth,
            )
        yield _snapshot(settings, project_id, rounds, acts)
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
    """Steps 4 and 5. The refusal to guess, expressed as a question."""
    assert scenario.requirements, "nothing was extracted from eight engineering documents"
    assert scenario.clarifications, (
        "the run reached generation without asking a single question about a "
        "document set that contains a deliberate ambiguity"
    )
    assert any("resolved clarification" in act for act in scenario.human_acts), (
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


def test_cross_discipline_validation_blocks_the_release(scenario: Scenario) -> None:
    """Step 12. The point of the step is the stop, so the stop is what is asserted."""
    blocked = [
        (status, reason)
        for kind, status, reason in scenario.tasks
        if kind is TaskKind.VALIDATE_CROSS_DISCIPLINE and status is TaskStatus.BLOCKED
    ]

    assert scenario.blocking_errors, (
        "cross-discipline validation found nothing that stops a release in a document "
        "set with seven planted defects"
    )
    assert blocked, "validation found release-blocking errors and let the run continue"
    assert all(reason for _, reason in blocked), (
        "a run parked without a stated reason looks like a stalled one"
    )


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Accepting a requirement through POST /api/requirements/{id}/decision can turn it "
        "into a release blocker on the spot. ApprovalIntegrityValidator reports "
        "cross.approved_object_modified whenever updated_at > approved_at, and the accept "
        "path writes both in one flush: whether the two clock reads land in the same tick "
        "decides whether all seven requirements are reported as 'approved at T and modified "
        "at T+2ms, without a fresh approval'. Non-strict deliberately — the defect is a race, "
        "so the run that passes is as much evidence of it as the run that fails, and a strict "
        "marker would make the acceptance suite flaky in place of the product."
    ),
)
def test_accepting_a_requirement_does_not_invalidate_its_own_approval(
    scenario: Scenario,
) -> None:
    """A human decision must not read as tampering with the thing it decided.

    An approved object is protected so that an engineer's signature stays
    attached to what they read. When the act of signing is itself recorded as a
    later edit, every accepted requirement carries a name against content
    nobody changed, and the release stops for a reason that never happened.
    """
    self_invalidated = [
        report
        for report in scenario.blocking_errors
        if report.code == "cross.approved_object_modified"
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Planted issue 1 is not found. ILK-404 is documented as Existing, wired to "
        "Local:1:I.Data.7 and implemented nowhere — the string PE402 does not occur in the "
        "L5X. The Interlock Review Agent does not report the absence: the evidence graph "
        "offers PE401_BLOCKED as a candidate for PE402_BLOCKED on the 'same word initials' "
        "rule at 0.923, which is exactly the rule that cannot tell two photo-eyes apart, and "
        "the agent downgrades a missing protection to a medium naming mismatch that names the "
        "wrong device. The only other trace is tags.orphaned_tag on PE402_BLOCKED at "
        "information severity."
    ),
)
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Planted issue 3 is found, but inflated. The README rates it low to medium — the "
        "signal is implemented and correctly wired, and only its name differs — yet the "
        "product raises logic.output_without_driving_rung at ERROR, which is release-blocking "
        "and outranks the genuinely high-severity missing interlock it should sit below."
    ),
)
def test_planted_issue_3_is_not_inflated_into_a_release_blocker(scenario: Scenario) -> None:
    """A naming inconsistency must not outrank a missing protection."""
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Planted issue 6 is not found. The seven clarifications the run raises are all "
        "generated from the Cause and Effect worksheet's blank columns; none quotes the "
        "accumulation-release sentence in Project_Description.docx section 3, and no finding "
        "names it. The one deliberate ambiguity in the document set — the sentence the "
        "functional description itself records as unsettled — passes through unremarked."
    ),
)
def test_planted_issue_6_the_ambiguous_accumulation_release_is_raised_with_the_customer(
    scenario: Scenario,
) -> None:
    """Two defensible readings, and the product must ask rather than choose."""
    asked = [
        question
        for question in scenario.clarifications
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
        "raised:\n" + ("\n".join(f"  {item}" for item in scenario.clarifications) or "  (none)")
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Planted issue 7, the false-positive control, fails. CYL401 is implemented "
        "consistently in all eight artifacts and a correct review says nothing about it, but "
        "the run raises 'ILK-403 is incompletely specified' — a documentation_gap against the "
        "diverter interlock. The claim is false: row 4 of the Interlock Matrix sheet fills "
        "every column. It comes from the Cause and Effect worksheet, which the evidence graph "
        "reads as seven more interlock rows whose trip_tag, trip_condition, action and "
        "failsafe columns do not exist. The same duplication produces a spurious "
        "documentation_gap for all seven interlocks and seven spurious clarifications."
    ),
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
_DECOY_FIRES_TODAY = (
    "cyl401",
    "ilk_405_missing_from_the_base_export",
    "ilk_406_missing_from_the_base_export",
    "air_press_psi_has_no_controller_tag",
    "ilk_401_and_ilk_402",
    "ilk_407",
)

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
                        f"Decoy '{key}' fires. The README states: {statement} See the "
                        "duplication of the Interlock Matrix and Cause and Effect worksheets "
                        "into fourteen interlock requirements, and the treatment of a "
                        "'New (this upgrade)' scope as a current one."
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The fixture intentionally contains two devices assigned to Local:1:I.Data.10. "
        "The canonical importer now preserves both assignments and deterministic validation "
        "correctly blocks gate 2 until an engineer fixes that conflict or a named reviewer "
        "records a waiver. This scenario performs neither remediation, so approving gate 2 "
        "would be a safety regression rather than a successful end state."
    ),
)
def test_a_human_approves_gate_two(scenario: Scenario) -> None:
    """Step 8. Devices, I/O and tags, signed off by a named person."""
    assert ApprovalGateKey.DEVICES_IO_TAGS in scenario.gates_decided, (
        "gate 2 was never passed:\n"
        + "\n".join(
            f"  {key.value}: {status.value} {unmet}" for key, status, unmet in scenario.gate_states
        )
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The delivery/HMI dependency cycle is fixed, but this deliberately defective fixture "
        "still carries unwaived release-blocking validation results. The workflow must not "
        "assemble or seal a candidate while those earlier safety gates remain unsettled."
    ),
)
def test_generators_assemble_the_package_and_a_release_candidate_is_sealed(
    scenario: Scenario,
) -> None:
    """Steps 14 and 15. Hashes, import order, limitations and approvals."""
    assert scenario.candidate_count == 1, f"{scenario.candidate_count} release candidates"
    assert scenario.candidate_artifacts, "the candidate carries no artifacts"

    kinds = {kind for kind, _, _ in scenario.candidate_artifacts}
    assert ExportArtifactKind.LIMITATIONS in kinds, sorted(item.value for item in kinds)

    over_claimed = [
        (kind.value, path, level.value)
        for kind, path, level in scenario.candidate_artifacts
        if VALIDATION_LEVEL_ORDER[level] > VALIDATION_LEVEL_ORDER[ValidationLevel.ROUND_TRIP_PARSED]
    ]
    assert not over_claimed, (
        f"artifacts claim more than this backend can prove offline: {over_claimed}"
    )
    assert "import-ready candidate" in scenario.limitations_text.lower(), (
        "LIMITATIONS.md does not call the package an import-ready candidate"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "This fixture deliberately leaves true validation errors unresolved and unwaived, so "
        "the safe terminal state is blocked rather than export-ready. Separately, a genuinely "
        "finalized archive converges to COMPLETED; READY_FOR_EXPORT is the pre-finalization "
        "state, not the durable post-delivery status."
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
                "tags": _count(session, Tag, scenario.project_id),
                "rungs": _count(session, LogicRung, scenario.project_id),
                "tasks": _count(session, Task, scenario.project_id),
                "gates": _count(session, ApprovalGate, scenario.project_id),
            }

        assert counts["files"] == len(scenario.artifacts)
        assert counts["findings"] == len(scenario.findings)
        assert counts["requirements"] == len(scenario.requirements)
        assert counts["clarifications"] == len(scenario.clarifications)
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
