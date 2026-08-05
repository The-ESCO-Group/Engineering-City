# The Release Pipeline

From a merged revision to a package a customer can hold, and the six places a
human has to say yes.

This document is for someone about to run this for real. It assumes
[the project core](project-core.md) — revisions, change sets, merge — and picks
up where a merge leaves off. For why verification is a level rather than a
boolean, read [ADR 0001](adr/0001-extend-the-review-platform-into-a-production-platform.md)
decision 4; for what a level's *ceiling* means and why a gate re-opens, read
[ADR 0002](adr/0002-what-a-release-record-is-allowed-to-claim.md).

The section that matters most is the last one.

---

## The shape in one paragraph

A production run is a chain of tasks with an **approval gate between each stage
and the next**. A gate is a task rather than a check inside the following stage,
because a gate is where a run *stops*: the work behind it depends on it, so an
undecided gate leaves that work queued and visible instead of letting the run
walk into engineering nobody authorised. Deterministic code — never a model —
reads the approved revision and emits the files. The package is assembled in
memory, written under `releases/`, hashed, frozen against a named person, and
only then sealed into the ZIP that is downloaded.

---

## Which shape of run a project gets

`app.orchestration.engine.choose_workflow` asks the project profile one
question — `blocks_generation` — and returns the answer with the reason that
applied. A project with no profile, an incomplete profile, a `review_only`
release mode, or a restricted domain without a qualified workflow gets the
**review** run: parsers, evidence graph, findings, approvals, reports. It cannot
generate, and that is a scoping decision rather than a failure.

Everything below describes the **production** run.

---

## The stages a revision passes through

In order, as `_plan_production` enqueues them. `priority` is the queue's
ordering; the dependency edges are what actually hold a stage back.

| # | Task kind | Who runs it | What it does |
| --- | --- | --- | --- |
| 1 | `inventory_files` | deterministic | Records what was supplied, and names what was expected and is absent. |
| 2 | `parse_file` (one per file) | deterministic | Seven parsers; evidence anchors. |
| 3 | `build_evidence_graph` | deterministic | Joins every parsed artifact into one cross-file structure. |
| 4 | `extract_requirements` | Requirements Agent | Turns the brief into a traceable requirements model, citing where each item was read. |
| 5 | `plan_project` | Project Manager Agent | Decides which specialist reviews the supplied evidence supports. |
| 6 | `resolve_clarifications` | deterministic | **Holds the run** until every mandatory question and generation-blocking clarification has an answer. |
| 7 | `senior_review` (initial) | Senior Review Agent | Independently checks every finding *before* it reaches a human. |
| — | **Gate 1 — project profile** | a person | |
| 8 | `design_tags` | Tag Architecture Agent | Proposes the canonical tag vocabulary every other discipline binds to. |
| 9 | `validate_cross_discipline` | deterministic | Every validator over the revision as it stands. |
| — | **Gate 2 — devices, I/O and tags** | a person | |
| 10 | `design_plc_architecture` | PLC Logic Agent | Proposes the tasks, programs and routines the logic will live in. |
| 11 | `generate_logic` | PLC Logic Agent | Selects an approved template per network and supplies its parameters; deterministic code expands them into rungs. |
| 12 | `delivery` | partner-owned plugins | HMI, alarm, testing and report deliverables, if any are installed. |
| 13 | `validate_cross_discipline` | deterministic | |
| — | **Gate 3 — logic and delivery design** | a person | |
| 14 | `validate_cross_discipline` | deterministic | The full run the QA gate reads. |
| 15 | `senior_review` (final) | Senior Review Agent | Only findings created or changed since the first pass. |
| — | **Gate 4 — QA and risk** | a person | |
| 16 | `request_approvals` | Senior Review Agent | Raises an approval for everything needing an engineer's decision. |
| 17 | `generate_release_candidate` | deterministic | Reserves the candidate, builds the package, writes it, records one artifact row per file. |
| 18 | `verify_import` | deterministic | Asks the workstation adapter, and records that it could not when there is no licensed workstation. |
| — | **Gate 5 — import verification** | a person, or skipped with a reason | |
| 19 | `assemble_package` | deterministic | Freezes the manifest against a named approver. |
| — | **Gate 6 — release** | a person | |
| 20 | `finalize_project` | deterministic | Seals the delivery ZIP and completes the project. |

**Three validation runs, not one.** Gate 2's criteria include "the newest
validation run examined *this* revision and covered tags and I/O", and gate 3
says the same of logic and delivery. A single run after senior review would
leave both gates permanently not-ready, complaining about validation currency in
a way no amount of design work could clear. Every run covers every discipline
regardless of which stage asked for it: a run that examined three disciplines
must never be mistaken for one that examined all of them.

**Senior review runs before the first gate**, which is the one place the chain
departs from a straight reading of the pipeline order. The specialist reviews
raise their approvals the moment they write a finding, so senior review has to
precede gate 1 or a reviewer is asked to decide findings nobody has
independently checked.

### Two outcomes that are not failures

`blocked` means a human has to supply or decide something before the step can
pass — an unanswered question, an unwaived validation error, a gate whose
criteria are unmet. `awaiting_approval` means the platform has raised the
decision and is holding for it. Neither is a failure and **neither may be
reported as completion**; the project's status is the phase it stopped in.

---

## What each of the six gates actually asks

`app.engineering.gates`. Six gates, 37 criteria. A criterion is a small
predicate over the canonical model that returns the empty string when it holds
and, when it does not, the sentence explaining what is missing — one callable
rather than a predicate plus a message builder, because the code that discovers
*which* three devices are unwired is the only code in a position to say so.

A criterion that raises is recorded as **unmet**, naming the exception. A gate
that reported "ready" because the check which would have blocked it crashed is
worse than one that reports its own failure, because the first looks like a pass.

### Gate 1 — project profile and architecture (9 criteria)

Everything downstream is generated under the standards approved here, so an
unanswered question at this gate becomes a guess in every tag name after it.

* A profile exists, and every required field is recorded and *usable*.
* The target platform states its controller family, catalogue number, firmware
  revision and Studio 5000 version.
* The HMI product and version are recorded, **or** the project states no HMI is
  in scope. `HmiProduct.NONE` is a settled answer, not a gap — a project with no
  HMI is a real project, and demanding a product name for one would be demanding
  a fabricated decision.
* The pinned template set declares support for the target controller family.
* The project is cleared to generate, or is explicitly scoped to review only.
* Every extracted requirement has been accepted or rejected by a human, and a
  generating project has at least one accepted one.
* Every mandatory project question has been answered.

### Gate 2 — devices, I/O and tags (6 criteria)

Approving this fixes the vocabulary every rung, alarm, display and test step
will reference. Renaming an approved tag afterwards is a separate,
impact-analysed act.

* At least one device, and at least one tag.
* Every device has signals or is declared network-only. A device that exchanges
  everything over the network must say so; otherwise this is unfinished I/O
  design.
* Every tag typed as a UDT or Add-On Instruction names a definition this project
  holds. A tag of type `ESCO_Motor` where nothing defines `ESCO_Motor` is a
  package that fails at the customer's import dialog rather than here.
* A validation run covering tags and I/O completed **at the current revision**,
  and no unwaived tag or I/O error is outstanding.

### Gate 3 — logic and delivery design (7 criteria)

Where an engineer confirms that what the platform generated is what the
requirements asked for.

* At least one program with at least one routine, and every *ladder* routine
  holds at least one rung. Only ladder is checked: a structured-text routine
  legitimately has no rungs.
* HMI screens exist, or the platform states no HMI is in scope.
* An alarm model exists, or the project defines no faults to annunciate.
* Every non-spare, non-virtual I/O point names the hardware module it lives on.
  A point with an address but no module is an address read out of a spreadsheet
  that nobody tied to a physical card; it exports as a tag resolving to nothing
  and the failure surfaces on the plant floor.
* A validation run covering logic and the cross-discipline checks completed at
  the current revision, with no unwaived errors.

### Gate 4 — QA and risk (6 criteria)

Nothing here is closed by a validator. This gate exists because a named engineer
has to carry what remains.

* A validation run covering **every** discipline completed at the current
  revision, with no unwaived error-severity result.
* Every critical and high finding has completed independent senior review *and*
  has a recorded human disposition. Those are two criteria, not one: reviewed and
  decided are different facts.
* Every safety-related or equipment-damage recommendation has been approved or
  rejected by a named engineer.
* Every validation result flagged as needing a human decision has been waived
  with a stated reason.

### Gate 5 — import verification (4 criteria)

* A release candidate has been generated, and every artifact in it carries a
  checksum.
* A licensed Rockwell workstation imported the package and reported no errors.
* The package was generated for the controller catalogue number, firmware
  revision and Studio 5000 version **currently** recorded. A candidate built
  against firmware 33 and approved after somebody moved the project to firmware
  35 imports into the wrong controller, and nothing else in the chain compares
  the two.

**This gate is unreachable in almost every deployment, and that is normal.** It
resolves to `SKIPPED` with the reason attached — never `PASSED` — when the
project is not scoped to `rockwell_workstation_assembly`, or when no workstation
verification job has ever run. `record_decision` refuses an approval that has no
verification evidence behind it, so the gate cannot be talked past.

### Gate 6 — release (5 criteria)

* The candidate was generated from the project's **current** revision.
* Its manifest is frozen and carries both a content hash and a checksum-file
  hash.
* Every settled gate decision on this project names the human who made it.
* Every disabled gate states why it was not applied.
* No unwaived error-severity result is outstanding.

A decision on gate 6 always requires a comment, whatever the verdict: the
covering sentence is part of the record handed to the customer.

### The rules that apply to all six

**Ordering.** A gate cannot be decided while an earlier enabled gate is
undecided. `SKIPPED` counts as settled; `FAILED` does not — a gate a reviewer
actively refused is the strongest block there is.

**A decision is recorded against a revision.** `decided_revision_number` is what
stops a later merge inheriting an approval nobody gave. The gate re-opens when
the project has moved past the decision **and** the operations merged in between
touched an object type the gate covers. Both halves are required; see ADR 0002
decision 1.

**A gate that was not applied is recorded, never skipped quietly.** An
administrator may disable a gate, but only with a stated reason, and that reason
travels into the release record. There is currently **no HTTP endpoint** for
enabling or disabling a gate — `set_gate_enabled` is service-level only.

**`ACCEPT_RISK` is not `APPROVE`.** Both leave the gate `PASSED`, because both
let the work proceed, but the approval row keeps them apart
(`ApprovalStatus.RISK_ACCEPTED`) all the way into the package's approval log. A
release that proceeded on knowingly carried risk must never be
indistinguishable afterwards from one with no risk at all. `REQUEST_CHANGES` and
`DEFER` settle nothing: the work goes back to the engineers and the gate returns
to the queue.

**Models propose; humans decide.** An agent may call `request_decision`, which
raises a pending `Approval` and moves the gate to `AWAITING_DECISION`. Only
`record_decision` settles one, and it demands an authenticated user holding the
reviewer role on the project. There is no path through the module by which an
agent settles a gate.

---

## What a `ValidationLevel` means

Seven ordered members. The whole point of the scale is that an artifact is never
"valid" — it has reached a level, and the level it claims is the level it
reached.

| Level | What it means | What sets it |
| --- | --- | --- |
| `generated` | This platform wrote the file. Nothing has been checked. | The default for every file, and the final answer for every CSV, Markdown and JSON document in the package. |
| `schema_validated` | — | **Nothing.** No code path assigns it. |
| `internal_semantic_validated` | — | **Nothing.** No code path assigns it. |
| `round_trip_parsed` | This platform's own L5X parser read the generated file back and the rungs came back as the rungs we wrote. | `build_release_package`, on an L5X only, and only when `PackageOptions.settings` was supplied. |
| `rockwell_import_verified` | A licensed Rockwell workstation imported the package and reported no errors. | Only `VerificationOutcome.attained_validation_level`, and only from an adapter reporting `READY`. Unreachable with the adapter off. |
| `human_approved` | — | **Nothing.** No code path assigns it. |
| `released` | — | **Nothing.** No code path assigns it. |

Three things follow, and each is a claim this documentation is making on the
code's behalf:

**`ROUND_TRIP_PARSED` does not mean Studio 5000 will import the file.** It means
our generator's output parses back into the same model. That is worth real
money — it catches escaping bugs, dropped operands, mis-ordered rungs, and
anything the emitter got wrong that our own reader can see — and it is *not*
evidence about Rockwell's importer. Our parser and our generator were written by
the same people from the same understanding of the format; agreement between
them proves they are consistent, not that they are right. No file in a package
produced by this backend has ever been opened by Studio 5000. Say that to a
customer in those words rather than saying "verified".

**The package's level is its weakest file's, not its strongest.** One CSV that
only reached `generated` means the package reached `generated`, whatever the L5X
files managed. The manifest states both ends —
`verification.highest_level_attained` and `verification.package_level` — and
rolling the package up to the highest would promote a file nobody checked to the
standing of one that was.

**`SCHEMA_VALIDATED`, `INTERNAL_SEMANTIC_VALIDATED`, `HUMAN_APPROVED` and
`RELEASED` are declared and never assigned.** They are in the enum because the
scale is published contract and members are added, never renamed — but no code
sets them, so a client that branches on them is branching on something it will
not see. Human approval and release *are* recorded; they live on
`ReleaseCandidate.status`, `approval_state`, `approved_by_id` and the frozen
`ReleaseManifest`, not on the validation level. Do not read a candidate's
`validation_level` as a statement about whether anyone signed it.

### Import verification status is not a validation level

`ImportVerificationStatus` is a separate column, and its default is deliberately
not neutral. With no adapter configured the status is `UNAVAILABLE` carrying the
adapter's own sentence. `NOT_ATTEMPTED` on a package nobody could ever have
attempted reads as "fine so far", which is exactly the overclaim the level scale
exists to prevent.

---

## The package, directory by directory

Written to `data/projects/{project_id}/releases/RC-{candidate:04d}/ESCO_Project_Release_{revision}/`.
The candidate directory is cleared before each generation, so a file from a
previous run cannot survive into a package whose checksums do not reference it.

```
00_Manifest/
  manifest.json            the whole record: target, templates, artifacts,
                           import order, limitations, unsupported items,
                           approvals, determinism and verification statements
  checksums.sha256         every file in the package except itself
  IMPORT_ORDER.md          the order to import in, and why each edge exists
  target_profile.json      the controller family, Studio version, L5X schema
                           revision and restricted-domain modes it was built for
  validation_summary.json  the validation run behind it, by severity
  LIMITATIONS.md           what this package does not do, in words an engineer
                           reads before importing
01_Controller/
  Controller_Project.L5X   the controller context every other file imports into
  Controller_Tags.csv
  Controller_Comments.csv
  Cross_Reference.csv
  UDTs/<Name>.L5X
  AOIs/<Name>.L5X
  Programs/<Program>.L5X
  Routines/<Program>-<Routine>.L5X
02_HMI/                    partner-plugin slot
03_Alarms/                 partner-plugin slot
04_Testing/                partner-plugin slot
05_Reports/                partner-plugin slot
06_Audit/
  change_log.json          every operation that landed up to this revision
  approval_log.json        every settled human decision, with who and when
  evidence_index.json      every generated object and the basis it cites
```

Two files sit outside the manifest's own artifact list by construction:
`manifest.json`, which cannot contain its own hash, and `checksums.sha256`,
which hashes the manifest. `checksums.sha256` covers every other file, so one
value verifies the whole package — which is why `ReleaseCandidate.package_sha256`
is the hash *of the checksum file*.

**`02_HMI` through `05_Reports` are always empty.** The directories are created
and each one's absence is written into `LIMITATIONS.md` naming the discipline and
the reason, because an empty folder with no explanation reads as "nothing was
needed" and that is a claim nobody made. But `app.engineering.release`
constructs every delivery slot with `is_present=False` and no files, so a
delivery plugin's output does not reach the package even when a plugin ran and
produced something. Wiring plugin output into the slots is not done.

### Determinism, and what it buys

No wall-clock reading enters any file — not the L5X `ExportDate`, not the CSV
preamble Studio writes, not the manifest. No identifier is invented during
emission. Every collection is sorted explicitly before it is written.

Without those three the `sha256` in the manifest records nothing more than
"somebody hashed something once". With them, regenerating the same revision
produces byte-identical files, so a re-generated package can be **diffed against
a previously approved one** rather than merely re-hashed. That is the property a
checksum in a release manifest exists for.

---

## The four calls, and what each one refuses

`app.engineering.release`. Each stage can refuse, and a refusal carries every
block rather than the first, so fixing one thing does not reveal the next on the
following attempt. Each block has a stable `BlockCode`, a summary naming the
rule, and a detail naming the object, count or gate — because "release blocked"
gives a UI nothing to render and the person holding the gate nothing to do.

**`release_readiness`** answers "may I press the button" without generating
anything, and re-evaluates the gates while it is there: a gate status computed
against an older revision is not an answer to this question. It reports the
outstanding gates in order, the unwaived error count, whether import
verification is possible at all, and
`highest_attainable_validation_level` — which is the **ceiling this deployment
can reach**, not what any package has reached. Saying so up front is what stops
"verified" being expected later.

**`create_release_candidate`** reserves a candidate number, pins the revision,
and marks that revision `is_release_baseline` so it can never become the base of
an in-place edit. `force=True` buys exactly one thing: permission for the later
stages to proceed when the project has moved on underneath this candidate. It is
taken here, at creation, because that is the moment a human is present to give
it — `generate_artifacts` may well run unattended from a queue, and a prompt
nobody sees is not consent. The permission is recorded as a *limitation on the
package*, so it ships inside the files: a customer holding a package cut from
revision 7 while the project stands at revision 9 has to be able to see that
from the package. Nothing else is forceable.

**`generate_artifacts`** assembles everything in memory and only then writes,
which is what makes the generator's refusals cheap — a model the generator will
not emit leaves nothing on disk, and the candidate is left `VALIDATION_FAILED`
naming the object that stopped it. A generator refusal is *returned* on the
candidate rather than raised: the generator has made an engineering statement,
and it belongs where a reviewer will look for it rather than only in a stack
trace.

**`freeze_manifest`** writes the immutable record against a named person.
`approved_by` is typed optional and refused at runtime on purpose: a signature is
the one thing in this pipeline that cannot be inferred, defaulted or supplied by
an agent, so the rule is enforced where it can be tested rather than left to a
type checker that does not run in production. The manifest is built from the
bytes on disk, and **every** artifact is re-read and re-hashed, not only the
three documents the row quotes. A file that changed since generation is a
refusal (`PACKAGE_ALTERED`), because a package that changed after generation is
not a package anyone can sign.

Then `finalize_release_archive` (`app.engineering.release_archive`) builds the
deterministic outer ZIP. It deliberately does not reuse
`ReleaseCandidate.package_sha256`: that column authenticates
`00_Manifest/checksums.sha256`, while `ReleaseArchive.sha256` authenticates the
complete container. Finalization verifies and atomically publishes the bytes
*before* writing the row, so a process that dies in between leaves an orphan ZIP
that a retry adopts only when it is byte-for-byte the deterministic result.
Existing paths are never overwritten. `GET /api/release-candidates/{id}/download`
re-hashes the file and streams the exact handle it verified, rather than
reopening the path.

---

## Who may do what

| Action | Requires |
| --- | --- |
| Read readiness, candidates, artifacts, download | project membership (any role) |
| Reserve a candidate | project `owner`/`engineer` **and** the global `engineer` role |
| Generate or regenerate artifacts | project `owner`/`engineer` **and** the global `engineer` role |
| Decide a gate | project `owner`/`reviewer` **and** the global `reviewer` role |
| Finalize the delivery ZIP | project `owner`/`reviewer` **and** the global `reviewer` role |
| Enable or disable a gate | the global `administrator` role — **no endpoint** |

Administrators are treated as project owners everywhere, so an orphaned project
can be recovered. Holding the global `engineer` role grants nothing on someone
else's project.

---

## Running it

Verified against this tree. Substitute `.venv/bin/` for `.venv/Scripts/` off
Windows.

```bash
# the acceptance scenario, against a real uvicorn on a throwaway database
.venv/Scripts/python scripts/backend/smoke_production.py

# the review smoke test, for comparison
.venv/Scripts/python scripts/backend/smoke.py

# the pipeline in-process, further than HTTP currently reaches
.venv/Scripts/python -m pytest backend/tests/test_line4_end_to_end.py
```

`smoke_production.py` drives the eight synthetic artifacts in
`fixtures/line4_packaging_conveyor/` and judges what comes back against the
ground truth that fixture's README states **in advance**: seven planted defects
and eleven plausible-looking things that are not defects. It reports four kinds
of result and only one of them can fail the run:

* `check` is a requirement — a failure is a regression and the script exits 1;
* `found` is one of the seven planted issues;
* `quiet` is one of the eleven decoys, and passes when nothing fired;
* `gap` is a step the product is already known not to reach, with the reason.

A `gap` that starts passing is announced as `RESOLVED` so whoever fixed it
deletes the entry. A smoke script that went red on a known limitation would be
silenced within a week, and the limitation would stop being visible at all.

Run against this tree on 2026-08-05 it reports **67 checks passed, 0 failed, 17
gaps**, including a real stop-and-reboot against the same database in the middle.
The project ends `blocked`; no release candidate is generated. The 17 gaps are the
most compact honest statement of what this pipeline does not yet do, and they are
worth reading before the prose below.

---

## What has not been verified, and why

Everything above describes what the code does. This section is what the code
does *not* establish, stated so that nobody has to discover it from a customer.

**No package has been imported into Studio 5000.** Not once, not in CI, not on a
developer's machine. The licensed-workstation adapter is off by default, cannot
be constructed without an administrator enabling it, and no deployment in this
repository has one. Offline verification stops at `ROUND_TRIP_PARSED`. Every
generated package is labelled **"import-ready candidate"** in `LIMITATIONS.md`
and in the manifest, and that label is accurate: it is a candidate for import,
and whether it imports is unknown.

**The manifest always says `rockwell_import_verified: false`.** The generator
writes it unconditionally, because a generator has no way to know. A candidate
whose verification later passes records that on the candidate row and on the
verification job, not in the frozen manifest — so a package's own manifest is
not the place to look for import verification, ever.

**The schema is only as migrated as the working tree.** Four columns
(`change_operations.values`, `change_operations.basis`,
`merge_results.validation_errors`, `logic_instructions.branch_id`) exist in the
ORM and had no migration behind them at commit `01e289f`; revision
`4f7a9c2d1e6b`, "preserve change proposals and branch paths", now covers them and
is **not yet committed**. With it, `alembic upgrade head` → `check` →
`downgrade base` → `upgrade head` round-trips cleanly on SQLite. Without it —
which is what a checkout of `01e289f` gets — `alembic check` reports the drift and
a fresh database is missing those columns. Verify which state you are in before
trusting either answer.

**PostgreSQL is untested.** The models use portable column types and the intent
is full support, but only SQLite is exercised.

**Six of the eleven declared validation disciplines have no validators.**
`ValidationDiscipline` declares `interlocks`, `hmi`, `alarms`, `testing`,
`documentation` and `release`; `ALL_DISCIPLINES` — the set a gate can ask for —
contains only `requirements`, `tags`, `io`, `plc_logic` and `cross_discipline`.
The interlock and mode checks are registered under `plc_logic`, and the HMI,
alarm and test checks under `cross_discipline`. A gate must ask for the
discipline a validator actually reports against; asking for `hmi` would demand
coverage nothing can give and the gate would never pass for a reason no engineer
could act on. There are 25 validators and 63 result codes (62 discipline-
namespaced, plus `validator.failed`).

**A full production run has never completed end to end.** The Line 4 acceptance
suite carries `xfail(strict=True)` markers with the diagnosis of each stop, and
they are the honest state of the product rather than a to-do list somebody
forgot. As recorded there: gate 2 never becomes decidable because the Line 4 I/O
list produces no `Device` rows, so `equipment_model_recorded` cannot be met; and
gate 3's `hmi_screens_recorded` needs `HMIScreen` rows that only the HMI delivery
plugin creates. Every gate after the first undecidable one reports "an earlier
gate is undecided", so generation, freezing and release are unobserved on that
fixture. `test_production_workflow.py` reaches further because its fixture
declares `HmiProduct.NONE`.

**Only one of the seven planted Line 4 defects comes out right**, and the
scoreboard is worth reading before anybody quotes a detection rate:

| Planted issue | Outcome |
| --- | --- |
| 1 — missing accumulation interlock (ILK-404, high) | **not found**; the evidence graph offers `PE401_BLOCKED` as a candidate for `PE402_BLOCKED` on a "same word initials" rule at 0.923, and the agent downgrades a missing protection to a medium naming mismatch naming the wrong device |
| 2 — two safety-relevant devices on one physical channel (high) | **not found, and cannot be**; the canonical importer collapses two I/O rows sharing one address into a single `IOPoint`, and the duplicate check only compares `AddressAssignment` rows, of which the Line 4 list produces none |
| 3 — inconsistent run-command name (low to medium) | **found, but inflated**: raised as a release-blocking `ERROR`, outranking the genuinely high-severity missing interlock it should sit below |
| 4 — alarm that can never annunciate | **not found** at any severity anyone acts on; `Alarm_Register.csv` never reaches the evidence graph because its `Alarm ID` header matches no alarm-name alias, so all seven rows are dropped |
| 5 — HMI binding to a missing tag | **not found, and nothing could find it**; no `HMIScreen`, `HMIObjectInstance` or `HMIBinding` is created from an HMI export at all |
| 6 — genuinely ambiguous accumulation release | **not put back to the customer**; seven clarifications are raised, all from a worksheet's blank columns, and the one deliberate ambiguity is never quoted |
| 7 — the correctly implemented CYL401 diverter | **false positive**; the Cause and Effect worksheet is read as seven more interlock rows with no trip tag, condition, action or failsafe column, so every interlock including the correct one is reported as incompletely specified |

Six of the eleven decoys also fire. Over-reach costs as much as a miss here: a
review that flags everything scores as well as one that is right, and the eleven
decoys are the only instrument that tells them apart.

**Reconstructing an arbitrary historical revision requires reverse-applying
operations.** That is ADR 0001's accepted cost of not duplicating the model on
every merge. Diffs, rollback metadata, impact analysis and the package change log
all work without it.

**Read-only sealing is best-effort.** Originals and published archives are
chmod'ed read-only; some network and container filesystems ignore that, in which
case immutability is a convention rather than an enforced property. A warning is
logged when the chmod fails.

**What is deliberately absent, and is not a gap:** no controller connection,
upload, download, online edit, mode change, force or firmware operation exists
anywhere in the codebase — absent, not disabled;
`backend/tests/security/test_product_boundaries.py` fails if an industrial
protocol library is ever imported. No `.ACD` parsing or fabrication. Models get
no filesystem, shell, database or network access. Restricted domains are
review-only unless an administrator names them in
`ESCO_ENABLED_RESTRICTED_DOMAINS`, and an unrecognised name is refused at startup
rather than silently ignored.
