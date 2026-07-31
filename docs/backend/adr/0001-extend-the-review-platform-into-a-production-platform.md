# ADR 0001 — Extend the review platform into a production platform

**Status:** accepted
**Date:** 2026-07-31
**Supersedes:** nothing
**Applies to:** the Claude-owned backend and engineering core

---

## Context

The repository already contains a working, tested, offline-capable **engineering
review** platform: secure ingestion, seven parsers, a cross-file evidence graph, a
persisted workflow engine, four review agents, findings, approvals, and generated
contracts. 1043 tests pass and a live-process smoke script exercises the whole
thing end to end.

The product brief now asks for a considerably larger promise:

> From project description to human-reviewed, Rockwell-import-ready engineering
> package.

That is a **production** system, not a review system. It has to hold a canonical
engineering model, let agents propose changes to it, version those changes, merge
them safely, validate them across disciplines, gate them behind named human
approval, and emit deterministic Rockwell import candidates with honest
verification status.

The question this ADR settles is what to do with the code that already exists.

## Decision

**Extend it. Do not fork it, and do not build a second application alongside it.**

The existing platform is not a prototype that the production system replaces — it
is the production system's *evidence layer*. The relationship is concrete rather
than rhetorical:

| Existing subsystem | Role in the production platform |
| --- | --- |
| Secure ingestion, hashing, archive/XML hardening | Unchanged. The same intake serves both. |
| Seven parsers + evidence anchors | Becomes the provenance substrate. Every generated object must cite a requirement, template rule or human decision, and requirements are extracted from parsed documents. |
| Evidence graph | Becomes the input to the Requirements Agent, and the fact base the Tag Architecture and PLC Logic agents build on. |
| Workflow engine, leases, restart recovery, events | Unchanged. The production workflow is more task kinds, not a different engine. |
| Findings, approvals, audit | Unchanged, and now also carry gate decisions and release approvals. |
| Agent action contract and runner | Extended with change-set proposals. The refusal rules stay exactly as they are. |

What is genuinely new is a **canonical engineering model** the agents write into,
and a **generation pipeline** that reads out of it. Those are additive.

### Consequences

* No rewrite, no parallel app, no duplicated modules. The 1043 existing tests
  stay meaningful and stay green.
* The new work is a layer on top: `app/engineering/`, `app/generators/`,
  `app/validation/`, `templates/`, plus new entities under `app/models/`.
* The review workflow remains a valid mode of operation. A project whose target
  profile is incomplete can still be reviewed; it simply cannot generate.

---

## Decision 2 — how revisions are represented

The brief requires revisions, diffs, object-level conflict detection, protected
approved objects, merge dry-run, and rollback metadata. Three shapes were
considered.

**(a) Snapshot per revision** — copy the whole project core on every merge.
Trivial to read, but duplicates the entire model on every change and makes
"which revision introduced this rung" a search rather than a lookup. Rejected.

**(b) Bitemporal object versioning** — every core row carries
`valid_from_revision` / `valid_to_revision`, an update closes one row and opens
another. Exact, and answers historical queries directly, but it doubles the
complexity of *every* query in the system for a capability the MVP needs mostly
for diffs. Rejected as disproportionate.

**(c) Current state + change log.** Chosen.

* Core objects hold **current state** with an `object_revision` integer that is
  bumped on every update. That integer is the optimistic-concurrency token and
  the unit of conflict detection: a change set based on `object_revision=4` that
  arrives when the object is at `5` is a conflict, detected by comparison rather
  than by guesswork.
* `ProjectRevision` marks a **merge point** — a numbered, immutable label with
  the change set that produced it and the author who approved it.
* `ChangeOperation` rows record the **before and after** of every field touched.
  A diff between two revisions is the operations merged between them; rollback
  metadata is the `before` snapshot; merge dry-run is running the validators and
  the conflict check without committing.

This gives every capability the brief lists, at the cost of one property it does
not ask for: reconstructing the full state of an arbitrary historical revision
requires reverse-applying operations rather than a single query. That is
recorded as a known limitation rather than hidden.

---

## Decision 3 — generation is deterministic code, never a model

Agents propose. They do not emit artifacts.

A model may propose a change set — "create these tags", "create this rung from
template `esco.motor.standard.v1` with these parameters". Deterministic code
validates the proposal, merges it if it survives, and only *then* deterministic
generators read the canonical model and emit L5X, CSV and the manifest.

The consequence is the one that matters: **the same approved revision always
produces byte-identical artifacts**, so a checksum in a release manifest means
something, and a re-generated package can be compared against a previously
approved one. If a model sat anywhere in the emission path that property would
be gone.

This also settles the L5X question. The PLC Logic Agent never writes XML. It
writes logic IR. The IR is validated against the target profile's instruction
set and tag scope, and a generator turns IR into XML with correct escaping.

---

## Decision 4 — verification status is a level, not a boolean

An artifact is never "valid". It has reached a level:

`GENERATED` → `SCHEMA_VALIDATED` → `INTERNAL_SEMANTIC_VALIDATED` →
`ROUND_TRIP_PARSED` → `ROCKWELL_IMPORT_VERIFIED` → `HUMAN_APPROVED` → `RELEASED`

Everything this backend can do offline stops at `ROUND_TRIP_PARSED` — we can
prove our own generator's output parses back into the same model, which is worth
something, and is not the same as proving Studio 5000 will import it.
`ROCKWELL_IMPORT_VERIFIED` requires the optional licensed-workstation adapter and
is unreachable without it. The level is stored, exported in the manifest, and
shown; nothing is labelled verified because a lower level passed.

Generated artifacts carry the label **"import-ready candidate"** until the
target-version import verification actually passes.

---

## Decision 5 — directory mapping

The brief suggests directories that do not all match this repository. Modules are
**not** duplicated to preserve the suggested names; the mapping is recorded
instead.

| Brief | This repository | Why |
| --- | --- | --- |
| `backend/app/db/**` | `backend/app/database/**` | Pre-existing; `db` is ambiguous with the session variable used throughout. |
| `backend/app/providers/**` | `backend/app/agents/base/providers/**` | Providers are only ever used by the agent runner; keeping them adjacent stops a second, unrelated notion of "provider" appearing. |
| `backend/app/agents/core/**` | `backend/app/agents/review/**` | Pre-existing package holding the Claude-owned agents. New core agents join it rather than founding a second one. |
| `backend/tests/core/**` | `backend/tests/**` | The Claude-owned suite is the whole of `backend/tests`; the partner's suite is `backend/tests_delivery/`. |
| `backend/tests/delivery/**` | `backend/tests_delivery/**` | Already created by the partner agent; a nested `tests/delivery` would sit inside a package I own. |
| `docs/handoffs/SOL.md` | `docs/handoffs/GPT.md` | Same partner agent, pre-existing filename. Not renamed, because it is partner-owned and renaming it would be an edit to a file I do not own. |
| `assets/city/**` | `frontend/src/**` | Partner-owned either way. |

New packages created by this work, which have no pre-existing equivalent:

| Package | Holds |
| --- | --- |
| `backend/app/engineering/` | Project Core services: revisions, change sets, merge, conflict detection, impact analysis |
| `backend/app/generators/plc/` | Logic IR construction and template expansion |
| `backend/app/generators/rockwell/` | L5X/CSV emission, import order, package assembly |
| `backend/app/validation/` | Cross-discipline validators over the canonical model |
| `backend/app/adapters/` | The optional Rockwell workstation adapter interface |
| `templates/` | Versioned ESCO control patterns, treated as code |

---

## Decision 6 — what stays impossible

These are enforced in code, and this ADR exists partly so that a later change
that weakens one is visibly a reversal rather than a refactor.

* No controller connection, upload, download, online edit, mode change, force or
  firmware operation exists anywhere in the codebase — not disabled, absent.
* No `.ACD` parsing or fabrication. `.ACD` uploads are refused at intake with an
  explanation and a pointer to L5X.
* Models get no filesystem, shell, database or network access. They return
  structured proposals into a closed action set and the runner refuses anything
  outside it.
* External providers cannot be constructed unless an administrator enables them.
  The default is a deterministic mock that is not a model and says so on every
  response.
* Safety-related and equipment-damage recommendations cannot be released without
  a named human approval, and restricted domains (motion, GuardLogix and safety
  logic, burner management, process safety, high-energy systems) are
  review-only unless an administrator explicitly enables a qualified workflow.
* Model chain-of-thought is never stored or displayed. A short, user-facing
  rationale is.
