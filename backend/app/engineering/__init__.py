"""Project Core: the service layer every change to the canonical model passes through.

Agents propose. This package decides. Nothing else in the codebase writes to the
canonical engineering model, and that is the point: conflict detection, the
protected-object rule, the traceability rule and the human gate are only
guarantees if there is exactly one door.

The shape follows ADR 0001 decision 2 — current state plus a change log. Every
canonical object carries ``object_revision``, an operation states the value it
was written against, and a mismatch is a conflict established by comparison
rather than by guesswork. A :class:`~app.models.revision.ProjectRevision` is a
numbered label on the timeline, and the operations merged between two revisions
*are* the difference between them.

Reading order, which is also the order a change travels:

* :mod:`~app.engineering.registry` — which ORM class an object type names, and
  which of its columns a proposal may set.
* :mod:`~app.engineering.changesets` — a validated proposal becomes an ordered,
  attributable, idempotent change set.
* :mod:`~app.engineering.merge` — scope, conflicts, validation, the approval
  gate, and an all-or-nothing apply inside a savepoint.
* :mod:`~app.engineering.revisions` — the numbered merge points that result.

Then the three modules that read that timeline rather than write to it:

* :mod:`~app.engineering.impact` — what a rename or a delete would reach, before
  anyone agrees to it.
* :mod:`~app.engineering.gates` — the six places a named human takes
  responsibility for a body of work, each stating what is still missing.
* :mod:`~app.engineering.release` — an approved revision turned into a package,
  or the list of reasons it may not be.

Re-exported here so that the workflow, the API and the agent handlers all reach
the same door. ``impact``, ``gates`` and ``release`` are also importable as
modules, which is how a caller that wants ``gates.GATE_SPECS`` alongside
``gates.evaluate_all`` should take them.
"""

from __future__ import annotations

from app.engineering import gates, impact, release
from app.engineering.changesets import (
    TEMP_REFERENCE_PREFIX,
    abandon,
    build_changeset,
    find_by_idempotency_key,
    proposed_values,
    stated_basis,
    submit,
    temp_ref,
)
from app.engineering.gates import (
    GATE_SPECS,
    ensure_gates,
    evaluate_all,
    evaluate_gate,
    gate_blocking,
    record_decision,
    request_decision,
)
from app.engineering.impact import analyse_changeset, analyse_delete, analyse_rename
from app.engineering.merge import MERGEABLE_STATUSES, merge_changeset
from app.engineering.registry import (
    OBJECT_TYPE_MODELS,
    PROTECTED_FIELDS,
    UNWRITABLE_OBJECT_TYPES,
    FieldValueError,
    UnknownObjectTypeError,
    allowed_fields,
    describe,
    label_for,
    model_for,
    natural_keys,
    object_type_for,
    type_label,
)
from app.engineering.release import (
    FORCED_STALE_REVISION_CODE,
    RELEASES_DIR,
    BlockCode,
    ReleaseBlock,
    ReleaseReadiness,
    ReleaseRefused,
    create_release_candidate,
    freeze_manifest,
    generate_artifacts,
    release_readiness,
    was_forced_past_stale_revision,
)
from app.engineering.revisions import (
    allocate_revision_number,
    create_revision,
    current_revision_number,
    ensure_initial_revision,
    get_revision,
    latest_revision,
    mark_release_baseline,
    revision_count,
)

__all__ = [
    "FORCED_STALE_REVISION_CODE",
    "GATE_SPECS",
    "MERGEABLE_STATUSES",
    "OBJECT_TYPE_MODELS",
    "PROTECTED_FIELDS",
    "RELEASES_DIR",
    "TEMP_REFERENCE_PREFIX",
    "UNWRITABLE_OBJECT_TYPES",
    "BlockCode",
    "FieldValueError",
    "ReleaseBlock",
    "ReleaseReadiness",
    "ReleaseRefused",
    "UnknownObjectTypeError",
    "abandon",
    "allocate_revision_number",
    "allowed_fields",
    "analyse_changeset",
    "analyse_delete",
    "analyse_rename",
    "build_changeset",
    "create_release_candidate",
    "create_revision",
    "current_revision_number",
    "describe",
    "ensure_gates",
    "ensure_initial_revision",
    "evaluate_all",
    "evaluate_gate",
    "find_by_idempotency_key",
    "freeze_manifest",
    "gate_blocking",
    "gates",
    "generate_artifacts",
    "get_revision",
    "impact",
    "label_for",
    "latest_revision",
    "mark_release_baseline",
    "merge_changeset",
    "model_for",
    "natural_keys",
    "object_type_for",
    "proposed_values",
    "record_decision",
    "release",
    "release_readiness",
    "request_decision",
    "revision_count",
    "stated_basis",
    "submit",
    "temp_ref",
    "type_label",
    "was_forced_past_stale_revision",
]
