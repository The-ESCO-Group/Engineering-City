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
"""

from __future__ import annotations

from app.engineering.changesets import (
    BASIS_KEY,
    TEMP_REFERENCE_PREFIX,
    abandon,
    build_changeset,
    find_by_idempotency_key,
    proposed_values,
    stated_basis,
    submit,
    temp_ref,
)
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
    "BASIS_KEY",
    "MERGEABLE_STATUSES",
    "OBJECT_TYPE_MODELS",
    "PROTECTED_FIELDS",
    "TEMP_REFERENCE_PREFIX",
    "UNWRITABLE_OBJECT_TYPES",
    "FieldValueError",
    "UnknownObjectTypeError",
    "abandon",
    "allocate_revision_number",
    "allowed_fields",
    "build_changeset",
    "create_revision",
    "current_revision_number",
    "describe",
    "ensure_initial_revision",
    "find_by_idempotency_key",
    "get_revision",
    "label_for",
    "latest_revision",
    "mark_release_baseline",
    "merge_changeset",
    "model_for",
    "natural_keys",
    "object_type_for",
    "proposed_values",
    "revision_count",
    "stated_basis",
    "submit",
    "temp_ref",
    "type_label",
]
