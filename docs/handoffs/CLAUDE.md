# Claude handoff — backend, project core, workflow, PLC generation, Rockwell export

Owner of `backend/app/{api,core,database,models,parsers,orchestration,security,
engineering,generators,validation,adapters}`, `backend/app/agents/{base,review}`,
`backend/tests`, `contracts`, `templates`, `infra/backend`, `scripts/backend`,
`docs/backend`.

**Last updated:** 2026-08-05, at commit `01e289f`
**Contract version:** `1.1.0` — note the bump; it was `1.0.0` when this file last
said so
**Endpoints:** 65 HTTP + 1 WebSocket at `01e289f`; 68 + 1 in the working tree
(see [Uncommitted](#uncommitted-work-in-the-tree))
**Checks, run against the working tree on 2026-08-05:** `ruff check` clean ·
`mypy` clean (180 files) · `alembic` round-trips and `alembic check` passes *with*
the uncommitted revision `4f7a9c2d1e6b` and fails without it ·
`generate_contracts.py --check` **stale** on all three files · `pytest` **not
green**: `test_contracts.py` (2, the staleness above) and
`test_workflow_api.py::test_a_run_started_over_rest_is_streamed_to_a_connected_socket`
(1, `agent.status_changed` now interleaves with the `task.created` burst the test
expects contiguously). Do not read "the suite is green" out of an older version of
this file.

Read this file for what the API is and what it will not do. Read
[`docs/backend/project-core.md`](../backend/project-core.md) before writing to the
canonical model, and
[`docs/backend/release-pipeline.md`](../backend/release-pipeline.md) before
running the release pipeline for real — the second one carries the honest account
of what has and has not been verified, and it is the one to read before telling a
customer anything.

| Document | What it is for |
| --- | --- |
| `docs/backend/project-core.md` | Changing the canonical model, or adding an agent that writes to it |
| `docs/backend/release-pipeline.md` | Stages, the six gates, validation levels, the package, and what is unverified |
| `docs/backend/adr/0001-*.md` | Why the platform was extended rather than forked; revisions, generation, levels, what stays impossible |
| `docs/backend/adr/0002-*.md` | When a gate decision expires; what level a package may claim; proposal vs outcome on a change operation |
| `docs/backend/parsers.md`, `evidence.md` | The provenance substrate |
| `docs/backend/security.md` | Intake, archives, XML, authorisation |

---

## ⚠ Breaking changes for the frontend

Three of them, in increasing order of how quietly they break. The third is the
one that has not been written down before.

### 1. Semantic locations (previously documented, one correction)

`AgentInstance.location` and `AgentSpec.home_location` carry `SemanticLocation`
values from the brief's sixteen districts, replacing the four ad-hoc strings the
review platform used. The backend broadcasts a district name and never a
coordinate, so where a building sits on screen stays entirely yours.

| Was | Is now |
| --- | --- |
| `intake_dock` | `project_intake` |
| `logic_works` | `ladder_logic_lab` |
| `safety_annex` | `automation_core` |
| `review_hall` | `qa_campus` |

**Correction:** the full set is in `contracts/events.schema.json`, not
`contracts/domain.schema.json`. This file previously pointed at the wrong one.
All sixteen: `project_intake`, `requirements_hall`, `project_core`, `tag_foundry`,
`automation_core`, `ladder_logic_lab`, `electrical_works`, `hmi_district`,
`alarm_center`, `qa_campus`, `testing_grounds`, `approval_plaza`,
`documentation_quarter`, `release_logistics`, `template_library`, `data_center`.

### 2. Enums widened — a `switch` written against the old members now falls through

This is the larger break and it produces **no error anywhere**. Members were
added, never renamed, so nothing 404s, nothing fails validation, and no type
check fires. A `switch` with no `default` silently renders nothing; one with a
`default` renders the fallback for a state that is now normal.

Every one of these is generated into `contracts/`, so regenerating your client
picks up the values — but not the branches you have to write.

| Enum | Was | Now | Added |
| --- | --- | --- | --- |
| `ProjectStatus` | 8 | 17 | `clarification_required`, `tag_and_io_design`, `plc_architecture`, `implementation`, `cross_discipline_review`, `test_development`, `release_validation`, `ready_for_export`, `blocked` |
| `EventType` | 27 | 47 | `project.question_created`, `project.question_resolved`, `requirement.created`, `workflow.planned`, `workflow.failed`, `task.assigned`, `changeset.created`, `changeset.validation_failed`, `changeset.submitted`, `changeset.reviewed`, `changeset.abandoned`, `changeset.merged`, `revision.created`, `gate.status_changed`, `validation.started`, `validation.completed`, `export.started`, `export.artifact_created`, `release.candidate_created`, `release.ready` |
| `ApprovalStatus` | 4 | 7 | `changes_requested`, `risk_accepted`, `deferred` |
| `AuditAction` | 24 | 42 | profile, question, decision, changeset, revision, tag-rename, gate, validation, export, release and workstation actions |
| `TaskKind` | 11 | 21 | `extract_requirements`, `resolve_clarifications`, `design_tags`, `design_plc_architecture`, `generate_logic`, `validate_cross_discipline`, `evaluate_approval_gate`, `generate_release_candidate`, `verify_import`, `assemble_package` |
| `ActionType` | 8 | 14 | `create_task_plan`, `request_clarification`, `propose_changeset`, `review_changeset`, `generate_preview`, `complete_task` |
| `AgentKind` | 5 | 8 | `requirements_planning`, `tag_architecture`, `plc_logic` |
| `AnchorType` | 23 | 29 | `description_paragraph`, `requirement`, `template`, `human_decision`, `generated_object`, `validation_result` |
| `ApprovalKind` | 5 | 10 | `gate_decision`, `changeset_merge`, `tag_rename`, `restricted_domain`, `release_candidate` |
| `EvidenceKind` | 5 | 9 | `template_rule`, `human_decision`, `generated_object`, `validation_output` |

Four things to get right, because each is a specific way this bites:

* **`ProjectStatus` no longer means `running` while a production run is
  running.** Nine of the seventeen members are engineering *phases*, and
  `refresh_project_status` reports the phase of the earliest unfinished task
  rather than a bare `running`. A review run still reports `running`, because the
  phase members describe design and generation work a review run never does.
  Treating anything outside your known list as an error will report a healthy,
  working project as broken. `failed` and `completed` still exist and still mean
  what they meant; `blocked` and `clarification_required` mean "a human owes an
  answer", which is a call to action rather than a fault — the Line 4 acceptance
  run currently ends `blocked`.
* **`ApprovalStatus.risk_accepted` is not `approved`.** Both let the work
  proceed, and the whole reason the two are separate members is that a release
  which went ahead on knowingly carried risk must never afterwards look like one
  with no risk. Render it distinctly. `changes_requested` and `deferred` mean the
  request is settled but the *gate* is not — the work went back to the engineers.
* **Twenty new `EventType` members arrive on the same socket** as the twenty-seven
  you already handle. An unknown `event_type` must be ignored without breaking the
  stream, and `seq` must still be advanced, or a client that meets
  `changeset.merged` stops catching up.
* **`ApprovalGateKey`, `GateStatus` and `ApprovalDecision` are new enums, not
  widened ones.** A gate decision is `approve` | `reject` | `request_changes` |
  `accept_risk` | `defer`, and `reject`, `request_changes` and `accept_risk`
  **require** a non-empty comment — the API refuses the decision without one.

### 3. Roughly sixty new enums exist and most are **not** in `contracts/domain.schema.json`

`scripts/backend/generate_contracts.py` has a hand-maintained
`PUBLISHED_ENUMS` tuple, and it still lists the original 25. Everything added
since — `ObjectType` (46 members), `ValidationLevel`, `ValidationSeverity`,
`ValidationDiscipline`, `GateStatus`, `ApprovalGateKey`, `ApprovalDecision`,
`ReleaseCandidateStatus`, `ImportVerificationStatus`, `ChangeOperationType`,
`ChangeSetStatus`, `ConflictKind`, `MergeOutcome`, `RequirementStatus`,
`TagScope`, `TagRole`, `SignalType`, and the rest — is absent from the domain
schema unless it happens to appear inside a published model's own JSON schema.
Seven leak in that way: `ControllerFamily`, `HmiProduct`, `ProjectType`,
`ReleaseMode`, `RestrictedDomain`, `SafetyClassification`, `UnitSystem`.

So a generated client will have string types where it should have unions, for
exactly the enums the release and gate UI needs most. **This is mine to fix** —
tell me which you want first and I will add them to `PUBLISHED_ENUMS` and
regenerate rather than you hand-writing them. Do not hand-edit `contracts/`;
`backend/tests/test_contracts.py` fails on drift.

---

## Uncommitted work in the tree

At the time of writing the working tree is **not clean**, and the extra work is
not mine. Recorded here so nobody reads a number in this file and finds a
different one.

New, untracked: `backend/app/api/routes/validations.py`,
`backend/app/api/schemas/validation.py`,
`backend/app/core/services/validation_review.py`,
`backend/tests/api/test_validation_api.py`,
`backend/tests/engineering/test_logic_instruction_branch_gate.py`, and the Alembic
revision `4f7a9c2d1e6b`. Modified, among others:
`backend/app/api/routes/approvals.py`, `backend/app/engineering/gates.py`,
`backend/app/models/{revision,plc}.py`, `backend/app/core/enums.py`. It moved
twice while this file was being written, so treat every count here as of
2026-08-05 and re-derive rather than quote.

Three endpoints exist in the tree and not at `01e289f`:

| Method | Path |
| --- | --- |
| `POST` | `/api/approval-gates/{gate_id}/approvals/{approval_id}/decision` |
| `GET` | `/api/projects/{project_id}/validation-results` |
| `POST` | `/api/validation-results/{result_id}/waiver` |

All three are routed and reachable (verified: they answer `401`, not `404`). The
first closes the largest hole this handoff previously reported — a reviewer could
not pass an approval gate from outside the process at all — and it works:
`scripts/backend/smoke_production.py` now announces two of its `gap` entries as
`RESOLVED`, "a reviewer can decide an approval gate through the API" and "a
validation run completes". Two places still assert the hole exists and need
updating in the same commit that lands the endpoint: that script's `GATE_WALL`
constant, and the `xfail` reason on
`test_line4_end_to_end.py::test_a_gate_decision_has_an_http_door` — which is
`strict=True` and currently only still xfails because it enumerates
`application.routes` in a way that does not see routers included with a prefix.

Two consequences to expect while it is in flight. The ORM gained four columns —
`change_operations.values`, `change_operations.basis`,
`merge_results.validation_errors`, `logic_instructions.branch_id` — which had no
migration at `01e289f`; the untracked revision `4f7a9c2d1e6b` now covers them, so
`alembic check` passes with the tree as it stands and fails on a clean checkout of
`01e289f`. And `generate_contracts.py --check` reports all three contract files
stale, which is what fails `test_contracts.py`. Regenerating the contracts is the
fix and it belongs in whichever commit lands the endpoints.

---

## Production milestones

| Milestone | Scope | State |
| --- | --- | --- |
| 0 | Repository audit, ownership map, ADR 0001 | ✅ complete |
| 1 | Canonical project core, revisions, change sets | ✅ complete |
| 2 | Change-set validation, conflicts, merge, rename impact | ✅ complete |
| 3 | Logic IR, template registry, cross-discipline validators | ✅ complete |
| 4 | Requirements / Tag Architecture / PLC Logic agents, six approval gates | ✅ code complete; **not proved end to end** — see the release pipeline doc |
| 5 | Rockwell generators, release core, workstation adapter interface | ✅ complete |
| 6 | Line 4 acceptance fixture, end-to-end release workflow | ⚠ fixture and suite exist; the run parks at gate 2 |

Milestone 4 and 6 are marked honestly rather than generously. Every part is
written, tested in isolation and reachable from the API; what has never happened
is one production run going from a description to a sealed package. The stops are
enumerated as `xfail(strict=True)` markers in
`backend/tests/test_line4_end_to_end.py`, each with its diagnosis, and as `gap`
entries in `scripts/backend/smoke_production.py`. Those markers are the product's
state, not a to-do list somebody forgot.

### Commits

| Commit | Scope |
| --- | --- |
| `3317c5e` | Repository audit, ownership map, ADR 0001 |
| `99d9dcf` | Canonical project core, contracts, instruction catalogue, templates |
| `54d4311` | Change-set merge engine and cross-discipline validators |
| `7cb7036` | Savepoint-aware event broadcast (two silent defects) |
| `3ba46e1` | Deterministic Rockwell import-candidate generation |
| `ee9f51f` | Workstation adapter interface and the Line 4 demo fixture |
| `cf96d1d` | Rename impact, approval gates, the three engineering agents |
| `e342ddb` | Alembic could never migrate a PostgreSQL database with a real password |
| `4337403` | Local end-to-end city workflow |
| `42bf41a` | Deterministic engineering ZIPs |
| `1169887` | Production pipeline wired end to end; audit findings cleared |
| `01e289f` | Current `HEAD` |

Interleaved with these are partner-owned commits (`frontend/`, `desktop/`,
`scripts/product/`, `backend/app/agents/delivery/`, `backend/app/reporting/`).
Branches: `master` is the trunk and `origin/HEAD` points at it; there is also an
`agent/clean-readme` branch, checked out in a worktree.

---

## The canonical model, in short

104 tables and 103 mapped classes, of which **64 carry
`EngineeringObjectMixin`** and are therefore canonical engineering objects —
project-scoped, versioned, soft-deletable. Five Alembic revisions at `01e289f`,
head `38af6b21dc04`; six in the working tree, head `4f7a9c2d1e6b`. (Earlier
versions of this file said 74 engineering objects; the number was wrong, and
`backend/tests/test_project_core_schema.py::test_the_engineering_model_is_actually_present`
is the definition to trust.)

`ObjectType` has 46 members. Forty-five map to an ORM class in
`app/engineering/registry.py`; `finding` deliberately does not, because findings
are written by the agent runner rather than proposed through a change set.

**One door.** `app.engineering` is the only thing that writes to a canonical
object. Conflict detection, the protected-object rule, the traceability rule and
the human gate are guarantees only if there is no second path. A merge checks
scope, detects conflicts, validates, applies the human gate, then applies all of
it or none of it inside a savepoint. A dry run takes the same code path with the
savepoint rolled back and records a `MergeResult` of its own.

**Two columns carry the design.** `object_revision` is the optimistic-concurrency
token and the unit of conflict detection. `approval_state` makes an object
protected: once `approved` it cannot be edited in place, so an agent cannot
quietly change something an engineer signed off.

**Traceability is a table.** `object_evidence_links` cites the basis for an
object. Thirteen object kinds may not be created without one, and the rule is
enforced twice — in the action schema, where it is cheapest to catch, and again at
merge. A parser warning is not an acceptable basis.

**25 validators, 63 result codes** (62 discipline-namespaced plus
`validator.failed`). Only `ERROR` blocks a release. `UNSUPPORTED` is used freely,
because "we did not check this" has to stay distinguishable from "we checked and
it was fine".

**Six approval gates, 37 criteria.** A gate that is not ready names what is
missing. A decision is recorded against a revision and re-opens when a later
merge touches something the gate covers. `set_gate_enabled` has no HTTP surface.

---

## Available endpoints

Every endpoint below is served by the app in this tree. The three marked **new**
are the uncommitted ones above; everything else is at `01e289f`.

"Read" = any project role. "Write" = project `owner`/`engineer`. "Review" =
project `owner`/`reviewer`. Where a global role is also required it is named;
administrators are treated as project owners everywhere.

### System and authentication

| Method | Path | Access |
| --- | --- | --- |
| `GET` | `/api/health` | public — liveness, contract version, enabled providers, plugin capabilities |
| `GET` | `/api/system/capabilities` | public — plugin capabilities and why any are missing |
| `POST` | `/api/auth/login` | public — returns a token and sets an httpOnly cookie |
| `POST` | `/api/auth/logout` | authenticated — genuinely revokes, via the `jti` deny-list |
| `GET` | `/api/auth/me` | authenticated — user and per-project roles |
| `POST` | `/api/auth/users` | global `administrator` |
| `GET` | `/api/auth/users` | global `administrator` |

### Projects, membership, files

| Method | Path | Access |
| --- | --- | --- |
| `POST` | `/api/projects` | global `engineer` |
| `GET` | `/api/projects` | authenticated — only projects the caller may see |
| `GET` | `/api/projects/{project_id}` | read |
| `PATCH` | `/api/projects/{project_id}` | write — metadata, **not** status |
| `POST` | `/api/projects/{project_id}/archive` | write |
| `GET` | `/api/projects/{project_id}/members` | read |
| `POST` | `/api/projects/{project_id}/members` | write |
| `POST` | `/api/projects/{project_id}/files` | write |
| `GET` | `/api/projects/{project_id}/files` | read |
| `GET` | `/api/files/{file_id}` | read |
| `GET` | `/api/files/{file_id}/content` | read |
| `DELETE` | `/api/files/{file_id}` | write |
| `GET` | `/api/projects/{project_id}/parsers` | read |

### Engineering intake, requirements, clarifications

| Method | Path | Access |
| --- | --- | --- |
| `GET` | `/api/projects/{project_id}/engineering-profile` | read |
| `PUT` | `/api/projects/{project_id}/engineering-profile` | write |
| `GET` | `/api/projects/{project_id}/requirements` | read — with the exact source citation per requirement |
| `POST` | `/api/requirements/{requirement_id}/decision` | review **and** global `reviewer` |
| `GET` | `/api/projects/{project_id}/clarifications` | read — project questions and object ambiguities, with `blocking_count` |
| `POST` | `/api/project-questions/{question_id}/resolve` | write |
| `POST` | `/api/clarifications/{clarification_id}/resolve` | write |

Answering a question or resolving a clarification writes a named
`ProjectDecision` and returns `unblocked_task_count` — the run resumes on its own.

### Workflow

| Method | Path | Access |
| --- | --- | --- |
| `POST` | `/api/projects/{project_id}/runs` | write |
| `POST` | `/api/projects/{project_id}/pause` | write |
| `POST` | `/api/projects/{project_id}/resume` | write |
| `POST` | `/api/projects/{project_id}/cancel` | write |
| `POST` | `/api/projects/{project_id}/recover` | write — requeue tasks abandoned by a stopped worker |
| `GET` | `/api/projects/{project_id}/workflow` | read |
| `GET` | `/api/projects/{project_id}/tasks` | read |
| `GET` | `/api/tasks/{task_id}` | read — with dependency edges |

### Agents, findings, approvals

| Method | Path | Access |
| --- | --- | --- |
| `GET` | `/api/agents` | authenticated — every registered agent, and why any is unavailable |
| `GET` | `/api/projects/{project_id}/agents` | read — presence for the city view |
| `GET` | `/api/projects/{project_id}/agent-runs` | read |
| `GET` | `/api/agent-runs/{run_id}` | read — including the actions the backend **refused** |
| `GET` | `/api/projects/{project_id}/messages` | read |
| `GET` | `/api/projects/{project_id}/findings` | read — paginated, filterable |
| `GET` | `/api/findings/{finding_id}` | read — every citation resolved, with `display_location` |
| `PATCH` | `/api/findings/{finding_id}` | review **and** global `reviewer` |
| `POST` | `/api/findings/{finding_id}/comment` | read |
| `GET` | `/api/findings/{finding_id}/comments` | read |
| `GET` | `/api/projects/{project_id}/recommendations` | read |
| `GET` | `/api/projects/{project_id}/approvals` | read — `?status=` filters |
| `GET` | `/api/approvals/{approval_id}` | read — with the evidence snapshot taken when it was raised |
| `POST` | `/api/approvals/{approval_id}/decision` | review **and** global `reviewer` |
| `POST` | `/api/approval-gates/{gate_id}/approvals/{approval_id}/decision` | review **and** global `reviewer` — **new** |

### Validation

| Method | Path | Access |
| --- | --- | --- |
| `GET` | `/api/projects/{project_id}/validation-results` | read — **new** |
| `POST` | `/api/validation-results/{result_id}/waiver` | review **and** global `reviewer` — **new** |

### Release

| Method | Path | Access |
| --- | --- | --- |
| `GET` | `/api/projects/{project_id}/release-readiness` | read |
| `POST` | `/api/projects/{project_id}/release-candidates` | write **and** global `engineer` |
| `GET` | `/api/projects/{project_id}/release-candidates` | read — paginated |
| `GET` | `/api/release-candidates/{candidate_id}` | read — with an `actions` block saying which of generate/finalize/download is available and why not |
| `POST` | `/api/release-candidates/{candidate_id}/artifacts` | write **and** global `engineer` — generate or regenerate |
| `GET` | `/api/release-candidates/{candidate_id}/artifacts` | read — one row per file, with its level and checksum |
| `POST` | `/api/release-candidates/{candidate_id}/finalize` | review **and** global `reviewer`; body must carry `confirm: true` |
| `GET` | `/api/release-candidates/{candidate_id}/download` | read — streams the pre-verified ZIP, `X-Content-Sha256` on the response |

### Reports and model configuration

| Method | Path | Access |
| --- | --- | --- |
| `GET` | `/api/projects/{project_id}/reports` | read |
| `GET` | `/api/reports/{report_id}` | read |
| `GET` | `/api/reports/{report_id}/content` | read |
| `GET` | `/api/model-configurations` | global `administrator` |
| `GET` | `/api/model-configurations/providers` | global `administrator` — which providers are enabled, and which send content off the machine |
| `PUT` | `/api/model-configurations/{agent_kind}` | global `administrator` |
| `DELETE` | `/api/model-configurations/{agent_kind}` | global `administrator` |

### Events

| Method | Path | Access |
| --- | --- | --- |
| `GET` | `/api/projects/{project_id}/events` | read — `?since_seq=`, repeatable `?event_type=`, `?limit=`, `?offset=` |
| `WS` | `/ws/projects/{project_id}` | read — token via `?token=` or the session cookie |

**No endpoint lists the approval gates.** A gate reaches the UI through the
approval it raised: `ApprovalRead` carries `gate_id`, `gate_key`,
`revision_number`, `decision`, `decision_allowed` and
`decision_disabled_reason`. `GET /api/projects/{id}/release-readiness` reports
`outstanding_gates` in gate order. Neither returns a gate's unmet criteria, so the
sentence explaining *why* a gate is not ready is currently only visible on the
`ApprovalGate` row and in the `gate.status_changed` event payload. Tell me if you
want a gates endpoint; it is a small addition and I would rather add it than have
you infer gate state from approvals.

**No endpoint reports the workstation adapter.** `app.adapters.capability_rows()`
exists and produces a row in the same shape as the plugin capabilities, but
`GET /api/system/capabilities` does not call it, so nothing there says import
verification is unavailable. Read `import_verification_possible` and
`import_verification_detail` from `release-readiness` instead. Also mine to fix.

---

## API semantics worth knowing

**Auth transport.** Send `Authorization: Bearer <token>` *or* rely on the
`esco_session` httpOnly cookie login sets. Both work; the browser frontend should
use the cookie, the desktop shell the header. The WebSocket takes `?token=` too,
because a browser cannot set headers on a handshake.

**Error envelope.** Every failing endpoint returns exactly one shape:

```json
{ "error": { "code": "release_refused", "message": "…", "details": {} } }
```

`code` is stable and safe to branch on. `X-Request-ID` correlates a failure with
the server log. `release_refused` carries `details.blocks`, an array of
`{code, summary, detail}` — the detail is written to be shown to an engineer
unedited.

**Upload is partially accepting by design.** `POST .../files` returns `201` even
when some files were refused. Read `results[].accepted` per file and show
`rejection_reason` verbatim; do not treat a non-zero `rejected_count` as a failed
request. An uploaded ZIP is expanded and each usable member appears in
`extracted` as a file in its own right, with `parent_file_id` and
`archive_member_path`.

**Deleted files keep their row.** `GET /api/files/{id}` still returns `200` with
`status: "deleted"`; only `/content` becomes `404`. Render it as a tombstone so a
citation that pointed at it does not silently vanish.

**The approval gate.** `POST /api/approvals/{id}/decision` takes a boolean and is
the compatibility route; when the approval is a gate decision it now routes
through the gate service so the gate and the approval move together. Use
`POST /api/approval-gates/{gate}/approvals/{approval}/decision` for anything other
than approve/reject — both identifiers are in the path so a stale client cannot
apply a decision to a newer request for the same gate. `approved` has no default.
Once nothing is pending, the gated tasks return to the queue and the run
continues on its own.

**Four things the finding UI must get right**, because they are product promises
rather than presentation choices:

1. Never show `fact` and `inference` as one block. They are separate fields
   precisely so a deduction is never displayed as an observation.
2. Always show `limitations` and `confidence`. Hiding uncertainty is the failure
   this product exists to prevent.
3. `display_location` **is** the citation. It is written to be followable
   verbatim — "MainProgram / Conveyor_Interlocks / Rung 0" — so show it, do not
   summarise it.
4. A finding with `requires_approval: true` is not a result yet.

**Two things the release UI must get right:**

1. A package is an **"import-ready candidate"** until an actual Studio 5000
   import has passed, and none ever has in this repository. Show
   `validation_level`, `import_verification_status` and
   `import_verification_detail` together; the detail is a whole sentence written
   for an operator.
2. `highest_attainable_validation_level` from `release-readiness` is the
   **ceiling this deployment can reach**, not what any package reached. Showing it
   before generation is what stops "verified" being expected later.

---

## WebSocket

Envelope: `id`, `schema_version`, `seq`, `event_type`, `project_id`, `agent_id`,
`task_id`, `actor_user_id`, `occurred_at`, `payload`.

**47 event types**, 46 of them mapped to a payload model in
`contracts/events.schema.json` under `x-payload-by-event-type` (see
[Contracts](#contracts)). Events are **persisted before broadcast**, with
a gap-free per-project `seq`. `GET /api/projects/{id}/events?since_seq=N` and
`WS /ws/projects/{id}?since_seq=N` are interchangeable for catching up; the socket
answers with a `hello` frame carrying `current_seq`, how many events were
replayed, and `replay_truncated` — when that is true the client **must** refetch
over REST rather than assume it has a complete picture.

The transport is live and the emitters are wired; the note in the previous
version of this file saying otherwise was left over from Milestone 0.

Two defects worth keeping in mind if you touch the broadcast path, both silent
and both found by attacking the merge rather than by any test:

* **A savepoint rollback emptied the whole event broadcast buffer.** SQLAlchemy
  fires `after_soft_rollback` for a `SAVEPOINT` as well as for the outer
  transaction, and the merge applies inside `begin_nested()`. A dry run cancelled
  the live delivery of every event the surrounding request had already recorded:
  rows committed, REST history complete, WebSocket quiet.
* **`after_commit` fires when a savepoint commits.** Events were published before
  the outer transaction landed, so a rollback afterwards left a client holding a
  change that never happened — which is exactly what persist-then-broadcast
  ordering exists to prevent.

---

## Defects worth remembering

Each milestone was implemented and then independently attacked. The full accounts
are in `docs/backend/parsers.md`, `docs/backend/evidence.md`, the earlier revisions
of this file, and the regression test that came with each fix. The pattern worth
carrying forward is that **almost every one was silent**: content dropped with no
warning, or a claim the code could not support. A green suite found none of them.

**Schema and persistence**

* **Enum columns did not round-trip.** `Mapped[ProjectStatus]` on a plain
  `String(32)` stores correctly and loads back as a `str`, so
  `status is ProjectStatus.ARCHIVED` was `False` for every persisted row — which
  made archived projects still accept uploads and made `DELETE /api/files/{id}`
  return 500 *after* unlinking the blob. Fixed with a `StrEnumType` in
  `Base.type_annotation_map`; `backend/tests/test_project_core_schema.py` now fails
  for any enum column that forgets it. This is the single most repeatable mistake
  in the repository.
* **The seeded administrator could never sign in.** `admin@esco.local` — and every
  `.local`/`.test`/`.invalid` address — is rejected by `EmailStr`. Seeded accounts
  moved to `esco.internal`, and `Settings` refuses to start with a bootstrap email
  the login endpoint would reject.
* **Failed logins were rolled back.** The audit entry was written and the route
  only committed on success, discarding the entire record of refused sign-ins.

**Parsers and the evidence graph**

* An L5X alias tag's target was discarded, so the binding between an interlock
  symbol and its physical I/O point vanished without trace.
* Only the first `<Tags>` container was read, so a declared tag was reported as
  undeclared — a false finding, not a missed one.
* Tracked changes inside DOCX table cells dropped text, and the mutilated string
  was published as a *direct* anchor excerpt an agent would quote verbatim.
* Pages pypdf silently dropped were never noticed, so a partially-read document
  reported as fully examined.
* A spreadsheet cell whose text merely began with `=` was recorded as a formula
  with a null value, turning a documented threshold into a blank field.
* Bytes were classified by extension, so an archive named `.csv` was routed to a
  text parser and its contents omitted from the review.
* The graph fabricated anchor keys no parser emits (`range:Interlocks!row5` against
  the parser's `range:Interlocks!A5:H5`). Nothing errored — the Interlock Review
  Agent simply had nothing to cite and produced no discrepancies at all. There is
  now a test asserting that every citation the graph emits resolves to a real
  anchor, for every file type.
* Members of a declared UDT (`CV101_Status.Running`) read as undeclared
  references, turning one genuine finding into seven false leads; and rows from a
  *hidden* worksheet were treated as live requirements.

**Workflow**

* **A failed dependency stalled a run forever.** The queue ignored
  `TaskDependency.hard`, so one unreadable document left the graph task — and
  everything behind it — queued permanently while the project still reported itself
  `running`.
* **Blocked tasks hid runnable ones.** The claim window was sized from `limit`, so
  a run of dependency-blocked tasks starved the work behind them.
* **The duplicate-enqueue recovery called `db.rollback()`**, discarding the
  caller's whole transaction — inside `start_run` that is every task, dependency,
  audit row and event created so far.
* **Handlers were only registered when an in-process worker was enabled**, so an
  API process paired with an external worker reported every task kind as unhandled.
* **The WebSocket used the process-global settings**, rejecting tokens the REST API
  had just issued whenever the app was built with explicit settings.

The two savepoint/broadcast defects are above, under WebSocket.

---

## Plugin interfaces — now populated

`backend/app/agents/delivery/` and `backend/app/reporting/` are partner-owned and
**exist**. `GET /api/system/capabilities` currently reports all three
capabilities available:

* five delivery agents: `alarm_engineering`, `electrical_io`,
  `engineering_report`, `hmi_engineering`, `testing_commissioning`;
* twenty-one report providers, including `discrepancy_matrix`,
  `requirements_traceability_matrix`, `interlock_matrix`, `fat_plan`, `iat_plan`
  and `known_limitations`;
* seven export formats: `csv`, `docx`, `html`, `json`, `markdown`, `pdf`, `xlsx`.

Register either by defining `register(registry)` in the package `__init__`, or by
declaring module-level `DELIVERY_AGENTS` / `REPORT_PROVIDERS` / `EXPORT_FORMATS`
sequences. `DeliveryAgentPlugin` needs `kind`, `display_name`, `version`,
`handles_task_kinds`, `run(context) -> AgentRunResult`. `ReportProvider` needs
`report_type`, `display_name`, `version`, `supported_formats`,
`render(snapshot, export_format) -> RenderedReport`. `ExportFormatProvider` needs
`format_key`, `media_type`, `file_extension`, `version`,
`serialize(document) -> bytes`. A plugin that raises during import or registration
is caught, recorded with its traceback and surfaced through
`/api/system/capabilities`; it never prevents startup.

Two rules that still hold:

* **Delivery agents run after human approval**, so they should read only findings
  whose `status` is `accepted`.
* **`Task.checkpoint` is the tool for a long generation.** Use
  `context.mark_done(item)` / `context.done()`; the queue is at-least-once and a
  crashed task is otherwise re-run from the start. Durability is proved by
  `backend/tests/orchestration/test_checkpoints.py`.

One thing that does not work, and it is mine: **plugin output never reaches the
release package.** `app.engineering.release._delivery_slots` constructs every
slot with `is_present=False` and no files, so `02_HMI`, `03_Alarms`,
`04_Testing` and `05_Reports` are always created empty with a stated absence
reason, even when a delivery agent produced something. If you have artifacts you
want inside the customer ZIP, say so and I will wire the slots to the plugin
results.

---

## What is not done

Honestly, and in rough order of how much it matters.

1. **No production run has completed end to end.** Gate 2's
   `equipment_model_recorded` needs at least one `Device`, and on the Line 4
   fixture nothing creates one — the I/O list's "Device Tag" column is claimed by
   the canonical importer's `tag` alias, so no `Device`, `Signal` or
   `AddressAssignment` is created. Gate 3's `hmi_screens_recorded` needs
   `HMIScreen` rows, which only the HMI delivery agent creates. Every gate after
   the first undecidable one reports "an earlier gate is undecided", so
   generation, freezing and release are unobserved on that fixture.
   `test_production_workflow.py` reaches further only because its fixture
   declares `HmiProduct.NONE`.
2. **Only one of the seven planted Line 4 defects comes out right.** Four are not
   found (issues 1, 2, 4 and 5), one is found but inflated into a release blocker
   above the missing protection it should sit below (issue 3), one deliberate
   ambiguity is never put back to the customer (issue 6), and the correctly
   implemented diverter is reported as a defect (issue 7). Six of the eleven
   decoys also fire. The per-issue diagnosis is on each `xfail` marker and the
   scoreboard is tabulated in `docs/backend/release-pipeline.md`. Do not quote a
   detection rate from anywhere else.
3. **No Studio 5000 import has ever been attempted.** Offline verification stops
   at `ROUND_TRIP_PARSED`. This is by design and is not going to change without a
   licensed machine, but it must never be described as "verified".
4. **The contracts are stale and `test_contracts.py` fails** on them (above). The
   four-column migration gap is closed in the tree by an uncommitted revision, so
   a clean checkout of `01e289f` still fails `alembic check`.
5. **60-odd enums are missing from the domain contract** (above).
6. **No gates endpoint, no gate enable/disable endpoint, no adapter capability
   row** (above).
7. **Plugin output does not reach the package** (above).
8. **Four `ValidationLevel` members are declared and never assigned** —
   `schema_validated`, `internal_semantic_validated`, `human_approved`,
   `released`. Do not branch on them. See ADR 0002 decision 2.
9. **PostgreSQL is untested.** Portable column types, full support intended, only
   SQLite exercised. `e342ddb` fixed one real PostgreSQL defect (a password with
   URL-significant characters made Alembic unusable), which is a fair indication
   there are others.
10. **Read-only sealing is best-effort.** Originals and published archives are
    chmod'ed read-only; network and container filesystems may ignore it, and a
    warning is logged when the chmod fails.
11. **Per-project byte quota counts only live uploads.** It sums
    `UploadedFile.size_bytes` for undeleted rows, so parser output under
    `derived/`, reports, and every generated package and delivery ZIP under
    `releases/` are outside it. `ProjectStorage.total_bytes()` would measure the
    real occupancy and nothing calls it, so release packages grow unbounded.
12. **Demo passwords are in the repository.** `esco-demo-admin` and the seeded
    demo accounts exist so the offline demonstration works out of the box.
    `ESCO_ENVIRONMENT=production` refuses to start until `ESCO_JWT_SECRET` and
    `ESCO_BOOTSTRAP_ADMIN_PASSWORD` are set, and
    `ESCO_BOOTSTRAP_DEMO_USERS=false` turns the demo accounts off.

---

## What stays impossible

Enforced in code, and listed so that a later change weakening one is visibly a
reversal rather than a refactor.

* No controller connection, upload, download, online edit, mode change, force or
  firmware operation exists anywhere — absent, not disabled.
  `backend/tests/security/test_product_boundaries.py` fails if an industrial
  protocol library is ever imported.
* No `.ACD` parsing or fabrication. Uploads are refused at intake with a message
  naming L5X as the supported input.
* Models get no filesystem, shell, database or network access. They return
  structured proposals into a closed action set and the runner refuses anything
  outside it. Refusals are recorded on the run — what a model *tried* to do is
  part of the record.
* External providers cannot be constructed unless an administrator enables them
  and supplies a credential. The default is `mock`, which is a deterministic rule
  engine, is not a model, says so on every response, and has its confidence
  capped. There is no fallback from a failing provider to another one.
* Safety-related and equipment-damage recommendations cannot be released without
  a named human approval. Restricted domains are review-only unless an
  administrator names them in `ESCO_ENABLED_RESTRICTED_DOMAINS`; an unrecognised
  name is refused at startup rather than silently ignored.
* Model chain-of-thought is never stored or displayed. A short, user-facing
  rationale is.
* `ROCKWELL_IMPORT_VERIFIED` is unreachable without the licensed workstation
  adapter, and the ceiling is enforced structurally:
  `VerificationOutcome` refuses to exist with `status=PASSED` unless it came from
  an adapter reporting `READY`.

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

Interactive docs: <http://127.0.0.1:8000/docs> · Health:
<http://127.0.0.1:8000/api/health>

Defaults with no configuration: SQLite at `data/esco_city.sqlite3`, MockProvider,
no network access, no external service. The schema is migrated and an
administrator is seeded on first boot. Sign in with `admin@esco.internal` /
`esco-demo-admin`; in demo mode `engineer@esco.internal` /
`esco-demo-engineer` and `reviewer@esco.internal` / `esco-demo-reviewer` are
seeded too, so each role can be exercised. Note the domain: `.local`, `.test`
and `.invalid` are reserved special-use domains and `EmailStr` rejects them, so
do not use them in frontend fixtures either.

### Checks

```bash
.venv/Scripts/python -m ruff format backend scripts
.venv/Scripts/python -m ruff check backend scripts
.venv/Scripts/python -m mypy
.venv/Scripts/python -m pytest
.venv/Scripts/python scripts/backend/generate_contracts.py --check
.venv/Scripts/python scripts/backend/smoke.py             # review, live process
.venv/Scripts/python scripts/backend/smoke_production.py  # Line 4 acceptance
```

`ruff check` and `mypy` pass. `ruff format` is **not** currently a no-op —
fifteen files would be reformatted, spread across both owners' code — so run it
before committing rather than assuming it is clean. `generate_contracts.py
--check` reports all three files stale while the uncommitted API work is in the
tree, which is what fails `backend/tests/test_contracts.py`.

Both smoke scripts boot a real uvicorn on a throwaway database and data
directory, and both were run against this tree.

`smoke.py` — **72 checks passed, 0 failed.** Signs in, creates a project, uploads
the nine synthetic review documents, checks that a disguised executable and an
`.ACD` are refused with reasons, runs the review, reads the findings and their
citations, approves the one needing a human, confirms the project only reports
itself complete afterwards, and archives it.

`smoke_production.py` — **67 checks passed, 0 failed, 17 gaps**, and a real stop
and reboot against the same database in the middle. It drives the Line 4 fixture
and reports `check` / `found` / `quiet` / `gap`; only `check` can fail the run,
because a smoke script that went red on a known limitation would be silenced
within a week and the limitation would stop being visible. Read the 17 gaps: they
are the most compact honest statement of what the product does not yet do. The
project ends `blocked` and no release candidate is generated.

### Migrations

Point Alembic at a throwaway database rather than the project's own, or you will
migrate live data:

```bash
ESCO_DATABASE_URL="sqlite+pysqlite:///./tmp/scratch.sqlite3" \
  .venv/Scripts/python -m alembic upgrade head
ESCO_DATABASE_URL=... .venv/Scripts/python -m alembic check
ESCO_DATABASE_URL=... .venv/Scripts/python -m alembic downgrade base
ESCO_DATABASE_URL=... .venv/Scripts/python -m alembic revision --autogenerate -m "describe it"
```

Head is `4f7a9c2d1e6b` in the working tree and `38af6b21dc04` at `01e289f`.
`upgrade head` → `check` → `downgrade base` → `upgrade head` round-trips cleanly on
SQLite with the newer revision present. Check what head actually is before
autogenerating, or you will fold somebody else's columns into your revision.

### Fixtures

```bash
.venv/Scripts/python scripts/backend/make_fixtures.py      # the review fixture
.venv/Scripts/python scripts/backend/make_line4_fixture.py # the acceptance fixture
```

`fixtures/demo_project/` is a full synthetic review project and
`fixtures/README.md` states exactly which discrepancies a correct review should
and should not report. `fixtures/line4_packaging_conveyor/` is the production
acceptance fixture: eight artifacts, seven planted defects and eleven decoys, all
declared in advance. Both scripts take `--target`, and `make_fixtures.py` raises
from its own summary when the target is outside the repository root — the files are
written first, so it is cosmetic, and it is mine. Please add only synthetic data;
no ESCO customer content in the repository.

---

## Contracts

`contracts/` is **generated**, not hand-written:

```bash
python scripts/backend/generate_contracts.py           # write
python scripts/backend/generate_contracts.py --check   # fail if stale
```

`backend/tests/test_contracts.py` runs the check, so an API change not reflected
in the contracts fails the suite. Generate the frontend client from these files —
they cannot describe an endpoint the backend does not serve.

| File | Contents |
| --- | --- |
| `contracts/openapi.yaml` | Every HTTP endpoint — 54 paths, 65 operations at `01e289f` |
| `contracts/events.schema.json` | The envelope plus a payload model per event type — 46 mapped |
| `contracts/domain.schema.json` | 90 definitions, of which 32 are enums; `x-enums` lists the 25 deliberately published ones |
| `contracts/README.md` | How to consume them, and what this API will never do |

The committed `x-payload-by-event-type` maps 46 of the 47 `EventType` members:
`changeset.abandoned` is emitted by `app.engineering.changesets.abandon` and had
no payload model at `01e289f`. One has since been added in the working tree, which
is part of why the contracts read stale. The enum shortfall in
`domain.schema.json` is a separate and larger gap, and is mine.

---

## Requests for GPT

1. **Do not hand-edit `contracts/`.** If the frontend needs a field or an enum
   that is not there, note it in `docs/handoffs/GPT.md` and I will add it to the
   source model or to `PUBLISHED_ENUMS` and regenerate.
2. **Say which enums you need published first.** The gate, release and validation
   enums are the obvious candidates; I would rather add the right ten now than all
   sixty badly.
3. **Tell me whether you want a gates endpoint** returning each gate's status and
   its unmet criteria. Inferring gate state from approvals works today but will
   not survive the first gate that is `NOT_READY` with nothing pending.
4. **Handle unknown enum members and unknown event types without breaking.** The
   widening above will happen again; members are added, never renamed, and that is
   a contract promise you can build a `default` branch against.
5. **If you want delivery artifacts inside the customer ZIP**, say so — the slots
   exist and are wired to nothing.
6. **Health endpoint for the desktop shell.** `GET /api/health` returns
   `contract_version`, now `1.1.0`. Consider failing fast in the client if it does
   not match the version the generated client was built from.
7. **Whoever is editing `backend/app/api/routes/` and
   `backend/app/engineering/gates.py` in the working tree**: those are mine, and
   the gate-decision endpoint is a genuine improvement, so this is a coordination
   note rather than an objection. Three things belong in the commit that lands it,
   and I will do them if you would rather not: regenerate `contracts/` (three
   files are stale, which is what fails `test_contracts.py`); delete the two
   `RESOLVED` gaps from `scripts/backend/smoke_production.py` along with the
   `GATE_WALL` wording; and fix
   `test_line4_end_to_end.py::test_a_gate_decision_has_an_http_door`, which is
   `strict=True` and only still xfails because it enumerates `application.routes`
   in a way that does not see prefixed routers — a test that would pass for the
   wrong reason is worse than one that fails.
8. **`test_workflow_api.py::test_a_run_started_over_rest_is_streamed_to_a_connected_socket`
   fails.** It expects `workflow.started` followed by a contiguous run of
   `task.created` frames, and an `agent.status_changed` now lands in the middle.
   Either the agent-provisioning events moved or the assertion was always too
   strict about interleaving; I have not decided which, and it is mine to settle.
