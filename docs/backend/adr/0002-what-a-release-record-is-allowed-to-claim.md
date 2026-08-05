# ADR 0002 — What a release record is allowed to claim

**Status:** accepted
**Date:** 2026-08-05
**Supersedes:** nothing
**Extends:** [ADR 0001](0001-extend-the-review-platform-into-a-production-platform.md),
decisions 2 and 4
**Applies to:** the Claude-owned backend and engineering core

---

## Context

ADR 0001 settled the shape of the platform: extend rather than fork, current
state plus a change log, deterministic generation, verification as a level rather
than a boolean. Building the pipeline out from those decisions to a working
release surfaced three further questions that ADR 0001 does not answer, and that
a future reader would otherwise have to reconstruct from the code:

* **when a human's sign-off stops applying**, now that a project can be merged
  into after a gate has been decided;
* **what level a package as a whole may claim**, now that a package is dozens of
  files that reached different levels;
* **whether a change operation records what was asked for or what happened**,
  now that a merge can refuse, coerce or partially apply a proposal.

They are one ADR rather than three because they are one question asked in three
places: *what is this record entitled to say?* In each case a plausible, simpler
implementation exists, is what a reader would assume, and quietly makes the
record claim more than happened.

---

## Decision 1 — a gate decision expires when the model it was about changes

A gate decision records `decided_revision_number`. The gate **re-opens** when
both of the following hold:

1. the project's current revision is past `decided_revision_number`; **and**
2. at least one operation merged since then touched an `ObjectType` in the gate's
   `covers` set.

Three shapes were considered.

**(a) A decision never expires.** Simplest, and wrong in the way that matters:
an engineer signs off the tag model at revision 4, an agent merges thirty new
tags at revision 5, and the release record still shows a passed tag gate. The
sign-off has silently become a statement about a model that no longer exists.
This is the failure mode the gate feature exists to prevent, so it cannot be the
default. Rejected.

**(b) Any merge re-opens every gate.** Correct in the sense that no stale
approval survives, and unusable. Plenty of merges touch nothing a given gate is
about — a requirement's status, an export profile, a finding disposition — and a
project of any size would present its reviewers with six re-opened gates after
every change. Reviewers who are asked to re-approve things nothing changed about
learn to click through gates, at which point the gate is a button rather than a
decision, and (b) has produced the same outcome as (a) by a longer route.
Rejected.

**(c) Re-open when the decision's *subject* changed.** Chosen. It requires each
gate to declare what it is about, which is `GateSpec.covers` — a frozen set of
object types per gate, with the release gate covering all of them because a
release approves the whole model.

`covers` is not a convenience. It is what makes an approval a statement about
specific content rather than about a moment in time.

### Consequences

* Every gate must declare `covers`, and a new `ObjectType` that belongs to a
  gate's subject and is not added to it produces an approval that outlives the
  thing it approved. That is the failure mode this decision introduces, and it is
  silent. `backend/tests/engineering/test_gates.py` is where a guard belongs.
* "Did anything this gate covers change since the sign-off" is a filter over
  merged change operations, not a comparison of two reconstructed model states.
  That is only possible because of ADR 0001 decision 2: the operations merged
  between two revisions *are* the difference between them. The two decisions are
  load-bearing for each other.
* **A re-opened gate is not forced to `READY`.** It is re-evaluated against its
  own criteria, so a merge that both invalidated the decision and broke a
  criterion lands the gate at `NOT_READY` saying both things. Announcing a gate as
  ready to sign while one of its criteria is unmet would contradict the rule the
  rest of the module enforces.
* The decision fields are **left in place** when a gate re-opens. The row keeps
  saying what was decided and when, and a synthetic `decision_is_current`
  criterion carries the reason it no longer holds. Clearing them would destroy
  the record of a decision that was genuinely made; a reviewer needs both facts
  in front of them.
* `SKIPPED` is excluded from the statuses a merge can invalidate. A gate that was
  never applied made no statement about the model, so there is nothing for a
  merge to make stale.

---

## Decision 2 — a package claims its weakest file's level, and the ceiling is stated before anyone asks

Three rules, taken together, because each one alone is defeatable.

**The candidate's `validation_level` is the minimum over its artifacts.** One CSV
that only reached `GENERATED` means the package reached `GENERATED`, whatever the
L5X files managed. Rolling up to the maximum was the obvious alternative and
promotes a file nobody checked to the standing of one that was. The manifest
states both ends — `highest_level_attained` and `package_level` — so nothing is
hidden, but the *package's* level is the floor.

**`ImportVerificationStatus` is taken from the adapter, never from a default.**
With no licensed workstation configured the status is `UNAVAILABLE` carrying the
adapter's own sentence. `NOT_ATTEMPTED` was the column default and is the wrong
answer: on a package nobody could ever have attempted it reads as "fine so far",
which is exactly the overclaim the level scale exists to prevent.

**`highest_attainable_validation_level` is reported by `release_readiness`,
before generation.** It is the ceiling *this deployment* can reach —
`ROUND_TRIP_PARSED` without a licensed workstation — and not what any package has
reached. A UI that shows it up front is a UI in which nobody is later surprised
that "verified" never arrived. Reporting only per-package levels would leave the
ceiling to be inferred from the absence of a higher one, which is not something
anybody infers.

The ceiling is enforced structurally rather than by convention.
`VerificationOutcome` refuses to exist with `status=PASSED` unless it came from an
adapter reporting `READY`, and `attained_validation_level` returns
`MAX_OFFLINE_VALIDATION_LEVEL` for everything else. A disabled adapter cannot
report `READY`, so there is no sequence of calls that produces
`ROCKWELL_IMPORT_VERIFIED` without a real, enabled, configured workstation behind
it.

### Consequences

* `SCHEMA_VALIDATED`, `INTERNAL_SEMANTIC_VALIDATED`, `HUMAN_APPROVED` and
  `RELEASED` are declared in `ValidationLevel` and **assigned by nothing**. Files
  are `GENERATED` or `ROUND_TRIP_PARSED`; `ROCKWELL_IMPORT_VERIFIED` comes only
  from the adapter. Human approval and release are real and are recorded — on
  `ReleaseCandidate.status`, `approval_state`, `approved_by_id` and the frozen
  `ReleaseManifest` — but not on the validation level, and a client that branches
  on those four members is branching on something it will not see. The members
  stay because the enum is published contract and members are added, never
  renamed. Whether the level scale should have contained them at all is a
  question for a later ADR; removing them now would be a contract break.
* A single unvalidatable file drags the whole package's level down. That is
  intended, and it means adding a new artifact kind without round-tripping it
  lowers every package silently. The manifest's `highest_level_attained` is the
  only place that becomes visible.
* `ROUND_TRIP_PARSED` needs `PackageOptions.settings` to be supplied. Withholding
  it leaves every file at `GENERATED`, which is not a safer answer — it is a less
  true one.
* Gate 5 resolves to `SKIPPED` with a stated reason in every deployment without a
  licensed workstation, and `record_decision` refuses an `APPROVE` on an
  unreachable gate outright. "Skipped, because there was no workstation" and
  "passed" are different facts and the release record keeps them apart.

---

## Decision 3 — a change operation records the proposal and the outcome in separate columns

`ChangeOperation` carries four JSON columns, in two pairs:

| Pair | Written by | Means |
| --- | --- | --- |
| `values`, `basis` | `build_changeset`, once | What the agent asked for, and why |
| `before`, `after` | the merge, on apply or dry-run | What the merge found, and what it wrote |

The proposal used to be written into `after` and the merge overwrote it with the
outcome. That is a single field doing two jobs, and it works exactly as long as
nothing goes wrong: the two are identical when a proposal is applied verbatim. It
fails in every case the audit trail exists for. A merged operation could no
longer answer "what was asked for", so a coerced value, a partially applied
create, or a refused operation left no record of the claim a reviewer would want
to disagree with.

Read the proposal through `changesets.proposed_values` and
`changesets.stated_basis` rather than off the columns, so a future change of
representation stays contained.

### Consequences

* Four columns where two would do, on the highest-volume table in the schema.
  Accepted: a change log that cannot reproduce a refused proposal is not an audit
  trail.
* `basis` keeps the *stated* form of a citation even though the merge resolves
  citations into `ObjectEvidenceLink` rows. An operation that was refused still
  says what it claimed; a link row for a refused operation would not exist.
* **The migration lands separately from the decision.** `values` and `basis` on
  `change_operations`, and `validation_errors` on `merge_results`, were in the ORM
  before any Alembic revision described them; revision `4f7a9c2d1e6b`, "preserve
  change proposals and branch paths", covers them. At the time of writing that
  revision is in the working tree and not committed, so a checkout of `01e289f`
  has the columns in the ORM and not in the schema and `alembic check` reports the
  drift. Recorded because the decision is settled and a reader of the model needs
  it now; the state of the migration is tracked in `docs/handoffs/CLAUDE.md`.

---

## What this ADR does not decide

* Whether `ValidationLevel` should be split into a *file* scale and a *record*
  scale. The four unassigned members are the symptom; the cause is that one enum
  is being used for "how far was this file proved" and "how far has this release
  got", which are different questions. Recorded here so that a later split is
  visibly a correction rather than a whim.
* Whether disabling a gate should have an HTTP surface. It currently does not;
  `set_gate_enabled` is service-level only, which means the reason a gate was not
  applied can only be recorded from inside the process.
