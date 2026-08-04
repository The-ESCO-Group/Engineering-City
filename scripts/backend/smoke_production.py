"""The Line 4 acceptance scenario, against a live backend process.

The production sibling of ``scripts/backend/smoke.py``. That script proves the
process starts and serves a review; this one drives the production pipeline over
the eight synthetic artifacts in ``fixtures/line4_packaging_conveyor/`` — on a
real uvicorn, against a throwaway database — and judges what comes back against
the ground truth that fixture's README states in advance: seven planted defects,
and eleven plausible-looking things that are **not** defects.

    python scripts/backend/smoke_production.py
    python scripts/backend/smoke_production.py --base-url http://127.0.0.1:8000

Four kinds of assertion, and the difference between them is the point:

* ``check`` is a requirement. A failure is a regression and the script exits 1.
* ``found`` is one of the seven planted issues.
* ``quiet`` is one of the eleven decoys, and passes when nothing fired.
* ``gap`` is a step of the acceptance scenario this script already knows the
  product does not reach, and says why. A gap that starts passing is announced
  as ``RESOLVED`` so whoever fixed it deletes the entry rather than leaving a
  stale one behind.

Only ``check`` can fail the run. The other three collect into one report at the
end, because a smoke script that went red on a known limitation would be
silenced within a week and the limitation would stop being visible at all.

The largest gap caps most of the others: **no HTTP route decides an approval
gate.** ``POST /api/approvals/{id}/decision`` settles the approval row and leaves
the ``ApprovalGate`` untouched, so a production run driven entirely over the API
parks at gate 1 and never reaches design, generation or release. Everything
downstream of that gate is therefore unobservable here, and is recorded as a gap
rather than pretended away. ``backend/tests/test_line4_end_to_end.py`` reaches
further by calling :mod:`app.engineering` directly, and carries the same list as
``xfail(strict=True)`` markers with the full diagnosis of each.

Restart is exercised for real rather than simulated: the server is stopped and a
second process is booted against the same database and data directory, which is
the only way to find state that lived in one process's memory.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "line4_packaging_conveyor"

ADMIN_EMAIL = "admin@esco.internal"
ADMIN_PASSWORD = "esco-demo-admin"  # noqa: S105 - the seeded demo credential

PROJECT_NAME = "Line 4 Packaging Conveyor Upgrade"

#: Why nothing past gate 1 can be observed over HTTP. Quoted on every gap it
#: causes, so a reader never has to guess whether a step is broken or merely
#: out of reach.
GATE_WALL = (
    "the run parks at gate 1: no HTTP route reaches "
    "app.engineering.gates.record_decision, so a reviewer cannot pass a gate and "
    "nothing behind one ever runs"
)

#: The eight artifacts and the engineering purpose the uploader declares.
ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("Project_Description.docx", "other"),
    ("Functional_Description.docx", "functional_description"),
    ("IO_List.csv", "io_list"),
    ("Interlock_Matrix.xlsx", "interlock_schedule"),
    ("Conveyor_Line4_Base.L5X", "control_logic"),
    ("HMI_Screens.xml", "hmi_export"),
    ("Alarm_Register.csv", "alarm_list"),
    ("Approved_Templates.txt", "other"),
)

#: What the README forbids a correct review from reporting, and the words a
#: report about each would have to use. Order and wording follow the README's own
#: "What a correct review must not report" list.
DECOYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("anything about CYL401", ("cyl401", "ilk-403")),
    ("ILK-405 missing from the base export", ("ilk-405", "low plant air pressure")),
    ("ILK-406 missing from the base export", ("ilk-406", "case jam at the cv402")),
    ("AIR_PRESS_PSI having no controller tag", ("air_press_psi",)),
    ("the unimplemented scope of work", ("jog", "pressure transmitter", "jam detection")),
    ("AutoMode written and never read", ("automode",)),
    ("ILK-401 or ILK-402", ("ilk-401", "ilk-402")),
    ("ILK-407", ("ilk-407",)),
    ("the Cause and Effect scope cells", ("new alarm to be added",)),
    ("the ESCO-TPL-* identifiers", ("esco-tpl-",)),
    ("runtime or firmware behaviour", ("firmware", "majorrev", "minorrev")),
)

PASSED = 0
FAILED: list[str] = []
GAPS: list[str] = []
RESOLVED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok    {label}", flush=True)
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""), flush=True)


def gap(label: str, condition: bool, reason: str, detail: str = "") -> None:
    """A scenario step this script knows the product does not reach yet.

    Never fatal: the same truth is already an ``xfail(strict=True)`` in the
    acceptance test, and one loud instrument per fact is enough. Passing is
    announced as ``RESOLVED`` — somebody fixed it, and the entry should be
    deleted rather than left to rot.
    """
    global PASSED
    if condition:
        PASSED += 1
        RESOLVED.append(label)
        print(f"  ok    {label}  (RESOLVED - delete this gap entry)", flush=True)
    else:
        _record_gap(label, reason, detail)
        print(f"  gap   {label}", flush=True)


def found(label: str, condition: bool, reason: str, detail: str = "") -> None:
    """One of the seven planted issues, measured rather than expected.

    Which of them this build finds is what the run is for, so neither outcome
    is a surprise worth announcing: a detection is a detection, and a miss goes
    into the gap report with the diagnosis.
    """
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok     {label}", flush=True)
    else:
        _record_gap(label, reason, detail)
        print(f"  MISSED {label}", flush=True)


def quiet(label: str, condition: bool, reason: str, detail: str = "") -> None:
    """One of the eleven decoys. ``condition`` is true when nothing fired.

    These matter more than the positives: a review that reports everything
    scores as well as one that is right, and the decoys are the only thing that
    tells them apart.
    """
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok     {label}", flush=True)
    else:
        _record_gap(label, reason, detail)
        print(f"  FIRED  {label}", flush=True)


def _record_gap(label: str, reason: str, detail: str) -> None:
    GAPS.append(f"{label}\n      why: {reason}" + (f"\n      got: {detail}" if detail else ""))


def wait_for_health(base_url: str, timeout: float = 90.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/api/health", timeout=5.0)
            if response.status_code == 200:
                return dict(response.json())
            last = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"backend did not become healthy within {timeout:.0f}s: {last}")


# ---------------------------------------------------------------------------
# Driving the scenario over HTTP
# ---------------------------------------------------------------------------
def _intake_payload() -> dict[str, Any]:
    """The plant exactly as the fixture's README describes it.

    Every value is quoted from that document. Declaring a controller the fixture
    does not have, or no HMI when there is a PanelView on the line, would be
    arranging the answer rather than measuring the product.
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


def _state(client: httpx.Client, project_id: str, auth: dict[str, str]) -> dict[str, Any]:
    response = client.get(f"/api/projects/{project_id}/workflow", headers=auth)
    return dict(response.json()) if response.status_code == 200 else {"error": response.text}


def _quiesce(client: httpx.Client, project_id: str, auth: dict[str, str]) -> dict[str, Any]:
    """Wait until the background worker stops changing anything.

    Deliberately not "wait until the queue is empty", which is what the review
    smoke waits for: a production run leaves tasks queued behind a gate that has
    not been decided, and that queue never drains. Quiescence — no task running
    and no counter moving across consecutive polls — is the only condition that
    describes both a finished run and a parked one.
    """
    previous: tuple[Any, ...] = ()
    still = 0
    deadline = time.monotonic() + 120.0
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = _state(client, project_id, auth)
        signature = tuple(
            state.get(key, 0)
            for key in ("queued", "running", "blocked", "awaiting_approval", "completed", "failed")
        )
        if state.get("running"):
            still = 0
        elif signature == previous:
            still += 1
            if still >= 3:
                return state
        else:
            still = 0
        previous = signature
        time.sleep(0.4)
    return state


def _settle(client: httpx.Client, project_id: str, auth: dict[str, str], decided: set[str]) -> int:
    """One turn of the loop a person drives a production run with.

    Answer what was asked, decide what was raised. ``decided`` remembers what has
    already been settled, because a gate task that is unblocked and re-run raises
    a fresh approval every time and a caller that kept deciding them would spin
    for ever without moving the gate.
    """
    moved = 0

    inbox = client.get(f"/api/projects/{project_id}/clarifications", headers=auth)
    if inbox.status_code == 200:
        body = inbox.json()
        for row in body.get("clarifications", []):
            if not row["blocks_generation"] or row["status"] not in {"open", "deferred"}:
                continue
            answer = client.post(
                f"/api/clarifications/{row['id']}/resolve",
                headers=auth,
                json={
                    "answer": "Confirmed with the line engineer against the interlock matrix.",
                    "resolution": "answered",
                },
            )
            moved += 1 if answer.status_code == 200 else 0
        for row in body.get("project_questions", []):
            if not row["is_blocking"] or row["status"] not in {"open", "deferred"}:
                continue
            answer = client.post(
                f"/api/project-questions/{row['id']}/resolve",
                headers=auth,
                json={"answer": "Confirmed with the customer.", "resolution": "answered"},
            )
            moved += 1 if answer.status_code == 200 else 0

    requirements = client.get(f"/api/projects/{project_id}/requirements", headers=auth)
    if requirements.status_code == 200:
        for row in requirements.json():
            if row["status"] != "proposed":
                continue
            decision = client.post(
                f"/api/requirements/{row['id']}/decision",
                headers=auth,
                json={
                    "decision": "accept",
                    "rationale": "Checked against the functional description.",
                },
            )
            moved += 1 if decision.status_code == 200 else 0

    approvals = client.get(f"/api/projects/{project_id}/approvals", headers=auth)
    if approvals.status_code == 200:
        for row in approvals.json():
            key = f"{row['kind']}:{row['title']}"
            if row["status"] != "pending" or not row["decision_allowed"] or key in decided:
                continue
            decision = client.post(
                f"/api/approvals/{row['id']}/decision",
                headers=auth,
                json={"approved": True, "notes": "Reviewed against the source documents."},
            )
            if decision.status_code == 200:
                decided.add(key)
                moved += 1

    return moved


def _run_to_rest(client: httpx.Client, project_id: str, auth: dict[str, str]) -> dict[str, Any]:
    """Work, answer, decide, repeat, until a turn changes nothing."""
    decided: set[str] = set()
    state = _quiesce(client, project_id, auth)
    for _ in range(12):
        if not _settle(client, project_id, auth, decided):
            break
        state = _quiesce(client, project_id, auth)
    return state


def _reports(client: httpx.Client, project_id: str, auth: dict[str, str]) -> list[dict[str, Any]]:
    """Everything the product said about this plant that costs somebody time.

    Findings, and whatever stops a release. Standing capability disclosures are
    excluded: they describe the limits of what was checked, not this plant. Same
    scope as the acceptance test's, so the two cannot disagree.
    """
    rows: list[dict[str, Any]] = []

    findings = client.get(f"/api/projects/{project_id}/findings", headers=auth)
    if findings.status_code == 200:
        for item in findings.json()["items"]:
            detail = client.get(f"/api/findings/{item['id']}", headers=auth)
            body = detail.json() if detail.status_code == 200 else item
            rows.append(
                {
                    "source": "finding",
                    "code": str(body.get("category", "")),
                    "severity": str(body.get("severity", "")),
                    "title": str(body.get("title", "")),
                    "text": " ".join(
                        str(body.get(key, ""))
                        for key in (
                            "title",
                            "category",
                            "fact",
                            "inference",
                            "suggested_review_action",
                            "limitations",
                        )
                    ).lower(),
                }
            )

    readiness = client.get(f"/api/projects/{project_id}/release-readiness", headers=auth)
    if readiness.status_code == 200:
        for item in readiness.json().get("blocks", []):
            rows.append(
                {
                    "source": "release_block",
                    "code": str(item.get("code", "")),
                    "severity": "high",
                    "title": str(item.get("message", ""))[:120],
                    "text": " ".join(str(value) for value in item.values()).lower(),
                }
            )
    return rows


def _names(report: dict[str, Any], *subjects: str) -> bool:
    return any(subject in report["text"] for subject in subjects)


def _rank(value: str) -> int:
    return {"informational": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}.get(value, 0)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
def run_checks(base_url: str) -> str:
    """Drive the scenario and judge it. Returns the project id for the restart pass."""
    print("\n[health]")
    health = wait_for_health(base_url)
    check("status is ok", health.get("status") == "ok", str(health))
    check(
        "only the mock provider is enabled",
        health.get("enabled_providers") == ["mock"],
        f"got {health.get('enabled_providers')}",
    )
    capabilities = {row["name"]: row for row in health.get("capabilities", [])}
    check(
        "delivery agent plugins are installed",
        capabilities.get("delivery_agents", {}).get("available") is True,
        str(capabilities.get("delivery_agents")),
    )
    check(
        "report providers are installed",
        capabilities.get("report_providers", {}).get("available") is True,
        str(capabilities.get("report_providers")),
    )

    client = httpx.Client(base_url=base_url, timeout=120.0)

    print("\n[the engineer signs in]")
    login = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    check("seeded administrator can sign in", login.status_code == 200, login.text)
    if login.status_code != 200:
        client.close()
        return ""
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

    print("\n[project and intake]")
    created = client.post(
        "/api/projects",
        headers=auth,
        json={
            "name": PROJECT_NAME,
            "description": (
                "Acceptance scenario from fixtures/line4_packaging_conveyor. Synthetic only."
            ),
            "client_reference": "LINE4-SMOKE-001",
        },
    )
    check("project created", created.status_code == 201, created.text)
    if created.status_code != 201:
        client.close()
        return ""
    project_id = str(created.json()["id"])

    missing = [name for name, _ in ARTIFACTS if not (FIXTURES / name).is_file()]
    if missing:
        check(
            "the eight fixture artifacts are present",
            False,
            f"missing {missing}; run python scripts/backend/make_line4_fixture.py",
        )
        client.close()
        return project_id

    print("\n[upload]")
    for name, role in ARTIFACTS:
        data = (FIXTURES / name).read_bytes()
        response = client.post(
            f"/api/projects/{project_id}/files",
            headers=auth,
            files=[("files", (name, data))],
            data={"document_role": role},
        )
        item = response.json()["results"][0] if response.status_code == 201 else {}
        check(
            f"{name} accepted as {role}",
            response.status_code == 201 and item.get("accepted") is True,
            str(item.get("rejection_reason") or response.text),
        )

    listing = client.get(f"/api/projects/{project_id}/files", headers=auth)
    check("all eight artifacts are listed", len(listing.json()) == 8, str(listing.status_code))

    print("\n[stored bytes match the recorded hash]")
    for row in listing.json():
        download = client.get(f"/api/files/{row['id']}/content", headers=auth)
        digest = hashlib.sha256(download.content).hexdigest() if download.status_code == 200 else ""
        source = FIXTURES / row["original_filename"]
        check(
            f"{row['original_filename']} serves the bytes it was stored under",
            digest == row["sha256"],
            f"recorded {row['sha256'][:12]}, served {digest[:12]}",
        )
        check(
            f"{row['original_filename']} is byte-identical to the fixture",
            source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == row["sha256"],
        )

    profile = client.put(
        f"/api/projects/{project_id}/engineering-profile", headers=auth, json=_intake_payload()
    )
    check("engineering intake is accepted", profile.status_code == 200, profile.text)
    check(
        "the profile reports itself complete enough to generate",
        profile.status_code == 200 and profile.json()["is_complete_for_generation"] is True,
        profile.text[:400],
    )

    print("\n[the production run]")
    run = client.post(f"/api/projects/{project_id}/runs", headers=auth, json={})
    check("run starts", run.status_code == 201, run.text)
    if run.status_code != 201:
        client.close()
        return project_id
    check(
        "the whole chain is planned up front",
        run.json()["task_count"] >= 19,
        f"{run.json()['task_count']} tasks",
    )
    # The choice of workflow is recorded on the project so that a run stays
    # explicable after somebody edits the profile.
    chosen = dict(
        client.get(f"/api/projects/{project_id}", headers=auth).json().get("attributes", {})
    ).get("workflow", {})
    check(
        "the production workflow was chosen, and says why",
        chosen.get("kind") == "production" and bool(chosen.get("reason")),
        str(chosen),
    )

    state = _run_to_rest(client, project_id, auth)
    check("no task failed", state.get("failed", 0) == 0, str(state))

    # Read from the resource endpoints rather than from the event log: events
    # are paginated and a production run emits more than one page, so a missing
    # event type would say more about the page size than about the run.
    extracted = client.get(f"/api/projects/{project_id}/requirements", headers=auth)
    check(
        "requirements were extracted from the documents",
        extracted.status_code == 200 and len(extracted.json()) > 0,
        extracted.text[:300],
    )

    inbox = client.get(f"/api/projects/{project_id}/clarifications", headers=auth)
    questions = (
        [row["question"].lower() for row in inbox.json()["clarifications"]]
        if inbox.status_code == 200
        else []
    )
    check("at least one clarification was raised", bool(questions), "no question was asked")

    raised = client.get(f"/api/projects/{project_id}/approvals", headers=auth)
    check(
        "a human decision was requested",
        raised.status_code == 200 and len(raised.json()) > 0,
        raised.text[:300],
    )

    print("\n[what the review reported]")
    reports = _reports(client, project_id, auth)
    check("the review produced findings", any(r["source"] == "finding" for r in reports))

    print("\n[the seven planted issues]")
    found(
        "issue 1: the missing ILK-404 accumulation interlock is reported at high severity",
        any(
            _names(r, "pe402") and _rank(r["severity"]) >= 3 and "missing" in r["code"]
            for r in reports
        ),
        "PE401_BLOCKED is offered as a candidate for PE402_BLOCKED on the 'same word "
        "initials' rule at 0.923, so a missing protection is downgraded to a medium naming "
        "mismatch that names the wrong photo-eye.",
    )
    found(
        "issue 2: Local:1:I.Data.10 carrying two devices is reported",
        any(_names(r, "local:1:i.data.10") for r in reports),
        "the I/O list has no device column, so no Signal and no AddressAssignment is created "
        "and io.duplicate_point_assignment has nothing to compare; the two rows collapse into "
        "a single I/O point.",
    )
    found(
        "issue 3: the CV401 run command carrying two names is reported",
        any(_names(r, "mtr401_run_cmd", "cv401_run_output") for r in reports),
        f"found in-process by logic.output_without_driving_rung, but {GATE_WALL} - and the "
        "first validation run is behind it.",
    )
    found(
        "issue 4: the ALM_CYL402_FAIL alarm that can never annunciate is reported",
        any(_names(r, "alm_cyl402_fail") and _rank(r["severity"]) >= 2 for r in reports),
        "Alarm_Register.csv never reaches the evidence graph - its 'Alarm ID' header matches "
        "no alarm-name alias, so all seven rows are dropped - and no AlarmDefinition is "
        "imported, so the check that would find it has no rows to check.",
    )
    found(
        "issue 5: the HMI binding to the missing Accum_Case_Count tag is reported",
        any(_names(r, "accum_case_count") for r in reports),
        "the FactoryTalk display export contributes no bindings to the evidence graph and no "
        "HMIBinding rows to the canonical model, so the expression is never read at all.",
    )
    found(
        "issue 6: the ambiguous accumulation release is put back to the customer",
        any("accumulat" in item and "releas" in item for item in questions),
        "the clarifications raised are all generated from the Cause and Effect worksheet's "
        "blank columns; the one deliberate ambiguity in the document set is never quoted.",
        f"{len(questions)} question(s) raised",
    )
    cyl401 = [r for r in reports if _names(r, "cyl401", "ilk-403")]
    found(
        "issue 7: the correctly implemented CYL401 diverter produces no findings",
        not cyl401,
        "the Cause and Effect worksheet is read as seven more interlock rows with no "
        "trip_tag, trip_condition, action or failsafe column, so every interlock - including "
        "the correct one - is reported as incompletely specified.",
        "; ".join(item["title"] for item in cyl401),
    )

    print("\n[the eleven decoys]")
    for label, subjects in DECOYS:
        fired = [r for r in reports if _names(r, *subjects)]
        quiet(
            f"decoy stayed quiet: {label}",
            not fired,
            "over-reach. A review that flags everything scores as well as one that is right; "
            "these eleven are the only thing that tells them apart.",
            "; ".join(f"[{item['severity']}] {item['title']}" for item in fired),
        )

    print("\n[gates, generation and release]")
    openapi = client.get("/openapi.json")
    paths = list(openapi.json().get("paths", {})) if openapi.status_code == 200 else []
    gap(
        "a reviewer can decide an approval gate through the API",
        any("gate" in path.lower() for path in paths),
        "no HTTP route reaches app.engineering.gates.record_decision. "
        "POST /api/approvals/{id}/decision settles the approval row and leaves the gate "
        "untouched, so the six gates cannot be passed from outside the process at all.",
        f"{len(paths)} paths, none naming a gate",
    )
    validations = client.get(
        f"/api/projects/{project_id}/events",
        headers=auth,
        params={"event_type": "validation.completed", "limit": 5},
    )
    gap(
        "a validation run completes",
        validations.status_code == 200 and len(validations.json()) > 0,
        f"{GATE_WALL}; the first validation task sits behind gate 1.",
    )

    candidates = client.get(f"/api/projects/{project_id}/release-candidates", headers=auth)
    candidate_rows = (
        list(candidates.json().get("items", [])) if candidates.status_code == 200 else []
    )
    gap("a release candidate is generated", bool(candidate_rows), GATE_WALL)

    over_claimed: list[str] = []
    limitations = ""
    for candidate in candidate_rows:
        artifacts = client.get(f"/api/release-candidates/{candidate['id']}/artifacts", headers=auth)
        for artifact in artifacts.json() if artifacts.status_code == 200 else []:
            if artifact.get("validation_level") in {"rockwell_import_verified", "released"}:
                over_claimed.append(f"{artifact['kind']} claims {artifact['validation_level']}")
            if artifact.get("kind") == "limitations":
                limitations = str(artifact.get("content", ""))
    gap(
        "no artifact claims more than round-trip parsed",
        bool(candidate_rows) and not over_claimed,
        f"unreachable: {GATE_WALL}.",
        "; ".join(over_claimed),
    )
    gap(
        "LIMITATIONS.md calls the package an import-ready candidate",
        "import-ready candidate" in limitations.lower(),
        f"unreachable: {GATE_WALL}.",
    )

    project = client.get(f"/api/projects/{project_id}", headers=auth).json()
    check(
        "the project did not complete with a high-severity finding outstanding",
        project["status"] != "completed",
        f"status is {project['status']}",
    )
    gap(
        "the project reaches READY_FOR_EXPORT",
        project["status"] == "ready_for_export",
        f"{GATE_WALL}.",
        f"status is {project['status']}",
    )

    client.close()
    return project_id


def check_after_restart(base_url: str, project_id: str) -> None:
    """Everything the first process learned is still there in the second.

    A real stop and a real reboot against the same database and data directory,
    because state that lived only in a process's memory is exactly what a
    simulated restart cannot find.
    """
    print("\n[after a real restart]")
    wait_for_health(base_url)
    with httpx.Client(base_url=base_url, timeout=120.0) as client:
        login = client.post(
            "/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )
        check("the second process authenticates the same account", login.status_code == 200)
        if login.status_code != 200:
            return
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

        project = client.get(f"/api/projects/{project_id}", headers=auth)
        check("the project survived the restart", project.status_code == 200, project.text)
        if project.status_code != 200:
            return
        check("its name survived", project.json()["name"] == PROJECT_NAME)

        files = client.get(f"/api/projects/{project_id}/files", headers=auth)
        check("all eight artifacts survived", len(files.json()) == 8, str(len(files.json())))
        for row in files.json():
            download = client.get(f"/api/files/{row['id']}/content", headers=auth)
            digest = (
                hashlib.sha256(download.content).hexdigest() if download.status_code == 200 else ""
            )
            check(f"{row['original_filename']} still hashes to its record", digest == row["sha256"])

        findings = client.get(f"/api/projects/{project_id}/findings", headers=auth)
        check("the findings survived", findings.json()["total"] > 0, findings.text[:200])
        requirements = client.get(f"/api/projects/{project_id}/requirements", headers=auth)
        check(
            "the requirements survived", len(requirements.json()) > 0, str(requirements.status_code)
        )
        tasks = client.get(f"/api/projects/{project_id}/tasks", headers=auth)
        check("the task graph survived", len(tasks.json()) >= 19, str(len(tasks.json())))

        # Sweep anything the stopped process left leased, then let the fresh one
        # carry the run as far as it can. A run that cannot resume after a
        # restart stops needing a controls engineer and starts needing somebody
        # to notice, which is the failure this catches.
        recovered = client.post(f"/api/projects/{project_id}/recover", headers=auth)
        check("abandoned work can be recovered", recovered.status_code == 200, recovered.text)
        state = _run_to_rest(client, project_id, auth)
        check("no task failed after the restart", state.get("failed", 0) == 0, str(state))
        check(
            "the restarted process reports the same run",
            bool(state.get("active_run_id")),
            str(state),
        )


def _boot(root: Path, port: int, log_path: Path) -> subprocess.Popen[bytes]:
    env = {
        **os.environ,
        "ESCO_ENVIRONMENT": "test",
        "ESCO_DATA_DIR": str(root / "data"),
        "ESCO_DATABASE_URL": f"sqlite+pysqlite:///{(root / 'line4.sqlite3').as_posix()}",
        "ESCO_LOG_JSON": "false",
        "ESCO_LOG_LEVEL": "WARNING",
        "ESCO_WORKER_ENABLED": "true",
        "ESCO_BOOTSTRAP_ADMIN_EMAIL": ADMIN_EMAIL,
        "ESCO_BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "PYTHONPATH": str(REPO_ROOT / "backend"),
    }
    with log_path.open("ab") as log:
        return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )


def _stop(server: subprocess.Popen[bytes]) -> None:
    server.terminate()
    try:
        server.wait(timeout=20)
    except subprocess.TimeoutExpired:  # pragma: no cover - a wedged process
        server.kill()
        server.wait(timeout=20)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="",
        help="Check an already-running backend instead of booting one. Skips the restart pass.",
    )
    parser.add_argument("--port", type=int, default=8124)
    args = parser.parse_args()

    # A smoke script reports; it does not raise. An unexpected exception is a
    # failed check like any other, printed alongside the server log, because a
    # traceback tells the reader which line broke and not which check did.
    if args.base_url:
        try:
            run_checks(args.base_url.rstrip("/"))
        except Exception as exc:
            FAILED.append(f"{type(exc).__name__}: {exc}")
        return _report()

    with tempfile.TemporaryDirectory(prefix="esco-line4-smoke-") as tmp:
        root = Path(tmp)
        base_url = f"http://127.0.0.1:{args.port}"
        log_path = root / "server.log"
        print(f"booting backend on {base_url} (fresh database, fresh data dir)")

        project_id = ""
        server = _boot(root, args.port, log_path)
        try:
            project_id = run_checks(base_url)
        except Exception as exc:
            FAILED.append(f"{type(exc).__name__}: {exc}")
        finally:
            _stop(server)

        if project_id:
            print("\nrestarting the backend against the same database and data directory")
            server = _boot(root, args.port, log_path)
            try:
                check_after_restart(base_url, project_id)
            except Exception as exc:
                FAILED.append(f"{type(exc).__name__}: {exc}")
            finally:
                _stop(server)

        if FAILED:
            print("\n--- server log ---")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-4000:])

    return _report()


def _report() -> int:
    print(f"\n{PASSED} checks passed, {len(FAILED)} failed, {len(GAPS)} gaps")
    for failure in FAILED:
        print(f"  - {failure}")
    if GAPS:
        print("\nWhat the acceptance scenario asked for and did not get:")
        for item in GAPS:
            print(f"  - {item}")
        print(
            "\nEach of these is an xfail in backend/tests/test_line4_end_to_end.py, "
            "with the full diagnosis."
        )
    if RESOLVED:
        print("\nGaps that now pass - delete their entries from this script and the test:")
        for item in RESOLVED:
            print(f"  - {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
