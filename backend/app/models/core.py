"""The spine every canonical engineering object shares.

Three things live here because they have to be identical everywhere, and a
convention repeated in nine model modules is a convention that drifts:

* :class:`EngineeringObjectMixin` — the columns that make an object versionable,
  attributable and protectable.
* :class:`ObjectEvidenceLink` — the polymorphic citation table. It is what turns
  "every generated object is traceable" from an aspiration into a query.
* :data:`TRACEABLE_OBJECT_TYPES` — the object kinds that may not exist without a
  basis, named in one place so the rule is checkable rather than remembered.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import (
    ACCEPTABLE_BASIS_KINDS,
    TRACEABLE_OBJECT_TYPES,
    EvidenceKind,
    LogicOrigin,
    ObjectApprovalState,
    ObjectType,
)
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, UTCDateTime


class EngineeringObjectMixin:
    """Columns shared by every object in the canonical engineering model.

    ``object_revision`` is the load-bearing one. It is bumped on every update and
    is both the optimistic-concurrency token and the unit of conflict detection:
    a change set built against revision 4 that arrives when the object is at 5
    is a conflict established by comparison, not by guesswork.

    ``approval_state`` is the other. Once an object is ``APPROVED`` it is
    protected — a later change set must supersede it explicitly and win a fresh
    approval, so no agent can quietly edit something an engineer signed off.

    An approval records three things and needs all three: who
    (``approved_by_id``), when (``approved_at``) and *what*
    (``approved_object_revision``). The third is what makes "approved, and edited
    since" answerable by comparison rather than by inference from timestamps.
    """

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Bumped on every update. Compare to detect a stale-based change.
    object_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    #: Project revision numbers, not foreign keys: a revision is a label on the
    #: project timeline and an object should not pin one alive.
    created_in_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_in_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    approval_state: Mapped[ObjectApprovalState] = mapped_column(
        default=ObjectApprovalState.DRAFT, nullable=False, index=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The ``object_revision`` the approval covers. Recording an approval is
    #: itself an update, so ``approved_at`` and ``updated_at`` are two reads of
    #: the same wall clock in the same flush and their order is undefined —
    #: which made an approval intermittently indistinguishable from a later edit
    #: of the thing it approved. A revision cannot race: the reviewer signed
    #: revision *n*, and only an edit takes the object to *n + 1*.
    #:
    #: Null means no approval has been recorded, or that whatever recorded one
    #: did not state what it covered. That is a gap in the evidence rather than a
    #: fresh signature, and ``ApprovalIntegrityValidator`` reports it as one.
    approved_object_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: How this object came to exist. Recorded at creation and never inferred
    #: afterwards, so "an agent proposed this" stays distinguishable from "this
    #: was read out of the customer's export".
    origin: Mapped[LogicOrigin] = mapped_column(default=LogicOrigin.AGENT_PROPOSED, nullable=False)
    #: The change set that created the object, when one did.
    source_changeset_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    created_by_agent: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Soft delete. A deleted object keeps its row so a citation resolves to a
    #: tombstone rather than a dangling id.
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    #: Set when the object was replaced rather than removed.
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    #: Free-form structured payload for fields that are genuinely per-project.
    #: Never a substitute for a column something is queried or validated on.
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def is_protected(self) -> bool:
        """Approved objects may not be edited in place."""
        return self.approval_state is ObjectApprovalState.APPROVED


# ``TRACEABLE_OBJECT_TYPES`` and ``ACCEPTABLE_BASIS_KINDS`` are defined in
# :mod:`app.core.enums` and re-exported here. They live with the vocabulary
# rather than beside the ORM so that the action schema can enforce the
# traceability rule without the agent layer importing the database.
__all__ = [
    "ACCEPTABLE_BASIS_KINDS",
    "TRACEABLE_OBJECT_TYPES",
    "EngineeringObjectMixin",
    "ObjectEvidenceLink",
]


class ObjectEvidenceLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Cites the basis for one canonical object.

    Polymorphic by ``(object_type, object_id)`` rather than a foreign key per
    target: there are forty-odd object types and a citation table per type would
    make "show me everything derived from requirement REQ-014" a forty-way union.

    The trade is that the database cannot enforce the reference. The merge
    validator does, at the point where an object is created, which is the only
    moment at which a missing basis is still cheap to fix.
    """

    __tablename__ = "object_evidence_links"
    __table_args__ = (
        # One link per (object, basis). A single UNIQUE over the four basis
        # columns cannot express that: three of them are NULL on every row the
        # application can produce, NULL never equals NULL, so such a constraint
        # can never be violated and the table would have no uniqueness at all.
        # Because exactly one basis column is set per row, the rule decomposes
        # into one partial unique index per basis kind, each covering only the
        # rows where that basis is present, which is a predicate both
        # PostgreSQL and SQLite enforce.
        #
        # ``basis_kind`` is deliberately absent from all four: citing the same
        # anchor for the same object twice is a duplicate however the second
        # copy classifies itself. That matches the check-then-insert in
        # ``engineering/merge.py``, which these indexes now backstop under
        # concurrent merges.
        Index(
            "uq_object_evidence_links_anchor",
            "object_type",
            "object_id",
            "anchor_id",
            unique=True,
            sqlite_where=text("anchor_id IS NOT NULL"),
            postgresql_where=text("anchor_id IS NOT NULL"),
        ),
        Index(
            "uq_object_evidence_links_requirement",
            "object_type",
            "object_id",
            "requirement_id",
            unique=True,
            sqlite_where=text("requirement_id IS NOT NULL"),
            postgresql_where=text("requirement_id IS NOT NULL"),
        ),
        Index(
            "uq_object_evidence_links_decision",
            "object_type",
            "object_id",
            "decision_id",
            unique=True,
            sqlite_where=text("decision_id IS NOT NULL"),
            postgresql_where=text("decision_id IS NOT NULL"),
        ),
        # ``template_id`` is NOT NULL with an empty-string default rather than
        # nullable, so "no template basis" is '' and the predicate excludes it.
        Index(
            "uq_object_evidence_links_template",
            "object_type",
            "object_id",
            "template_id",
            unique=True,
            sqlite_where=text("template_id != ''"),
            postgresql_where=text("template_id != ''"),
        ),
        Index("ix_object_evidence_links_object", "object_type", "object_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    object_type: Mapped[ObjectType] = mapped_column(nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False)

    #: Exactly one of these four is set, matching ``basis_kind``.
    #:
    #: ``SET NULL`` rather than ``CASCADE``, and the difference is the whole
    #: point of the table. Anchors are hard-deleted on every re-parse — anchor
    #: keys are positional, so a routine rename or a parser-version bump makes
    #: the old keys disappear and
    #: :func:`app.core.services.parsing._persist_anchors` removes the rows. A
    #: cascade here would take the citation with them, so an object an engineer
    #: had already approved would be left with no basis *and* nothing recording
    #: that it ever had one. With ``SET NULL`` the link survives: ``basis_kind``
    #: and ``note`` still say what the object was built on and why, and
    #: :attr:`satisfies_traceability` turns False, which is a reportable state
    #: rather than a silent disappearance. Mirrors
    #: :attr:`app.models.requirements.RequirementSource.anchor_id`.
    anchor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("evidence_anchors.id", ondelete="SET NULL"), nullable=True, index=True
    )
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True, index=True)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True, index=True)
    #: Template identifier and version, e.g. ``esco.motor.standard.v1``.
    template_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    basis_kind: Mapped[EvidenceKind] = mapped_column(nullable=False, index=True)

    #: One sentence, shown next to the object. Written to be read by an engineer
    #: checking whether the stated basis actually supports what was built.
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    @property
    def satisfies_traceability(self) -> bool:
        """Whether this link counts towards the mandatory-basis rule."""
        if self.basis_kind not in ACCEPTABLE_BASIS_KINDS:
            return False
        return bool(self.anchor_id or self.requirement_id or self.decision_id or self.template_id)
