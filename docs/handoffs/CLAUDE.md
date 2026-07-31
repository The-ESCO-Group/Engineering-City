# Claude handoff — backend, data, security, agent orchestration

Owner of `backend/app/{api,core,database,models,parsers,orchestration,security}`,
`backend/app/agents/{base,review}`, `backend/tests`, `contracts`, `infra/backend`,
`scripts/backend`, `docs/backend`.

**Last updated:** Milestone 6 — Claude-owned scope complete
**Contract version:** `1.0.0`
**Tests:** 999 passing · ruff, mypy clean · 72/72 live-process smoke checks

---

## Status

| Milestone | Scope | State |
| --- | --- | --- |
| 0 | Foundation, core schemas, contracts, health endpoint | ✅ complete |
| 1 | Persistence, authentication, audit, storage | ✅ complete |
| 2 | Parsers and normalised evidence graph | ✅ complete |
| 3 | Workflow engine and WebSockets | ✅ complete |
| 4 | Four review agents and provider adapters | ✅ complete |
| 5 | Full API and backend integration tests | ✅ complete |
| 6 | Security review, documentation, handoff | ✅ complete |

### Commits

| Milestone | Commit | Message |
| --- | --- | --- |
| 0 | `08c3cfb` | `foundation: establish backend and contracts v1` |
| 1 | `c74b812` | `persistence: authentication, audit, storage and safe intake` |
| 2 | `751ba54` | `parsers: seven file parsers and the cross-file evidence graph` |
| 3+4 | `1303c08` | `workflow: persisted queue, live events and the four review agents` |
| 5 | `4869889` | `api: findings, approvals, agents and reports` |
| 6 | `3d79213` | `security: review, documentation and handoff` |
| — | _(this commit)_ | `config: model-configuration endpoints and checkpoint durability` |

---

## Milestone 0 — what exists now

**Domain vocabulary** — `backend/app/core/enums.py` holds all 25 published
enums (project/task/finding/agent statuses, severities, anchor types, event
types, audit actions, action types). These values are contract surface: members
are added, never renamed.

**ORM** — `backend/app/models/` defines all 22 required entities across 25
tables, with UUID primary keys and timezone-aware UTC timestamps. Portable
column types (`backend/app/database/types.py`) mean the same models run on
PostgreSQL and SQLite.

**AgentAction** — `backend/app/agents/base/actions.py`. The complete surface
through which a model can affect the system. Two rules are enforced by the
schema rather than by prompt wording:

* `create_finding` / `create_recommendation` are rejected without at least one
  evidence anchor;
* a finding in a safety-relevant category must state a
  `suggested_review_action`.

`extra="forbid"` everywhere, so a model cannot widen the action surface.

**Plugin interfaces** — `backend/app/agents/base/plugins.py`. `DeliveryAgentPlugin`,
`ReportProvider`, `ExportFormatProvider` plus discovery. See *Plugin interfaces*
below.

**Health** — `GET /api/health`, `GET /api/system/capabilities`.

**Contracts** — `contracts/` is generated from the running app. See below.

---

## Milestone 1 — what exists now

**Authentication** — Argon2id password hashing, HS256 access tokens with a `jti`
so `POST /api/auth/logout` genuinely revokes rather than gesturing. Login is
local only; no outbound call, no external identity provider. Failure messages are
identical for unknown-email, wrong-password and disabled-account, and an unknown
account still pays the cost of a real hash verification so response timing cannot
enumerate addresses.

**Authorisation** — two layers, both required: a global role
(`engineer`/`reviewer`/`administrator`) and per-project membership. Holding the
global `engineer` role does **not** grant access to another engineer's project.
Recording an approval needs both the global `reviewer` role and a reviewing role
on the project. Administrators are treated as owners everywhere so an orphaned
project can be recovered.

**Audit** — append-only, written in the *same transaction* as the change it
describes, so an operation that rolls back never leaves an entry claiming it
happened. Credential-shaped keys in `details` are redacted. Covers project
creation/update/archive, file upload/rejection/deletion, login success **and
failure**, logout, user creation and permission changes.

**Storage** — `data/projects/{id}/{originals,derived,reports}/`. Originals are
written once, sealed read-only, and never reopened for writing, so an evidence
anchor taken today still refers to the same bytes tomorrow. SHA-256 on every
blob. Deleting a file removes the bytes but keeps the row, so a citation resolves
to a tombstone instead of silently disappearing.

**Safe intake** — allowlist by content *and* extension. Refuses executables by
magic bytes regardless of filename (`MZ`, ELF, Mach-O, shebang, OLE), refuses
dangerous extensions, and refuses `.ACD`/`.doc`/`.xls` with a message that says
why and what to upload instead. A content/extension mismatch is accepted, routed
by content, and **warned about** rather than silently corrected.

**Safe archives** — the whole central directory is validated before a single byte
is written: entry count, total uncompressed size, compression ratio (zip-bomb),
and every member name proved containable. Zip Slip, backslash traversal, absolute
paths and drive prefixes are all refused; symlink and device members are skipped
with a reason; a member that understates its size is caught mid-copy. A rejected
archive leaves nothing behind, and the scratch tree is cleaned up unconditionally.

**Safe XML** — one hardened entry point (`app.security.xml_safety`). DTDs and
entities are disabled, so XXE and billion-laughs are refused; nesting depth and
document size are bounded. An XML upload carrying a DOCTYPE is refused at intake
with an explanation rather than failing opaquely later.

**Migrations** — Alembic, one revision, round-trips clean (`upgrade` → `check` →
`downgrade base` → `upgrade`) and `alembic check` confirms the migration matches
the ORM. `render_as_batch` is on so SQLite can alter tables at all.

### Defects found and fixed by an adversarial review of this milestone

Worth recording because all of them passed a fully green test suite first:

* **Enum columns did not round-trip.** `Mapped[ProjectStatus]` with `String(32)`
  stores correctly but loads back as a plain `str`, so
  `status is ProjectStatus.ARCHIVED` was `False` for every persisted row. This
  made archived projects still accept uploads, and made `DELETE /api/files/{id}`
  return 500 *after* unlinking the blob. Fixed with a `StrEnumType` decorator
  registered in `Base.type_annotation_map` (no DDL change, no migration), plus a
  structural test that fails for any future enum column that forgets it.
* **The seeded administrator could never sign in.** `admin@esco.local` — and
  every `.local`/`.test`/`.invalid` address — is rejected by `EmailStr`. Seeded
  accounts moved to `esco.internal`, and `Settings` now refuses to start with a
  bootstrap email the login endpoint would reject.
* **Failed logins were rolled back.** The audit entry was added and then the route
  only committed on success, discarding the entire record of refused sign-ins.
* **PK bytes were classified by extension.** An archive named `.csv` was routed to
  a text parser and its contents silently omitted from the review.
* Archive fixes: integrity check ran before the zip-bomb limits; a traversing
  member left already-written files behind; over-nesting was reported as a
  traversal attempt.

---

## Milestone 2 — what exists now

**Seven parsers** under `backend/app/parsers/`: L5X, XLSX, CSV, DOCX, PDF, XML and
plain text. Each turns one file into a normalised document plus evidence anchors,
under three rules that are product requirements rather than style guidance:

1. **Never guess silently.** Unreadable content becomes a warning or an
   unsupported item, never an omission. A review that reports nothing because the
   parser gave up is indistinguishable from a clean result.
2. **Never claim more than the format supports.** `GRT(AIR_PRESS_PSI, 40.0)` is a
   mnemonic and two operands — not "trips below 40 psi", and not "contradicts the
   documented 60".
3. **Be deterministic.** Same bytes, same content, same anchors, same order.

The PDF parser carries the sharpest version of rule 1: it never silently uses OCR
and never returns a quietly empty page. A scanned drawing produces a
`no_text_layer` warning, an unsupported item, and a page anchor marked
`unsupported_content` whose expectation says manual review is required — so "we
could not read this" survives into the report as a citable fact.

**The evidence graph** (`app/core/services/evidence_graph.py`) joins every parsed
artifact into one cross-file structure: tags, documented interlocks, rungs, I/O
points, alarms, narrative statements, and the limitations carried forward from
each parser. A discrepancy only exists *between* documents, so this is where the
product's value actually is.

It assembles facts and never draws conclusions. An interlock whose trip tag
matches nothing gets `matched_tag=None` plus ranked `candidate_tags`; a threshold
disagreement is recorded as both values verbatim. Whether those constitute a
finding is an agent's judgement, with evidence and a confidence, for a human to
approve.

**Anchors** are keyed `(file_id, anchor_key)` with a deterministic parser-assigned
key, so re-parsing updates rows in place and the findings that cite them keep
working across a parser upgrade. `content_hash` fixes the excerpt at creation
time, so a re-upload that changed the underlying file is detectable rather than
silently re-pointing an old finding at new text.

See `docs/backend/parsers.md` and `docs/backend/evidence.md`.

### What an adversarial review of this milestone found

Each parser was implemented, then independently attacked. 53 problems were
confirmed; the 40 at medium severity and above were fixed with a regression test
each, and 14 were declined with reasoning. The pattern worth recording is that
almost every one was a *silent* failure — content dropped with no warning, or a
claim the code could not support:

* an L5X alias tag's target was discarded, so the binding between an interlock
  symbol and its physical I/O point vanished without trace;
* only the first `<Tags>` container was read, so a declared tag was reported as
  undeclared — a false finding, not a missed one;
* tracked changes inside DOCX table cells dropped text, and the mutilated string
  was then published as a *direct* anchor excerpt an agent would quote verbatim;
* pages pypdf silently dropped were never noticed, so a partially-read document
  reported as fully examined;
* XML mixed content lost child text and fabricated separators inside identifiers;
* a spreadsheet cell whose text merely began with `=` was recorded as a formula
  with a null value, turning a documented threshold into a blank field.

The graph had two of its own, found by running it over the full fixture set:
members of a declared UDT (`CV101_Status.Running`) read as undeclared references,
turning one genuine finding into seven false leads; and rows from a *hidden*
worksheet were being treated as live requirements.

---

## Milestones 3 and 4 — what exists now

**The queue is the database.** No Redis. A worker claims a task by writing a
lease; if the process dies the lease expires and a sweep requeues it. At-least-
once, so every handler is idempotent — `Task.idempotency_key` stops duplicate
tasks and each handler converges rather than duplicating.

**A run** is: intake → one parse task per file → evidence graph → the Project
Manager Agent plans → the specialists it scheduled → senior review → the approval
gate → delivery → finalise. The prelude is fixed because it is mechanical; the
specialists are planned because which ones are worth running depends on what was
actually uploaded.

**Restart recovery.** A task left `running` by a killed worker returns to the
queue rather than sitting there forever, and the attempt is not double-counted —
a crash costs one attempt, not two.

**Events** are persisted before broadcast, with a gap-free per-project `seq`.
`GET /api/projects/{id}/events?since_seq=N` and
`WS /ws/projects/{id}?since_seq=N` are interchangeable for catching up. The
socket answers with a `hello` frame carrying `current_seq` and
`replay_truncated`; when that is true the client must refetch over REST rather
than assume it has a complete picture.

**Providers.** `mock` (deterministic, offline, the default), `ollama` (local),
`openai` and `anthropic`. The last two send project content off this machine, so
they cannot even be *constructed* unless an administrator lists them in
`ESCO_ENABLED_PROVIDERS` and supplies a credential. There is no fallback from a
failing provider to another one.

**The MockProvider is not a model and says so.** It is a fixed rule engine that
restates what the evidence graph already established, every response carries a
warning to that effect, and its confidence is capped. Nobody should be able to
mistake a mock run for an engineering review.

**The four agents.** Their permissions are narrow and enforced in code, not
prompt wording:

| Agent | May propose | Notably may **not** |
| --- | --- | --- |
| Project Manager | tasks, messages, blockers | create a finding — so it cannot draw a technical conclusion |
| PLC Review | findings, recommendations, messages | claim anything about a running controller |
| Interlock Review | findings, recommendations, approvals, messages | — |
| Senior Review | review verdicts, approvals, messages | create a finding, or **raise** a confidence |

**What the runner refuses**, before anything reaches the database: an action type
outside the closed set; a payload that fails its model; a technical claim with no
evidence anchor; an anchor id that does not belong to this project; a safety- or
damage-related recommendation marked as not needing approval; a senior review
that tries to raise confidence. Refusals are recorded on the run — what a model
*tried* to do is part of the record.

### The full review, over the synthetic project

Every planted case in `fixtures/README.md` comes out right, on the MockProvider,
with no network:

| Case | Outcome |
| --- | --- |
| ILK-003 documented, wired, never implemented | **high**, approval raised, project halts at `awaiting_review` |
| ILK-004 same interlock, different name | medium — not inflated, because it *is* implemented |
| ILK-007 no tag, no threshold | documentation gap; no threshold invented |
| `ALM_HOPPER_HIGH` undeclared | medium, cited to the exact rung |
| Duplicate rung | low |
| ILK-001, ILK-002, ILK-005 (correct) | **no findings** — no false positives |
| `Scanned_Drawing.pdf` | reported as unreadable, never as reviewed |

The project reaches `awaiting_review`, **not** `completed`, while that approval
is outstanding.

### Defects an adversarial review of this milestone found

* **A failed dependency stalled a run forever.** The queue ignored
  `TaskDependency.hard`, so one unreadable document left the graph task — and
  everything behind it — queued permanently while the project still reported
  itself `running`. No error, nothing to act on.
* **Blocked tasks hid runnable ones.** The claim window was sized from `limit`,
  so a run of dependency-blocked tasks starved the work behind them.
* **The duplicate-enqueue recovery called `db.rollback()`**, discarding the
  caller's whole transaction — inside `start_run` that is every task, dependency,
  audit row and event created so far.
* **Handlers were only registered when an in-process worker was enabled**, so an
  API process paired with an external worker reported every task kind as
  unhandled.
* **The WebSocket used the process-global settings**, rejecting tokens the REST
  API had just issued whenever the app was built with explicit settings.
* **The graph fabricated anchor keys no parser emits** (`range:Interlocks!row5`
  against the parser's `range:Interlocks!A5:H5`). Nothing errored — the Interlock
  Review Agent simply had nothing to cite and produced no discrepancies at all.
  There is now a test asserting that *every* citation the graph emits resolves to
  a real anchor, for every file type.

---

## Work for GPT — the outstanding half

The backend side of the remaining work is done (model-configuration endpoints and
checkpoint durability, below). What is left is all in GPT-owned directories, so I
have not touched any of it. In rough priority order.

### 1. Delivery agents — `backend/app/agents/delivery/` *(reserved for you)*

`GET /api/system/capabilities` currently reports `delivery_agents: false`, which
is accurate: nothing is installed, so a review produces findings but no
deliverables. The workflow already has a `delivery` task that completes with
`delivery_available: false` and an explanation, so nothing breaks — it just does
nothing.

To install one, create `backend/app/agents/delivery/__init__.py` exposing either
a `register(registry)` function or a module-level `DELIVERY_AGENTS` sequence:

```python
def register(registry):
    registry.add_delivery_agent(TestPlanAgent())
```

`DeliveryAgentPlugin` needs `kind: str`, `display_name: str`, `version: str`,
`handles_task_kinds: Sequence[str]`, and `run(context) -> AgentRunResult`.

Two things worth knowing:

* **Delivery agents run *after* human approval**, so they should read only
  findings whose `status` is `accepted`. Anything else has not been agreed by an
  engineer and must not reach a deliverable.
* **`Task.checkpoint` is the tool for a long generation.** A delivery agent
  producing twenty reports is one task doing twenty things, so use
  `context.mark_done(item)` / `context.done()` to record progress — the queue is
  at-least-once and a crashed task is re-run from the start otherwise. Durability
  is proved by `backend/tests/orchestration/test_checkpoints.py`. The core
  handlers do not use it because the workflow already decomposes to one task per
  file, which is finer grained; yours will not have that luxury.

### 2. Report providers — `backend/app/reporting/` *(reserved for you)*

`ReportProvider` needs `report_type: str`, `display_name: str`, `version: str`,
`supported_formats: Sequence[str]`, `render(snapshot, export_format)`.
`ExportFormatProvider` needs `format_key`, `media_type`, `file_extension`,
`version`, `serialize(document) -> bytes`.

Write output through `ProjectStorage.write_report()` and record a `Report` row;
`GET /api/reports/{id}/content` will then serve it. The `Report` model already
carries `finding_ids`, so a report stays traceable to the findings it was built
from — please populate it.

The obvious first two, given the fixture: a **discrepancy matrix** (the interlock
comparison as a table) and a **test plan** derived from the accepted findings.

### 3. Frontend integration — `frontend/`

These endpoints are live and contract-generated; the city view is not yet reading
them:

| Endpoint | What it gives the UI |
| --- | --- |
| `WS /ws/projects/{id}` | Live `agent.status_changed`, `agent.location_changed`, `task.*`, `finding.created`, `approval.requested` — everything the city animates on |
| `GET /api/projects/{id}/agents` | Agent presence: `status`, `location`, `current_activity` |
| `GET /api/projects/{id}/messages` | The activity feed |
| `GET /api/projects/{id}/findings` | Findings list, paginated and filterable |
| `GET /api/findings/{id}` | Every citation resolved, with `display_location` |
| `GET /api/projects/{id}/approvals` | The decision queue |
| `POST /api/approvals/{id}/decision` | The approval gate |

Four things the UI needs to get right, because they are product promises rather
than presentation choices:

1. **Never show `fact` and `inference` as one block.** They are separate fields
   precisely so a deduction is never displayed as an observation.
2. **Always show `limitations` and `confidence`.** Hiding uncertainty is the
   failure this product exists to prevent.
3. **`display_location` is the citation.** It is written to be followable
   verbatim — "MainProgram / Conveyor_Interlocks / Rung 0" — so show it, do not
   summarise it.
4. **A finding with `requires_approval: true` is not a result yet.** It needs an
   engineer's decision before it can be presented as one.

### 4. Smaller items

* **`GET /api/projects/{id}/parsers`** reports which file kinds this installation
  can read, and why any cannot. Worth surfacing at upload time so a user learns a
  format is unsupported before they wait for a review.
* **Provider badge.** `GET /api/model-configurations/providers` says which
  providers are enabled and which send content off the machine. If a project ran
  on the MockProvider, the UI should say so — every agent run carries a warning
  to that effect in `AgentRunDetail.warnings`, and a demo run must not look like
  a real review.

### Not requests, just notes

* Do not hand-edit `contracts/` — it is generated and `test_contracts.py` fails on
  drift. If you need a field that is not there, note it in `docs/handoffs/GPT.md`
  and I will add it to the source model and regenerate.
* `fixtures/demo_project/` is a full synthetic project you can develop against,
  and `fixtures/README.md` documents exactly which findings a correct review
  produces — useful for building the UI against known output.

---

## Definition of done

Every item verified, most of them against a live process rather than only in
tests.

| Requirement | Verified by |
| --- | --- |
| The backend starts locally | `scripts/backend/smoke.py` boots uvicorn on a fresh database |
| A user can authenticate | 72/72 smoke checks; `test_auth_api.py` |
| A project can be created | smoke + `test_projects_api.py` |
| Multiple files can be uploaded | 9 fixtures in one request, partial acceptance |
| Files are safely stored and parsed | 7 parsers, sealed read-only originals, SHA-256 |
| Evidence anchors are produced | ~500 anchors over the fixture set, every citation resolves |
| A workflow can start, pause, resume, fail and recover | `test_engine.py`, `test_worker.py` |
| Claude-owned agents run on the MockProvider | 4 agents, no network, deterministic |
| Findings contain exact evidence | every finding cites an anchor with a followable location |
| Findings reach human approval | the high-severity interlock halts the project at `awaiting_review` |
| Events persist and broadcast over WebSockets | gap-free `seq`, replay, live frames |
| The backend works after a restart | killed mid-run; a fresh process finished the review |
| All backend tests pass | 994 passing, ruff and mypy clean |
| Contracts complete enough for the frontend | 39 endpoints, 27 event types, generated and drift-checked |
| No external model or cloud service required | MockProvider is the default; cloud providers cannot be constructed unless enabled |

### The demonstration, in one command

```bash
python scripts/backend/smoke.py
```

Boots a real server on a throwaway database, signs in, creates a project,
uploads the nine synthetic documents, checks that a disguised executable and an
`.ACD` are refused with reasons, runs the full review, reads the findings and
their citations, approves the one that needs a human, and confirms the project
only reports itself complete afterwards. 72 assertions, no network.

---

## Running the backend

```bash
# from the repository root
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"          # Windows
# .venv/bin/pip install -e ".[dev]"            # Linux/macOS

cp .env.example .env                           # optional; defaults work

.venv/Scripts/python -m uvicorn app.main:app --app-dir backend --reload --port 8000
```

Interactive docs: <http://127.0.0.1:8000/docs>
Health: <http://127.0.0.1:8000/api/health>

Defaults with no configuration: SQLite at `data/esco_city.sqlite3`, MockProvider,
no network access, no external service. The schema is migrated and an
administrator is seeded on first boot.

Sign in with `admin@esco.internal` / `esco-demo-admin`. In demo mode
`engineer@esco.internal` / `esco-demo-engineer` and `reviewer@esco.internal` /
`esco-demo-reviewer` are seeded too, so each role can be exercised.

Checks:

```bash
.venv/Scripts/python -m ruff format backend scripts
.venv/Scripts/python -m ruff check backend scripts
.venv/Scripts/python -m mypy
.venv/Scripts/python -m pytest
.venv/Scripts/python scripts/backend/generate_contracts.py --check
.venv/Scripts/python scripts/backend/smoke.py          # boots a real server
```

`smoke.py` starts uvicorn against a throwaway database, signs in, creates a
project, uploads the whole synthetic fixture set, checks the rejection paths,
downloads and deletes a file, and archives the project — 42 assertions against a
live process rather than a TestClient.

Fixtures and migrations:

```bash
.venv/Scripts/python scripts/backend/make_fixtures.py  # regenerate fixtures
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m alembic check                  # models match migration?
.venv/Scripts/python -m alembic revision --autogenerate -m "describe it"
```

---

## Contracts

`contracts/` is **generated**, not hand-written:

```bash
python scripts/backend/generate_contracts.py           # write
python scripts/backend/generate_contracts.py --check   # fail if stale
```

`backend/tests/test_contracts.py` runs the check, so an API change that is not
reflected in the contracts fails the suite. Generate the frontend client from
these files — they cannot describe an endpoint the backend does not serve.

| File | Contents |
| --- | --- |
| `contracts/openapi.yaml` | Every HTTP endpoint |
| `contracts/events.schema.json` | Event envelope + payload per event type |
| `contracts/domain.schema.json` | 25 enums, 80 entity definitions |
| `contracts/README.md` | How to consume them |

`contracts/README.md` also lists what this API deliberately will never do
(controller connections, program upload/download, `.ACD` parsing, applying
design changes, releasing safety recommendations without human approval).

---

## Available endpoints

Every endpoint below is implemented, tested and exercised against a live process
by `scripts/backend/smoke.py`. Nothing is listed here until it works.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness, contract version, enabled providers, capabilities |
| `GET` | `/api/system/capabilities` | Optional plugin capabilities and why any are missing |
| `POST` | `/api/auth/login` | Sign in; returns a token and sets an httpOnly cookie |
| `POST` | `/api/auth/logout` | Revoke the presented token |
| `GET` | `/api/auth/me` | Current user and their per-project roles |
| `POST` | `/api/auth/users` | Create a user (administrator only) |
| `GET` | `/api/auth/users` | List users (administrator only) |
| `POST` | `/api/projects` | Create a project |
| `GET` | `/api/projects` | List projects the caller may see (paginated) |
| `GET` | `/api/projects/{project_id}` | Detail with membership and roll-up counts |
| `PATCH` | `/api/projects/{project_id}` | Update metadata (**not** status) |
| `POST` | `/api/projects/{project_id}/archive` | Archive; read-only from then on |
| `GET` | `/api/projects/{project_id}/members` | List members |
| `POST` | `/api/projects/{project_id}/members` | Add a member or change their role |
| `POST` | `/api/projects/{project_id}/files` | Upload one or more files |
| `GET` | `/api/projects/{project_id}/files` | List a project's files |
| `GET` | `/api/files/{file_id}` | File metadata, artifacts, anchor count |
| `GET` | `/api/files/{file_id}/content` | Download the stored original |
| `DELETE` | `/api/files/{file_id}` | Delete a file (row retained as a tombstone) |
| `GET` | `/api/agents` | Every registered agent, and why any is unavailable |
| `GET` | `/api/projects/{project_id}/agents` | Agent presence for the city view |
| `GET` | `/api/projects/{project_id}/agent-runs` | Agent runs for a project |
| `GET` | `/api/agent-runs/{run_id}` | One run, including the actions the backend **refused** |
| `GET` | `/api/projects/{project_id}/messages` | The project activity feed |
| `GET` | `/api/projects/{project_id}/findings` | List findings (paginated, filterable) |
| `GET` | `/api/findings/{finding_id}` | Finding detail with every citation resolved |
| `PATCH` | `/api/findings/{finding_id}` | Record a human disposition (reviewer role) |
| `POST` | `/api/findings/{finding_id}/comment` | Add a reviewer note |
| `GET` | `/api/findings/{finding_id}/comments` | Reviewer notes |
| `GET` | `/api/projects/{project_id}/recommendations` | Proposed engineering changes |
| `GET` | `/api/projects/{project_id}/approvals` | Decisions waiting on a human |
| `GET` | `/api/approvals/{approval_id}` | One approval with its evidence snapshot |
| `POST` | `/api/approvals/{approval_id}/decision` | **Record an engineering decision** |
| `GET` | `/api/projects/{project_id}/reports` | Reports generated for a project |
| `GET` | `/api/reports/{report_id}` | Report metadata |
| `GET` | `/api/reports/{report_id}/content` | Download a generated report |
| `GET` | `/api/model-configurations` | Configured provider/model rows (administrator) |
| `GET` | `/api/model-configurations/providers` | Enabled providers, and which send data off-machine |
| `PUT` | `/api/model-configurations/{agent_kind}` | Set an agent's provider and model (administrator) |
| `DELETE` | `/api/model-configurations/{agent_kind}` | Fall back to the next-broadest scope |
| `POST` | `/api/projects/{project_id}/runs` | Start a review run |
| `POST` | `/api/projects/{project_id}/pause` | Pause the run |
| `POST` | `/api/projects/{project_id}/resume` | Resume a paused run |
| `POST` | `/api/projects/{project_id}/cancel` | Cancel the run |
| `POST` | `/api/projects/{project_id}/recover` | Requeue tasks abandoned by a stopped worker |
| `GET` | `/api/projects/{project_id}/workflow` | Workflow state and task counts |
| `GET` | `/api/projects/{project_id}/tasks` | List tasks |
| `GET` | `/api/tasks/{task_id}` | Task detail with dependency edges |
| `GET` | `/api/projects/{project_id}/parsers` | Which file kinds this install can parse |
| `GET` | `/api/projects/{project_id}/events` | Replay the event history |
| `WS` | `/ws/projects/{project_id}` | Live event stream |

**Auth transport** — send `Authorization: Bearer <token>` *or* rely on the
`esco_session` httpOnly cookie that login sets. Both work; the browser frontend
should use the cookie, the desktop shell the header.

**Upload semantics** — `POST .../files` takes a multipart `files` list plus an
optional `document_role`. Each file is accepted or rejected **independently**:
the response is `201` with `accepted_count`, `rejected_count` and a per-file
result carrying `rejection_code` and `rejection_reason`. Uploading nine good
files and one executable keeps the nine. An uploaded ZIP is expanded and each
usable member appears in `extracted` as a file in its own right, with
`parent_file_id` and `archive_member_path` set.

**The approval gate.** `POST /api/approvals/{id}/decision` is the only way a
finding is released. It requires the global `reviewer` role *and* a reviewing
role on the project, so an engineer cannot sign off their own project's safety
findings. `approved` has no default — approving is always an explicit act. Once
every approval is decided the gated tasks return to the queue and the run
continues on its own.

**Reports** are produced by GPT-owned providers under `app.reporting`. With none
installed the list is legitimately empty and `/api/system/capabilities` says why;
the endpoints are not broken, the capability is absent.

---

## Error envelope

Every failing endpoint returns exactly one shape:

```json
{ "error": { "code": "file_rejected", "message": "…", "details": {} } }
```

`code` is stable and safe to branch on. The `X-Request-ID` response header
correlates a failure with the server log.

---

## WebSocket event types

Envelope: `id`, `schema_version`, `seq`, `event_type`, `project_id`, `agent_id`,
`task_id`, `actor_user_id`, `occurred_at`, `payload`.

27 event types are declared and mapped to payload models in
`contracts/events.schema.json` under `x-payload-by-event-type`:

`project.created` · `project.updated` · `project.completed` · `project.failed` ·
`file.uploaded` · `file.parsing_started` · `file.parsed` · `file.parse_failed` ·
`file.deleted` · `workflow.started` · `workflow.paused` · `workflow.resumed` ·
`workflow.cancelled` · `task.created` · `task.started` · `task.completed` ·
`task.failed` · `task.blocked` · `agent.status_changed` ·
`agent.location_changed` · `agent.message_created` · `finding.created` ·
`finding.reviewed` · `finding.updated` · `approval.requested` ·
`approval.decided` · `report.created`

Events are persisted before broadcast. `seq` is gap-free per project;
reconnecting clients pass `?since_seq=` and receive a `hello` frame stating
`current_seq`, how many events were replayed, and `replay_truncated` when the
requested point is outside the replay window.

> **Not yet emitting.** The types and payload shapes are frozen contract, but
> the transport and the emitters land in Milestone 3. Build against the schema;
> do not expect live frames yet.

---

## Plugin interfaces

Reserved for GPT, discovered from `app.agents.delivery` and `app.reporting`.
Neither package exists yet, and **the backend starts fine without them** — it
reports the missing capability instead of crashing. Verified by
`backend/tests/test_health.py::test_missing_plugins_are_reported_not_fatal`.

Register either by defining `register(registry)` in the package `__init__`, or
by declaring module-level `DELIVERY_AGENTS` / `REPORT_PROVIDERS` /
`EXPORT_FORMATS` sequences.

```python
# backend/app/agents/delivery/__init__.py
from .test_plan import TestPlanAgent

def register(registry):
    registry.add_delivery_agent(TestPlanAgent())
```

**`DeliveryAgentPlugin`** — `kind: str`, `display_name: str`, `version: str`,
`handles_task_kinds: Sequence[str]`, `run(context) -> AgentRunResult`.

**`ReportProvider`** — `report_type: str`, `display_name: str`, `version: str`,
`supported_formats: Sequence[str]`, `render(snapshot, export_format) -> RenderedReport`.

**`ExportFormatProvider`** — `format_key: str`, `media_type: str`,
`file_extension: str`, `version: str`, `serialize(document) -> bytes`.

A plugin that raises during import or registration is caught, recorded with its
traceback, and surfaced through `/api/system/capabilities`. It never prevents
startup.

> `AgentRunResult`, `RenderedReport` and the read-only context objects are
> defined in Milestone 4. Until then, treat the attribute names above as fixed
> and the return types as forthcoming.

---

## Known limitations

* **No parsers yet.** Files are stored, hashed, classified by kind and role, and
  safely expanded from archives — but nothing reads their *contents*. Parsers and
  evidence anchors land in Milestone 2, so `GET /api/files/{id}` currently
  reports zero artifacts and zero anchors for every file. That is accurate, not a
  placeholder.
* **No workflow, agents, findings, approvals or reports yet** (Milestones 3–5).
* **No live WebSocket.** Contract is frozen; transport lands in Milestone 3.
  Events *are* already persisted (`project.created`, `file.uploaded`,
  `file.deleted`) with gap-free per-project `seq`, so replay will have real
  history to serve.
* **Demo passwords are in the repository.** `esco-demo-admin` and the seeded demo
  accounts exist so the offline demonstration works out of the box.
  `ESCO_ENVIRONMENT=production` refuses to start until `ESCO_JWT_SECRET` and
  `ESCO_BOOTSTRAP_ADMIN_PASSWORD` are set, and `ESCO_BOOTSTRAP_DEMO_USERS=false`
  turns the demo accounts off.
* **Read-only sealing is best-effort.** Originals are chmod'ed read-only; some
  network and container filesystems ignore that, in which case immutability is a
  convention rather than an enforced property. A warning is logged when the chmod
  fails.
* **Per-project byte quota counts only live files.** Deleted rows are excluded, so
  reclaiming space works, but the derived/ tree is not counted against the quota.
* **PostgreSQL is untested in CI.** The models use portable types and the intent
  is full support, but only SQLite is exercised so far.
* **`.ACD` files are not supported and never will be.** The format is
  proprietary; the product does not claim otherwise. L5X exports are the
  supported Studio 5000 input.
* **PDF text extraction only, no silent OCR.** A scanned PDF will produce a
  warning that manual review or OCR is required rather than a quietly empty
  parse. (Milestone 2.)

---

## Requests for GPT

Nothing blocking yet. Noted for when the relevant milestone lands:

1. **Do not hand-edit `contracts/`.** It is generated and the test suite
   enforces it. If the frontend needs a field that is not there, note it in
   `docs/handoffs/GPT.md` and I will add it to the source model and regenerate.
2. **Root `.gitignore`** — I appended a Python/backend section beneath the
   existing Node entries rather than restructuring the file. Shout if you would
   rather split it per-directory.
3. **Branch** — `master` is the only branch and the initial commit is yours, so
   I am committing backend milestones there too. Say the word if you would
   prefer separate branches.
4. **Delivery/reporting packages** — `backend/app/agents/delivery/` and
   `backend/app/reporting/` are yours; I have not created them, so plugin
   discovery currently reports them as missing (by design). The interfaces above
   are stable enough to start against once Milestone 4 defines the result types.
5. **Health endpoint for the desktop shell** — `GET /api/health` returns
   `contract_version`. Consider failing fast in the client if it does not match
   the version the generated client was built from.
6. **Seeded demo credentials** — `admin@esco.internal` / `esco-demo-admin`. Note
   the domain: `.local`, `.test` and `.invalid` are reserved special-use domains
   and `EmailStr` rejects them, so don't use them in frontend fixtures either.
7. **Upload is partially-accepting by design.** `POST .../files` returns `201`
   even when some files were refused. Read `results[].accepted` per file and show
   `rejection_reason` — it is written to be shown to an engineer verbatim. Do not
   treat a non-zero `rejected_count` as a failed request.
8. **Deleted files keep their row.** `GET /api/files/{id}` still returns `200`
   with `status: "deleted"`; only `/content` becomes `404`. Render it as a
   tombstone so a citation that pointed at it does not silently vanish.
9. **Synthetic fixtures are available** for frontend work:
   `fixtures/demo_project/` holds a full invented project (L5X, interlock
   workbook, I/O list, functional description, PDFs including one with no text
   layer, HMI XML, and a ZIP package). `fixtures/README.md` documents the ground
   truth — exactly which discrepancies a correct review should and should not
   report. Please add only synthetic data; no ESCO customer content in the repo.
