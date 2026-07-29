# Claude handoff — backend, data, security, agent orchestration

Owner of `backend/app/{api,core,database,models,parsers,orchestration,security}`,
`backend/app/agents/{base,review}`, `backend/tests`, `contracts`, `infra/backend`,
`scripts/backend`, `docs/backend`.

**Last updated:** Milestone 0
**Contract version:** `1.0.0`

---

## Status

| Milestone | Scope | State |
| --- | --- | --- |
| 0 | Foundation, core schemas, contracts, health endpoint | ✅ complete |
| 1 | Persistence, authentication, audit, storage | ⏳ next |
| 2 | Parsers and normalised evidence graph | ⏳ |
| 3 | Workflow engine and WebSockets | ⏳ |
| 4 | Four review agents and provider adapters | ⏳ |
| 5 | Full API and backend integration tests | ⏳ |
| 6 | Security review, documentation, handoff | ⏳ |

### Commits

| Milestone | Commit | Message |
| --- | --- | --- |
| 0 | _(see below)_ | `foundation: establish backend and contracts v1` |

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

Checks:

```bash
.venv/Scripts/python -m ruff format backend scripts
.venv/Scripts/python -m ruff check backend scripts
.venv/Scripts/python -m mypy
.venv/Scripts/python -m pytest
.venv/Scripts/python scripts/backend/generate_contracts.py --check
```

Defaults with no configuration: SQLite at `data/esco_city.sqlite3`, MockProvider,
no network access, no external service.

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

As of Milestone 0:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness, contract version, enabled providers, capabilities |
| `GET` | `/api/system/capabilities` | Optional plugin capabilities and why any are missing |

The full endpoint set (auth, projects, files, workflow, agents, findings,
approvals, reports, events, WebSocket) lands across Milestones 1–5. This table
is updated as each ships — an endpoint is listed here only once it works.

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

* **Milestone 0 only.** Only the health endpoints are implemented. No auth, no
  file upload, no parsers, no workflow, no agents yet.
* **No Alembic migrations yet** — the schema is currently created from
  `Base.metadata`. Migrations land in Milestone 1; until then a schema change
  means deleting your local `data/esco_city.sqlite3`.
* **No live WebSocket.** Contract is frozen; transport lands in Milestone 3.
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
