# The ESCO Project Core

The canonical engineering model, and the single door every change to it passes
through.

This document is for someone about to change the model or add an agent that
writes to it. For *why* it is shaped this way, read
[ADR 0001](adr/0001-extend-the-review-platform-into-a-production-platform.md)
first — decision 2 in particular.

For what happens to a revision *after* it is merged — the six approval gates, the
generators, what a verification level does and does not mean, and what the
package contains — read [the release pipeline](release-pipeline.md).
[ADR 0002](adr/0002-what-a-release-record-is-allowed-to-claim.md) records the
three decisions that shape both documents: when a gate decision expires, what
level a package may claim, and why a change operation stores the proposal and the
outcome separately.

---

## The shape in one paragraph

A project has a **current state** (64 tables of engineering objects, out of 104 in
the schema) and a **change log**. Every object carries an `object_revision` integer. An agent
proposes a `ChangeSet` of `ChangeOperation`s, each stating the `object_revision`
it was written against. Deterministic code checks scope, detects conflicts,
validates, applies a human gate where the change warrants one, and merges — all
of it or none of it — producing a numbered `ProjectRevision`. The operations
merged between two revisions *are* the difference between them.

---

## Writing to the model

There is exactly one door: `app.engineering`. Nothing else writes to a canonical
object, and that is not a style preference — conflict detection, the
protected-object rule, the traceability rule and the human gate are only
guarantees if there is no second path.

```python
from app.engineering import build_changeset, merge_changeset

changeset = build_changeset(
    db,
    project=project,
    payload=payload,                 # a validated ProposeChangesetPayload
    author_agent="tag_architecture",
    task_id=task.id,
    agent_run_id=run.id,
    idempotency_key=f"tags:{task.id}",
)
result = merge_changeset(db, changeset=changeset)
```

`build_changeset` is idempotent on `idempotency_key`. The task queue is
at-least-once, so a re-run must converge rather than propose the batch twice.

### What a merge does, in order

1. **Refuse** an unmergeable status or an archived project.
2. **Scope** — every operation's `object_type` must be within the scope the agent
   was given. A Tag Architecture Agent editing a logic rung is a bug, and it
   fails as `SCOPE_VIOLATION` rather than being quietly applied.
3. **Conflicts** — a missing target, a deleted target, a `based_on_object_revision`
   that no longer matches, an approved (and therefore protected) object, a
   duplicate natural key, a missing dependency.
4. **Validation** — a traceable object created with no basis; a field outside the
   writable allowlist.
5. **The human gate** — `APPROVAL_REQUIRED`, and nothing is applied.
6. **Apply**, inside a savepoint. Bump `object_revision`, write the evidence
   links, resolve `temp_key` references, fill in each operation's `before` and
   `after` — and leave the proposal columns untouched (below).
7. **Record** — a `MergeResult` with its conflicts, a new `ProjectRevision`, an
   audit entry, and a `changeset.merged` event.

A **dry run** takes the same path and rolls the savepoint back. It writes a
`MergeResult` with `is_dry_run=True`, because "we checked and it would have
conflicted" is information a reviewer wants, and because a rehearsal that runs
different code proves nothing about the real thing.

---

## Two columns carry the design

**`object_revision`** is the optimistic-concurrency token and the unit of
conflict detection. A change set written against revision 4 that arrives when
the object is at 5 is a conflict established by comparison rather than by
guesswork.

**`approval_state`** makes an object protected. Once `APPROVED`, it cannot be
edited in place — a later change must supersede it explicitly and win a fresh
approval. This is what stops an agent quietly editing something an engineer
signed off.

---

## The proposal is not the outcome

`ChangeOperation` carries four JSON columns in two pairs, and mixing them up is
the easiest way to break the audit trail:

* `values` and `basis` are **what the agent asked for, and why**. Written once by
  `build_changeset` and never touched again.
* `before` and `after` are **what the merge found, and what it wrote**. Empty
  until the operation is applied or rehearsed.

They are identical exactly when nothing went wrong, and the audit trail exists
for the times something did — a coerced value, a partially applied create, a
refused operation. The two pairs used to share `after`, so a merged operation
could no longer say what had been proposed. Read the proposal through
`changesets.proposed_values` and `changesets.stated_basis` rather than off the
columns; see [ADR 0002](adr/0002-what-a-release-record-is-allowed-to-claim.md)
decision 3, including the migration that has not yet been generated for them.

---

## Traceability

`object_evidence_links` cites the basis for an object: a parsed document anchor,
a requirement, an approved template rule, or a named human decision.

Thirteen object kinds — `TRACEABLE_OBJECT_TYPES` — cannot be created without
one. The rule is enforced twice: in the action schema, where a missing basis is
cheapest to catch, and again at merge, because the schema only sees what an
agent sent.

A **parser warning is not an acceptable basis.** "The PDF had no text layer" is
a fact worth recording; it is not a reason for a rung to exist. The acceptable
kinds are in `ACCEPTABLE_BASIS_KINDS`, and the exclusions are deliberate.

---

## Adding an object type

1. Add the member to `ObjectType` in `app/core/enums.py`. **Members are added,
   never renamed** — the value is published contract and is stored in rows.
2. Write the model in the right module under `app/models/`, using
   `UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base`. Never
   redeclare a mixin column.
3. Register any new enum in `Base.type_annotation_map`. A `Mapped[SomeEnum]` on
   a plain `String` stores correctly and loads back as a `str`, silently
   breaking identity comparison on every row read from the database. This has
   bitten the repository once already;
   `backend/tests/test_project_core_schema.py` now fails if it recurs.
4. Export it from `app/models/__init__.py`.
5. Map it in `app/engineering/registry.py` — the ORM class, the writable field
   allowlist, the natural key, and `label_for`.
6. If it must cite a basis, add it to `TRACEABLE_OBJECT_TYPES`.
7. Generate a migration and check it round-trips:
   `alembic upgrade head` → `alembic check` → `downgrade base` → `upgrade head`.
   Point `ESCO_DATABASE_URL` at a throwaway file first; the default is the
   project's live SQLite database. Check what the head revision is before
   autogenerating: another agent's uncommitted revision may already carry columns
   you would otherwise fold into yours.

### Field allowlists

A change operation may not set `id`, `project_id`, `object_revision`,
`approval_state`, `created_in_revision`, `deleted_at` or `approved_by_id`. Those
are the service layer's business. The allowlist is derived from the mapper's
columns minus a denylist, so a new column is writable by default and the
protected ones never are.

---

## Revisions

`ProjectRevision` is a numbered label, not a copy. Revision 0 is created with the
project and means "nothing has been merged yet", so an agent always has a real
base to propose against rather than a null every caller special-cases.

Reconstructing the full state of an arbitrary historical revision means
reverse-applying operations. That is the accepted cost of not duplicating the
model on every merge, and it is recorded here rather than hidden. Diffs, rollback
metadata and impact analysis all work without it.

A revision that produced a release candidate is marked `is_release_baseline` and
must never be the base for an in-place edit: a package with a checksum has to
keep meaning what it meant.

---

## Impact analysis

Mandatory before a tag rename is approved, and `ProposeChangesetPayload` refuses
a change set that renames while claiming to need no approval.

`app.engineering.impact` searches both the resolved foreign keys and the **text
names**, because the text name is where a broken reference hides. Every table
that can reference a tag keeps the name as text alongside a nullable FK,
precisely so an unresolved reference is storable and therefore findable.

Two things it must get right: a substring match is a false positive (`MTR_101`
must not match `MTR_1010`), and unresolved references are listed separately
rather than folded into the total, because they are the ones most likely to
break quietly.

---

## Validation

`app.validation.run_validation` runs every validator over the current revision
and persists `ValidationResult` rows.

Severity is a judgement, not a reflex. **Only `ERROR` blocks a release**, because
treating every warning as a blocker trains people to bypass the gate. Any
`ERROR` in the codebase carries a comment saying why proceeding would produce
something wrong.

`UNSUPPORTED` is a real severity: "we did not check this" has to stay
distinguishable from "we checked and it was fine". A validator that raises is
recorded as a failed check and the run continues — a run that dies halfway and
reports three issues looks like a clean result.

Check codes are stable API, namespaced by discipline
(`logic.duplicate_destructive_output`). Clients branch on them, and customers
write waiver policies against them, so a code is added or retired and **never
quietly repurposed**. There are 25 validators and 63 codes: 62 discipline-
namespaced, plus `validator.failed`, which is the run reporting on itself.

**A gate may only ask for a discipline a validator reports against.**
`ValidationDiscipline` declares eleven members; `ALL_DISCIPLINES` — the set the
gates read — holds five. The interlock and mode checks are registered under
`plc_logic`, and the HMI, alarm and test checks under `cross_discipline`, so a
gate demanding coverage of `hmi` would demand something nothing in the registry
can give and would never pass, for a reason no engineer could act on. There is a
test asserting that every discipline a gate asks for is one a validator covers;
keep it green when adding either.

---

## What the model refuses to express

Recorded so that adding one later is a visible reversal:

* No controller connection, upload, download, online edit, mode change, force or
  firmware operation exists — not disabled, absent.
  `backend/tests/security/test_product_boundaries.py` fails if an industrial
  protocol library is ever imported.
* No `.ACD` parsing. The format is proprietary; uploads are refused with a
  message naming L5X as the supported input.
* Nothing sets `ValidationLevel.ROCKWELL_IMPORT_VERIFIED` without the licensed
  workstation adapter, which is off by default. Offline verification stops at
  `ROUND_TRIP_PARSED` — proving our own generator's output parses back into the
  same model, which is worth something and is not the same as proving Studio
  5000 will import it.
* Restricted domains (motion, safety logic, burner management, process safety,
  high-energy systems) are review-only unless an administrator names them in
  `ESCO_ENABLED_RESTRICTED_DOMAINS`. An unrecognised name is refused at startup
  rather than silently ignored, because an administrator who believes they
  enabled a qualified motion workflow and did not is worse off than one who was
  told the name was wrong.
