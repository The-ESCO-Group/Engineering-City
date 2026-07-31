"""What the customer asked for, decomposed, and where each piece was stated.

Requirements are **extracted, never invented**. Every table here exists to keep
that promise checkable rather than merely intended:

* :class:`RequirementSource` records where a requirement was read from, in a form
  a human can follow back to a paragraph in a document — not just an id.
* ``Requirement.is_inferred`` marks the ones nobody actually wrote down. An
  inferred requirement is a proposal, not a finding of fact: it stays
  ``PROPOSED`` until a named human accepts it, and nothing in the platform may
  promote one to ``ACCEPTED`` on its own. ``needs_clarification`` is that rule
  expressed as a query.
* :class:`Assumption` and :class:`Clarification` are where the gaps go. A gap
  recorded as an assumption with its consequence stated, or as a question with
  the competing readings named, is a gap somebody can close. A gap quietly
  filled with a plausible default is indistinguishable from a requirement once
  it reaches an export, and that is the exact failure this product exists to
  prevent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    ClarificationStatus,
    ObjectType,
    RequirementKind,
    RequirementPriority,
    RequirementStatus,
)
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONList, UTCDateTime
from app.models.core import EngineeringObjectMixin


class Requirement(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One thing the delivered system must do, stated once.

    ``statement`` and ``source_text`` are separate columns because they answer
    different questions. ``statement`` is what this platform will quote
    downstream; ``source_text`` is what the document said. Keeping both means an
    engineer can check the tidying that happened between them, which is the only
    way to catch a requirement that changed meaning while being cleaned up.

    Decomposition is a self-reference rather than a separate hierarchy table: a
    child of ``REQ-014`` is a requirement in its own right — it is cited,
    approved and verified independently — so it is a row of the same shape.
    """

    __tablename__ = "requirements"
    __table_args__ = (
        UniqueConstraint("project_id", "reference", name="uq_requirements_project_id_reference"),
        Index("ix_requirements_project_id_status", "project_id", "status"),
        Index("ix_requirements_project_id_kind", "project_id", "kind"),
    )

    #: Stable per-project key, e.g. ``REQ-014``. Quoted in reports, cited by
    #: generated objects and printed on test cases, so it is assigned once and
    #: never reused: a reference that changes meaning silently invalidates every
    #: document that already quotes it.
    reference: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)

    #: The canonical wording — the requirement exactly as this platform will
    #: quote it in reports, test cases and evidence links. Editorial tidying
    #: (numbering, pronouns, splitting a compound sentence) lands here and
    #: nowhere else; ``source_text`` below preserves what was actually written.
    statement: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: The wording as found, verbatim, when this was derived from a document.
    #: Empty when the requirement was stated verbally or inferred. Never
    #: rewritten — it is the thing ``statement`` gets checked against.
    source_text: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Defaults to ``OTHER`` rather than ``FUNCTIONAL``: an unclassified
    #: requirement is a classification nobody made, and guessing one would hide
    #: that from the validator that reports it.
    kind: Mapped[RequirementKind] = mapped_column(
        default=RequirementKind.OTHER, nullable=False, index=True
    )
    priority: Mapped[RequirementPriority] = mapped_column(
        default=RequirementPriority.EXPECTED, nullable=False
    )
    status: Mapped[RequirementStatus] = mapped_column(
        default=RequirementStatus.PROPOSED, nullable=False, index=True
    )

    #: True when the platform derived this rather than read it. An inferred
    #: requirement may be perfectly sensible and still be something the customer
    #: never asked for, so it carries this flag for the rest of its life — it is
    #: not cleared when the requirement is accepted, only recorded alongside.
    is_inferred: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: 0.0-1.0. On a *requirement* this is not "how likely is this to be true".
    #: It is how sure the extractor is that this sentence is a requirement at
    #: all, rather than narrative, background or a worked example — the failure
    #: mode being a paragraph of scene-setting promoted into a shall-statement.
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    #: Decomposition parent. ``SET NULL`` rather than ``CASCADE`` deliberately:
    #: deleting a parent must not take its children with it. An orphan resurfaces
    #: as a top-level requirement somebody has to re-file, which is visible;
    #: a cascade would delete customer-stated content without a trace.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Order among siblings — document order, where the source had one.
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Free-form labels (``"conveyor"``, ``"phase-2"``). A filtering aid only:
    #: nothing is validated against them, so they can be as loose as the reader
    #: finds useful.
    tags: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    #: Why the requirement exists, when the source gave a reason. A rationale
    #: that merely restates the requirement is worse than an empty one.
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: How it will be proved: inspection, test, analysis or demonstration. A
    #: plain string because that vocabulary is not in ``app.core.enums`` and this
    #: module may not add one; treat the four words as the expected values.
    verification_method: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    #: Readable summary of what was built to satisfy this, for reports. A
    #: convenience cache, never the authority: the real answer is the set of
    #: evidence links citing this requirement as their basis.
    implemented_by_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)

    sources: Mapped[list[RequirementSource]] = relationship(
        back_populates="requirement", cascade="all, delete-orphan", lazy="selectin"
    )
    acceptance_criteria: Mapped[list[AcceptanceCriterion]] = relationship(
        back_populates="requirement", cascade="all, delete-orphan", lazy="select"
    )
    parent: Mapped[Requirement | None] = relationship(
        back_populates="children", remote_side="Requirement.id"
    )
    children: Mapped[list[Requirement]] = relationship(back_populates="parent", lazy="select")

    @property
    def needs_clarification(self) -> bool:
        """Inferred, and nobody has ruled on it yet.

        The work queue for the requirements agent, and the guard on the rule
        that an inferred requirement is never silently promoted: while this is
        true, the item is something the platform made up and a human still owes
        an answer on.
        """
        return self.status is RequirementStatus.PROPOSED and self.is_inferred

    @property
    def is_safety_related(self) -> bool:
        return self.kind is RequirementKind.SAFETY


class RequirementSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Where one requirement was read from.

    A plain link table, not an engineering object: it has no independent life,
    nothing cites it and it is never approved on its own — it is deleted with the
    requirement it explains.

    ``anchor_id`` and ``location_label`` are both kept because they fail
    differently. The anchor is machine-followable and goes stale when a file is
    re-uploaded; the label stays readable in a printed report where no database
    is at hand. A citation that only one of them can resolve is a citation that
    stops working the moment it is most needed.
    """

    __tablename__ = "requirement_sources"
    __table_args__ = (
        Index("ix_requirement_sources_requirement_id_is_primary", "requirement_id", "is_primary"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requirement_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True
    )

    anchor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("evidence_anchors.id", ondelete="SET NULL"), nullable=True, index=True
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("uploaded_files.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: The passage as it stands in the source, copied at extraction time so the
    #: quotation survives the file being replaced.
    quoted_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Human-followable location, e.g. ``"Functional_Description.docx / 3.2
    #: Conveyor Interlocks / paragraph 4"``. Written so an engineer with the
    #: original document and no access to this system can find the passage.
    location_label: Mapped[str] = mapped_column(String(512), default="", nullable=False)

    #: The wording the requirement was actually derived from, when several
    #: sources say overlapping things. Defaults False: which source is primary is
    #: a judgement, and an unmade judgement should not look like a made one.
    is_primary: Mapped[bool] = mapped_column(default=False, nullable=False)

    requirement: Mapped[Requirement] = relationship(back_populates="sources")


class AcceptanceCriterion(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One condition that must hold before a requirement counts as met.

    Rows rather than a list on the requirement, because each criterion is
    approved, tested and cited on its own: a test case points at ``AC-014-2``,
    not at "part of REQ-014".
    """

    __tablename__ = "acceptance_criteria"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "reference", name="uq_acceptance_criteria_project_id_reference"
        ),
        Index("ix_acceptance_criteria_requirement_id_ordinal", "requirement_id", "ordinal"),
    )

    requirement_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Stable per-project key, conventionally derived from its requirement's:
    #: ``AC-014-2`` is the second criterion on ``REQ-014``.
    reference: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    statement: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: False records a criterion written in terms nothing can check ("the system
    #: shall be user-friendly"). Kept rather than dropped: it is a documentation
    #: defect a validator reports, and deleting it would hide that the customer
    #: asked for something.
    is_testable: Mapped[bool] = mapped_column(default=True, nullable=False)
    #: How this would be exercised, and anything that makes it awkward to.
    test_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[RequirementStatus] = mapped_column(
        default=RequirementStatus.PROPOSED, nullable=False, index=True
    )

    requirement: Mapped[Requirement] = relationship(back_populates="acceptance_criteria")


class Assumption(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """Something the platform had to take as given because nobody said.

    An assumption is the honest form of a gap. The dishonest form is a sensible
    default applied inside generated logic, which after export is
    indistinguishable from something the customer asked for.

    An unconfirmed assumption is not a defect — engineering proceeds on
    assumptions all the time. It becomes one when it reaches a release nobody
    has read it against, which is why ``is_confirmed`` is a column and not a
    footnote.
    """

    __tablename__ = "assumptions"
    __table_args__ = (
        UniqueConstraint("project_id", "reference", name="uq_assumptions_project_id_reference"),
        Index("ix_assumptions_project_id_is_confirmed", "project_id", "is_confirmed"),
    )

    #: Stable per-project key, e.g. ``ASM-007``. Quoted in the limitations
    #: document shipped with every release package.
    reference: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    statement: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: The agent that made the leap. Distinct from the mixin's
    #: ``created_by_agent``, which names whoever wrote the row: a later agent
    #: re-recording an assumption must not inherit authorship of the judgement.
    made_by_agent: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    #: What breaks if this turns out to be false. Required in spirit — the
    #: service layer refuses to persist an empty one. An assumption whose
    #: consequence nobody wrote down cannot be prioritised, cannot be tested and
    #: will never be revisited; it is a guess with a reference number.
    impact_if_wrong: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Set only by a named human. No agent may confirm its own assumption — that
    #: would make the record self-certifying and therefore worthless.
    is_confirmed: Mapped[bool] = mapped_column(default=False, nullable=False)
    confirmed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    #: The project question raised to close this assumption, when one was.
    question_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("project_questions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: The requirement the assumption was made while interpreting, when it was
    #: made against one rather than against the project as a whole.
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True, index=True
    )


class DesignConstraint(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A limit the design must respect.

    Called ``DesignConstraint`` and not ``Constraint`` because that name is
    SQLAlchemy's own for a table constraint; two meanings in one namespace is a
    confusion that eventually becomes a bug. The corresponding
    :class:`~app.core.enums.ObjectType` member is still ``CONSTRAINT``.

    A constraint differs from a requirement in direction: a requirement says
    what the system must do, a constraint says what the design may not do
    (existing panel space, a fixed IP range, a controller that must stay on
    firmware 32). They are modelled apart because they are validated apart — a
    constraint is checked against the design, not verified against a test.
    """

    __tablename__ = "design_constraints"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "reference", name="uq_design_constraints_project_id_reference"
        ),
        Index("ix_design_constraints_project_id_is_hard", "project_id", "is_hard"),
    )

    #: Stable per-project key, e.g. ``CON-003``.
    reference: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    statement: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Reuses :class:`RequirementKind`, which has a ``CONSTRAINT`` member, so a
    #: constraint can also be classified by the discipline it comes from
    #: (``REGULATORY``, ``SAFETY``, ``INTERFACE``) rather than flattened.
    kind: Mapped[RequirementKind] = mapped_column(
        default=RequirementKind.CONSTRAINT, nullable=False, index=True
    )
    #: Where the limit came from, in a sentence. Full traceability is a
    #: :class:`~app.models.core.ObjectEvidenceLink`; this is what a reader sees
    #: without following one.
    source_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Hard means violating it is an *error*: validation fails and no release
    #: candidate is produced. Soft means a *warning* an engineer may read and
    #: knowingly accept. Defaults hard, because the cost of being wrong is
    #: asymmetric — a soft limit wrongly treated as binding starts a
    #: conversation, a hard limit wrongly treated as advisory ships a violation.
    is_hard: Mapped[bool] = mapped_column(default=True, nullable=False)


class Clarification(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A specific ambiguity, attached to the thing that is ambiguous.

    Distinct from ``ProjectQuestion``, which is about the project as a whole —
    which controller family, which naming standard, is the HMI in scope. A
    clarification is local: *this* requirement, *this* device, *this* interlock
    can be read two ways, and work on it cannot honestly continue until somebody
    says which reading was meant.

    The target is either a requirement (the common case, hence its own column)
    or any canonical object by ``(object_type, object_id)``. As in
    :class:`~app.models.core.ObjectEvidenceLink`, the polymorphic pair cannot be
    enforced by the database; the service layer checks it at the point of
    creation.
    """

    __tablename__ = "clarifications"
    __table_args__ = (
        Index("ix_clarifications_object", "object_type", "object_id"),
        Index("ix_clarifications_project_id_status", "project_id", "status"),
    )

    requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Set together, and only when the ambiguity is about something other than a
    #: requirement. Both null means the clarification hangs off ``requirement_id``.
    object_type: Mapped[ObjectType | None] = mapped_column(nullable=True)
    object_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    #: The question as it will be put to the customer or the lead engineer.
    #: Answerable in a sentence; if it needs a meeting, it is more than one
    #: clarification.
    question: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: What specifically is unclear, and which readings compete. Naming both
    #: readings is the entire point: "this is ambiguous" gives the reader nothing
    #: to act on, whereas "does *stop* here mean a controlled ramp-down or an
    #: immediate drive disable" can be answered in one word.
    ambiguity: Mapped[str] = mapped_column(Text, default="", nullable=False)

    status: Mapped[ClarificationStatus] = mapped_column(
        default=ClarificationStatus.OPEN, nullable=False, index=True
    )
    #: The answer, in the answerer's words. Kept verbatim: it is cited as a
    #: human decision by whatever gets built once the question is closed.
    resolution: Mapped[str] = mapped_column(Text, default="", nullable=False)
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    #: True stops generation of anything derived from the target until the
    #: question is answered. Defaults True, because continuing past an
    #: unresolved ambiguity means picking a reading, and picking one silently is
    #: inventing a requirement. Clearing it is a deliberate act: someone has
    #: decided the work can proceed either way.
    blocks_generation: Mapped[bool] = mapped_column(default=True, nullable=False)
