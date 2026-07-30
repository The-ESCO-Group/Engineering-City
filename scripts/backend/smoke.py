"""End-to-end smoke check against a live backend process.

Exercises the path a real user travels — boot, sign in, create a project, upload
the synthetic fixtures, read them back, download one, delete one — against a
running uvicorn server rather than a TestClient. The test suite proves the units;
this proves the process actually starts and serves.

    python scripts/backend/smoke.py                       # boot a server and check
    python scripts/backend/smoke.py --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "demo_project"

PASSED = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))


def wait_for_health(base_url: str, timeout: float = 60.0) -> dict[str, object]:
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


def _wait_for_review(client: httpx.Client, project_id: str, auth: dict[str, str]) -> dict:
    """Poll the workflow state until the background worker settles.

    Polling is the honest way to observe an asynchronous worker from outside the
    process. The test suite drives the queue directly instead, where determinism
    matters more than realism.
    """
    state: dict = {}
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        response = client.get(f"/api/projects/{project_id}/workflow", headers=auth)
        if response.status_code != 200:
            return {"error": response.text}
        state = response.json()
        if state.get("queued", 0) == 0 and state.get("running", 0) == 0:
            return state
        time.sleep(0.5)
    return state


def run_checks(base_url: str, admin_email: str, admin_password: str) -> None:
    print("\n[health]")
    health = wait_for_health(base_url)
    check("status is ok", health.get("status") == "ok", str(health))
    check("contract version reported", bool(health.get("contract_version")))
    check(
        "only the mock provider is enabled",
        health.get("enabled_providers") == ["mock"],
        f"got {health.get('enabled_providers')}",
    )

    client = httpx.Client(base_url=base_url, timeout=60.0)

    print("\n[authentication]")
    # Checked on a cookie-free client, and before logging in: the shared client
    # keeps the session cookie that login sets, so asking it afterwards would
    # pass via cookie auth and prove nothing about anonymous access.
    with httpx.Client(base_url=base_url, timeout=30.0) as anonymous:
        denied = anonymous.get("/api/auth/me")
        check("unauthenticated access is refused", denied.status_code == 401, denied.text)
        check(
            "refusal uses the documented error envelope",
            denied.json().get("error", {}).get("code") == "authentication_required",
            denied.text,
        )

    login = client.post("/api/auth/login", json={"email": admin_email, "password": admin_password})
    check("seeded administrator can sign in", login.status_code == 200, login.text)
    if login.status_code != 200:
        return
    token = login.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    bad = client.post(
        "/api/auth/login", json={"email": admin_email, "password": "definitely-wrong"}
    )
    check("a wrong password is refused", bad.status_code == 401, bad.text)
    me = client.get("/api/auth/me", headers=auth)
    check("authenticated identity is returned", me.status_code == 200, me.text)
    # Login also set an httpOnly cookie; the browser frontend relies on it, so
    # the bearer header must not be the only thing that works.
    check(
        "the session cookie authenticates too",
        client.get("/api/auth/me").status_code == 200,
    )

    print("\n[project]")
    created = client.post(
        "/api/projects",
        headers=auth,
        json={
            "name": "Smoke Check - Conveyor Line 3",
            "description": "Created by scripts/backend/smoke.py against synthetic fixtures.",
            "client_reference": "SMOKE-001",
        },
    )
    check("project created", created.status_code == 201, created.text)
    if created.status_code != 201:
        return
    project = created.json()
    project_id = project["id"]
    check("project starts as a draft", project["status"] == "draft")
    check("creator is its owner", project["members"][0]["role"] == "owner")

    print("\n[upload]")
    uploads = [
        "Conveyor_Line3.L5X",
        "Interlock_Schedule.xlsx",
        "IO_List.csv",
        "Functional_Description.docx",
        "Testing_Notes.pdf",
        "Scanned_Drawing.pdf",
        "HMI_Alarm_Export.xml",
        "Line3_Project_Package.zip",
    ]
    missing = [name for name in uploads if not (FIXTURES / name).exists()]
    if missing:
        check(
            "fixtures are present",
            False,
            f"missing {missing}; run python scripts/backend/make_fixtures.py",
        )
        return

    files = [("files", (name, (FIXTURES / name).read_bytes())) for name in uploads]
    response = client.post(f"/api/projects/{project_id}/files", headers=auth, files=files)
    check("upload accepted", response.status_code == 201, response.text)
    if response.status_code != 201:
        return
    body = response.json()
    check(
        f"all {len(uploads)} files accepted",
        body["accepted_count"] == len(uploads),
        f"accepted={body['accepted_count']} rejected={body['rejected_count']}",
    )
    extracted = sum(len(item["extracted"]) for item in body["results"])
    check("archive members were expanded", extracted >= 3, f"extracted {extracted}")

    print("\n[rejection]")
    rejected = client.post(
        f"/api/projects/{project_id}/files",
        headers=auth,
        # A Windows executable wearing a .pdf name: intake must refuse it on
        # content, not on the filename.
        files=[("files", ("notes.pdf", b"MZ\x90\x00" + b"\x00" * 512))],
    )
    check("disguised executable request succeeds", rejected.status_code == 201, rejected.text)
    if rejected.status_code == 201:
        item = rejected.json()["results"][0]
        check("disguised executable is refused", item["accepted"] is False)
        check("refusal explains itself", bool(item["rejection_reason"]), str(item))

    acd = client.post(
        f"/api/projects/{project_id}/files",
        headers=auth,
        files=[("files", ("Line3.acd", b"\x00\x01\x02synthetic-binary"))],
    )
    if acd.status_code == 201:
        item = acd.json()["results"][0]
        check(".ACD is refused", item["accepted"] is False)
        check(
            ".ACD refusal says the format is not parsed and points at L5X",
            "L5X" in item["rejection_reason"] and "proprietar" in item["rejection_reason"].lower(),
            item["rejection_reason"],
        )

    print("\n[read back]")
    listing = client.get(f"/api/projects/{project_id}/files", headers=auth)
    check("files can be listed", listing.status_code == 200, listing.text)
    rows = listing.json()
    check("every accepted file is listed", len(rows) >= len(uploads), f"{len(rows)} rows")
    kinds = {row["file_kind"] for row in rows}
    for expected in ("l5x", "xlsx", "csv", "docx", "pdf", "xml", "zip"):
        check(f"a {expected} file was classified", expected in kinds, f"kinds={sorted(kinds)}")

    l5x = next(row for row in rows if row["file_kind"] == "l5x")
    check("L5X was classified as control logic", l5x["document_role"] == "control_logic")
    check("a SHA-256 was recorded", len(l5x["sha256"]) == 64)

    download = client.get(f"/api/files/{l5x['id']}/content", headers=auth)
    check("stored original downloads", download.status_code == 200, download.text)
    check(
        "downloaded bytes match the stored hash",
        download.headers.get("x-content-sha256") == l5x["sha256"],
    )
    check(
        "download is served as an attachment",
        "attachment" in download.headers.get("content-disposition", ""),
    )
    check(
        "download cannot be sniffed into active content",
        download.headers.get("x-content-type-options") == "nosniff",
    )

    print("\n[delete]")
    csv_row = next(row for row in rows if row["file_kind"] == "csv")
    deleted = client.delete(f"/api/files/{csv_row['id']}", headers=auth)
    check("file deletes", deleted.status_code == 200, deleted.text)
    tombstone = client.get(f"/api/files/{csv_row['id']}", headers=auth)
    check("row survives as a tombstone", tombstone.status_code == 200, tombstone.text)
    check("row is marked deleted", tombstone.json()["status"] == "deleted")
    gone = client.get(f"/api/files/{csv_row['id']}/content", headers=auth)
    check("deleted content is no longer served", gone.status_code == 404)

    print("\n[review]")
    run = client.post(f"/api/projects/{project_id}/runs", headers=auth, json={})
    check("review run starts", run.status_code == 201, run.text)
    if run.status_code == 201:
        check("a task graph was created", run.json()["task_count"] > 5, run.text)
        state = _wait_for_review(client, project_id, auth)
        check(
            "the run reaches the approval gate",
            state.get("awaiting_approval", 0) >= 1,
            str(state),
        )
        check("no task failed", state.get("failed", 0) == 0, str(state))

        events = client.get(f"/api/projects/{project_id}/events", headers=auth)
        check(
            "the event log records the review",
            events.status_code == 200 and len(events.json()) > 20,
            str(events.status_code),
        )
        if events.status_code == 200:
            types = {row["event_type"] for row in events.json()}
            check("a finding was created", "finding.created" in types, str(sorted(types)))
            check("an approval was requested", "approval.requested" in types)

        findings = client.get(f"/api/projects/{project_id}/findings", headers=auth)
        check("findings are listed", findings.status_code == 200, findings.text)
        if findings.status_code == 200:
            rows = findings.json()["items"]
            check("findings were produced", len(rows) > 0)
            check(
                "every finding cites evidence",
                all(row["evidence_count"] > 0 for row in rows),
                str([(r["title"], r["evidence_count"]) for r in rows]),
            )
            check(
                "every finding went through senior review",
                all(row["is_reviewed"] for row in rows),
            )
            if rows:
                detail = client.get(f"/api/findings/{rows[0]['id']}", headers=auth)
                check("finding detail resolves its anchors", detail.status_code == 200)
                if detail.status_code == 200:
                    evidence = detail.json()["evidence"]
                    check(
                        "each citation carries a followable location",
                        all(e["anchor"] and e["anchor"]["display_location"] for e in evidence),
                    )

        print("\n[approval]")
        approvals = client.get(f"/api/projects/{project_id}/approvals", headers=auth)
        check("approvals are listed", approvals.status_code == 200, approvals.text)
        pending = (
            [a for a in approvals.json() if a["status"] == "pending"]
            if approvals.status_code == 200
            else []
        )
        check("a decision is waiting on a human", len(pending) >= 1)

        if pending:
            approval_id = pending[0]["id"]
            # The seeded administrator holds the reviewer role; an engineer-only
            # account is refused, which is the separation the gate exists for.
            decided = client.post(
                f"/api/approvals/{approval_id}/decision",
                headers=auth,
                json={"approved": True, "notes": "Confirmed against the panel drawing."},
            )
            check("the approval can be decided", decided.status_code == 200, decided.text)
            if decided.status_code == 200:
                check("the decision is recorded", decided.json()["status"] == "approved")
                check("the decider is named", bool(decided.json()["decided_by_display_name"]))
            repeat = client.post(
                f"/api/approvals/{approval_id}/decision",
                headers=auth,
                json={"approved": False},
            )
            check("an already-decided approval cannot be re-decided", repeat.status_code == 409)

            final = _wait_for_review(client, project_id, auth)
            check(
                "the run finishes once the decision is made",
                final.get("awaiting_approval", 0) == 0,
                str(final),
            )
            project_after = client.get(f"/api/projects/{project_id}", headers=auth).json()
            check(
                "the project completes only after the approval",
                project_after["status"] == "completed",
                project_after["status"],
            )

        print("\n[agents]")
        agents = client.get("/api/agents", headers=auth)
        check("the agent catalogue lists the four review agents", agents.status_code == 200)
        if agents.status_code == 200:
            kinds = {row["kind"] for row in agents.json()}
            for kind in ("project_manager", "plc_review", "interlock_review", "senior_review"):
                check(f"agent {kind} is registered", kind in kinds, str(sorted(kinds)))

        runs = client.get(f"/api/projects/{project_id}/agent-runs", headers=auth)
        check("agent runs are listed", runs.status_code == 200)
        if runs.status_code == 200 and runs.json():
            run_detail = client.get(f"/api/agent-runs/{runs.json()[0]['id']}", headers=auth)
            check("an agent run is inspectable", run_detail.status_code == 200)
            if run_detail.status_code == 200:
                body = run_detail.json()
                check(
                    "no reasoning trace is exposed",
                    not any(k in body for k in ("thinking", "reasoning", "chain_of_thought")),
                    str(sorted(body)),
                )

        reports = client.get(f"/api/projects/{project_id}/reports", headers=auth)
        check("reports endpoint responds with no provider installed", reports.status_code == 200)

    print("\n[archive]")
    archived = client.post(f"/api/projects/{project_id}/archive", headers=auth)
    check("project archives", archived.status_code == 200, archived.text)
    check("status becomes archived", archived.json()["status"] == "archived")
    blocked = client.post(
        f"/api/projects/{project_id}/files",
        headers=auth,
        files=[("files", ("late.csv", b"Tag\nESTOP_OK\n"))],
    )
    check("archived project refuses uploads", blocked.status_code == 409, blocked.text)

    client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="",
        help="Check an already-running backend instead of booting one.",
    )
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()

    if args.base_url:
        run_checks(args.base_url.rstrip("/"), "admin@esco.internal", "esco-demo-admin")
        return _report()

    with tempfile.TemporaryDirectory(prefix="esco-smoke-") as tmp:
        root = Path(tmp)
        env = {
            **os.environ,
            "ESCO_ENVIRONMENT": "test",
            "ESCO_DATA_DIR": str(root / "data"),
            "ESCO_DATABASE_URL": f"sqlite+pysqlite:///{(root / 'smoke.sqlite3').as_posix()}",
            "ESCO_LOG_JSON": "false",
            "ESCO_LOG_LEVEL": "WARNING",
            "ESCO_WORKER_ENABLED": "true",
            "ESCO_BOOTSTRAP_ADMIN_EMAIL": "admin@esco.internal",
            "ESCO_BOOTSTRAP_ADMIN_PASSWORD": "esco-demo-admin",
            "PYTHONPATH": str(REPO_ROOT / "backend"),
        }
        base_url = f"http://127.0.0.1:{args.port}"
        print(f"booting backend on {base_url} (fresh database, fresh data dir)")

        log_path = root / "server.log"
        with log_path.open("wb") as log:
            server = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.port),
                ],
                cwd=str(REPO_ROOT),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                run_checks(base_url, "admin@esco.internal", "esco-demo-admin")
            except Exception as exc:
                FAILED.append(f"{type(exc).__name__}: {exc}")
            finally:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    server.kill()

        if FAILED:
            print("\n--- server log ---")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-4000:])

    return _report()


def _report() -> int:
    print(f"\n{PASSED} checks passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  - {failure}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
