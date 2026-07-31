"""The project profile, its target platform, and the questions and decisions around them.

This module holds the objects that decide whether a project is allowed to
generate anything at all. Nothing here describes plant, logic or tags; it
describes the frame those are produced inside:

* :class:`ProjectProfile` — what the project is, which ESCO standards and
  templates it is bound to, how far it intends to take generation, and whether
  it touches a restricted domain.
* :class:`TargetPlatform` — the controller and HMI the output has to be valid
  for. An L5X is only "correct" relative to a firmware revision and an
  instruction set, so this is a first-class object rather than profile fields.
* :class:`EngineeringStandardSet` / :class:`TemplateSet` — the versioned,
  checksummed rule and template payloads a project pins itself to.
* :class:`ProjectQuestion` — what the platform needs answered before it can
  proceed, and what stays blocked until it is.
* :class:`ProjectDecision` — a named human's recorded engineering decision. This
  is the object :data:`~app.core.enums.EvidenceKind.HUMAN_DECISION` points at,
  so a generated object can cite "an engineer decided this" and the citation
  resolves to a quotable sentence with a name against it.

The load-bearing idea is that an incomplete profile is not a warning, it is a
stop. A profile that cannot say what naming standard applies cannot be used to
name a tag, and guessing is the exact failure this product exists to prevent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import utcnow
from app.core.enums import (
    ClarificationStatus,
    ControllerFamily,
    DecisionKind,
    HmiProduct,
    ProfileFieldStatus,
    ProjectType,
    QuestionCategory,
    ReleaseMode,
    RestrictedDomainMode,
    SafetyClassification,
    UnitSystem,
)
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, JSONList, UTCDateTime
from app.models.core import EngineeringObjectMixin

#: Profile columns whose absence stops generation, named in one place so the
#: rule is checkable rather than remembered. These are the keys the workflow
#: writes into :attr:`ProjectProfile.completeness`, and the values it copies
#: into :attr:`ProjectProfile.missing_required_fields`.
#:
#: Each one gates a concrete act: without a naming standard nothing can be
#: named, without an alarm philosophy no alarm can be given a severity, without
#: a template set and a standard set there is no approved pattern to expand, and
#: without a locale every generated description is written in a guessed
#: language. ``project_description`` is here because it is the text requirements
#: are extracted from — with no description there is nothing to derive from.
#:
#: Generation additionally requires a :class:`TargetPlatform` row. That is not a
#: column on this table, so it is checked separately rather than smuggled into
#: this tuple.
REQUIRED_PROFILE_FIELDS: tuple[str, ...] = (
    "project_name",
    "project_description",
    "project_type",
    "naming_standard_version",
    "alarm_philosophy_version",
    "template_set_id",
    "engineering_standard_set_id",
    "units",
    "locale",
)


class ProjectProfile(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """What this project is, and what it is permitted to produce.

    One row per project. Everything the intake brief established lives here, and
    :attr:`blocks_generation` turns it into a single question the rest of the
    platform can ask before it does any work.

    The profile is deliberately separate from :class:`~app.models.project.Project`.
    A ``Project`` is a workspace — files, members, tasks, status. A profile is an
    approved engineering statement about that workspace, versioned and
    supersedable like every other canonical object, so changing the naming
    standard mid-project is a recorded, approvable event rather than an edit.
    """

    __tablename__ = "project_profiles"
    __table_args__ = (
        # One profile per project. The uniqueness is what lets every other
        # module look the profile up by ``project_id`` alone.
        UniqueConstraint("project_id", name="uq_project_profiles_project_id"),
    )

    #: The project's name as the brief states it. Kept apart from
    #: ``projects.name``, which is only the workspace label an ESCO user typed
    #: when creating the workspace and may be an internal shorthand.
    project_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: The customer this is for, or the internal programme name when the project
    #: is ESCO's own work.
    customer_label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    plant_or_site: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    #: The description exactly as supplied, before any agent rewrote or
    #: summarised it. Requirements are extracted from this text and cite back
    #: into it, so an edited copy would break the citation it is the root of.
    project_description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    project_type: Mapped[ProjectType] = mapped_column(default=ProjectType.OTHER, nullable=False)

    #: Versions, not names: "ESCO naming standard" is meaningless without the
    #: revision, and a tag generated under v3 must stay attributable to v3 after
    #: v4 ships.
    naming_standard_version: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    alarm_philosophy_version: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    #: The pinned template and standard payloads. Nullable because a profile
    #: exists from the moment intake starts, long before anyone has chosen them;
    #: ``completeness`` is what records that the choice is still outstanding.
    template_set_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("template_sets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    engineering_standard_set_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID,
        ForeignKey("engineering_standard_sets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    #: :class:`~app.core.enums.UnitSystem` has no "not stated" member, so this
    #: default is a display convention rather than an engineering statement. The
    #: fact that nobody has actually said which system applies is carried by
    #: ``completeness["units"]``, not by this column.
    units: Mapped[UnitSystem] = mapped_column(default=UnitSystem.METRIC, nullable=False)
    #: BCP 47 tag, e.g. ``en-US``. Drives the language of every generated tag
    #: description, alarm message and HMI caption.
    locale: Mapped[str] = mapped_column(String(35), default="", nullable=False)

    #: Prose: controller network topology, remote I/O chassis, VLAN separation,
    #: whatever the brief said. Unbounded because it is quoted, not parsed.
    network_architecture: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Defaults to the mode that needs no licensed Rockwell workstation, because
    #: assuming a licensed workstation exists is assuming a fact about the
    #: customer's site that nobody has stated.
    release_mode: Mapped[ReleaseMode] = mapped_column(
        default=ReleaseMode.IMPORT_PACKAGE_ONLY, nullable=False, index=True
    )

    #: :class:`~app.core.enums.RestrictedDomain` values. A list rather than a
    #: flag: a project can be motion *and* burner management, and which domain
    #: applies decides which qualification a reviewer needs.
    restricted_domains: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    #: Defaults to read-and-report. Enabling a qualified workflow is an
    #: administrator action, never something intake infers from a document.
    restricted_domain_mode: Mapped[RestrictedDomainMode] = mapped_column(
        default=RestrictedDomainMode.MANUAL_REVIEW_ONLY, nullable=False
    )

    # -- Completeness -----------------------------------------------------
    # An incomplete profile must be able to say *which* fields are missing and
    # *why*, because that is what the workflow turns into a clarification. A
    # bare "profile incomplete" would force a human to diff the profile against
    # the standard by hand, which is the work the platform is meant to do.
    #: Maps each name in :data:`REQUIRED_PROFILE_FIELDS` to a
    #: :class:`~app.core.enums.ProfileFieldStatus` value. The status is the
    #: "why": ``missing`` needs an answer, ``ambiguous`` needs a choice between
    #: readings, ``unsupported`` needs a scope decision — three different
    #: questions, so they are three different statuses rather than one null.
    completeness: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    #: Denormalised from ``completeness`` so the blocking set is one read, and so
    #: a clarification can be composed without re-deriving it.
    missing_required_fields: Mapped[list[Any]] = mapped_column(
        JSONList, default=list, nullable=False
    )
    #: Recomputed whenever the profile changes. False until proven otherwise: a
    #: profile nobody has evaluated is not a complete one.
    is_complete_for_generation: Mapped[bool] = mapped_column(default=False, nullable=False)

    @property
    def unusable_required_fields(self) -> dict[str, ProfileFieldStatus]:
        """Required fields that are not usable yet, and why each one is not.

        ``completeness`` holds plain strings because it is JSON. Reading it back
        through the enum here means a caller composing a clarification does not
        have to remember what ``"ambiguous"`` was supposed to mean, and a value
        this application did not write fails loudly instead of being ignored.
        """
        unusable: dict[str, ProfileFieldStatus] = {}
        for field_name, raw_status in self.completeness.items():
            status = ProfileFieldStatus(raw_status)
            if status is not ProfileFieldStatus.PRESENT:
                unusable[field_name] = status
        return unusable

    @property
    def blocks_generation(self) -> bool:
        """Whether this profile forbids generating anything for the project.

        This is a product rule, not a convenience. Three independent conditions
        each stop generation on their own, and every one of them is a case where
        producing a plausible-looking artifact would be worse than producing
        nothing:

        * ``REVIEW_ONLY`` — the project was explicitly scoped to read and report.
        * an incomplete profile — the standards that would govern the output are
          not established, so anything produced would be governed by a guess.
        * a restricted domain without a qualified workflow — generating motion,
          safety or burner-management logic needs a reviewer with a formal
          qualification, and the platform must not produce work that nobody
          present is competent to check.

        Callers gate on this rather than re-deriving it, so there is one place
        to change if the rule changes.
        """
        if self.release_mode is ReleaseMode.REVIEW_ONLY:
            return True
        if not self.is_complete_for_generation:
            return True
        return bool(self.restricted_domains) and (
            self.restricted_domain_mode is not RestrictedDomainMode.QUALIFIED_WORKFLOW_ENABLED
        )


class TargetPlatform(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """The controller and HMI the generated output has to be valid for.

    Separate from :class:`ProjectProfile` because it is a different kind of
    statement with a different reviewer. A profile says what the project is; a
    target platform says what "correct" means for the files that come out of it.
    An L5X is valid relative to a firmware revision, a Studio 5000 version and an
    instruction set — change any of them and the same generated logic may no
    longer import.
    """

    __tablename__ = "target_platforms"
    __table_args__ = (
        # One target platform per project. Multi-controller projects are modelled
        # by the controller/module objects, not by a second platform row: two
        # rows here would mean two answers to "what does correct mean".
        UniqueConstraint("project_id", name="uq_target_platforms_project_id"),
    )

    controller_family: Mapped[ControllerFamily] = mapped_column(
        default=ControllerFamily.OTHER, nullable=False, index=True
    )
    #: Rockwell catalog number, e.g. ``1756-L83E``.
    controller_catalog_number: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    #: Major.minor as Rockwell writes it, e.g. ``35.011``. A string, not a
    #: number: ``35.011`` and ``35.11`` are different firmware revisions and
    #: parsing this as a float would silently merge them.
    firmware_revision: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    #: The Studio 5000 version the import package targets, e.g. ``35.00``.
    studio_5000_version: Mapped[str] = mapped_column(String(20), default="", nullable=False)

    #: ``UNKNOWN`` by default and deliberately so. A controller nobody has
    #: classified is not a standard controller; treating it as one would be
    #: inventing the single most consequential decision in the profile.
    safety_classification: Mapped[SafetyClassification] = mapped_column(
        default=SafetyClassification.UNKNOWN, nullable=False, index=True
    )

    #: ``NONE`` by default. There is no "not stated" member, and defaulting to a
    #: real product would have the platform generate displays for an HMI nobody
    #: asked for; ``NONE`` fails closed by generating nothing until someone says.
    hmi_product: Mapped[HmiProduct] = mapped_column(default=HmiProduct.NONE, nullable=False)
    hmi_version: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    #: Terminal catalog or model, e.g. ``2711P-T15C22D9P``.
    hmi_terminal_model: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    #: Screen geometry in pixels; ``0`` means not stated. Real columns rather
    #: than an ``attributes`` key because display layout validation compares
    #: against them.
    hmi_resolution_width: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    hmi_resolution_height: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: FactoryTalk deployment shape: Directory scope, server roles, redundancy.
    #: Prose, because it is quoted into the delivery package, not parsed.
    factorytalk_architecture: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Names the instruction-set profile the logic validator loads, e.g.
    #: ``logix.v35.standard``. A ``String`` rather than an enum because
    #: instruction-set profiles are data that ships and versions independently of
    #: this contract; the validator resolves the key and refuses to run on one it
    #: does not recognise, so a typo is caught rather than assumed.
    supported_instruction_set_key: Mapped[str] = mapped_column(
        String(80), default="", nullable=False
    )
    #: What this platform cannot do, stated up front and carried into the
    #: delivery package: unsupported instructions, features this firmware lacks,
    #: anything the generator will refuse. Written at profile time so a
    #: limitation surfaces before work is done against it, not after.
    known_limitations: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    @property
    def is_safety_controller(self) -> bool:
        """Whether safety-rated logic is in scope for this controller.

        The family is checked as well as the classification: a GuardLogix is a
        safety controller whatever anyone wrote in the classification field, and
        an unclassified one must not read as standard just because nobody filled
        the field in.
        """
        if self.controller_family in {
            ControllerFamily.GUARDLOGIX,
            ControllerFamily.COMPACT_GUARDLOGIX,
        }:
            return True
        return self.safety_classification in {
            SafetyClassification.SAFETY_RATED,
            SafetyClassification.MIXED_STANDARD_AND_SAFETY,
        }


class EngineeringStandardSet(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A named, versioned set of ESCO engineering standards a project is bound to.

    ``project_id`` comes from :class:`~app.models.core.EngineeringObjectMixin`
    and that is deliberate, even though a standard set reads like a global
    library object. Rows here are the copy the project pinned. If a project
    referenced a shared library row, editing that row would retroactively change
    the standard an approved project was generated under, and every citation
    pointing at it would start resolving to text nobody approved. Materialising
    a copy per project makes "which rules produced this tag" answerable forever.

    ``checksum`` is what makes the pin verifiable rather than assumed: it is the
    sha256 of the rule payload, so a modified copy is detectable.
    """

    __tablename__ = "engineering_standard_sets"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "key",
            "version",
            name="uq_engineering_standard_sets_project_id_key_version",
        ),
    )

    #: Stable dotted identifier, e.g. ``esco.standards.controls``.
    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: Semantic version of the standard set, e.g. ``2.1.0``.
    version: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: The rule payload itself. Structured, because validators read it; opaque
    #: to this table, because the rule vocabulary versions with the set rather
    #: than with the schema.
    rules: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    #: sha256 hex of the serialised ``rules`` payload.
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)

    #: Cleared rather than deleted when a set is withdrawn, so projects already
    #: generated against it keep a resolvable reference.
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)


class TemplateSet(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A named, versioned collection of approved ESCO templates.

    Same pinning argument as :class:`EngineeringStandardSet`: the row is the
    project's own copy, checksummed, so logic expanded from ``esco.motor.v1``
    stays attributable to the exact template text that produced it.

    ``supported_controller_families`` is on the set rather than on each template
    because selection happens at profile time: choosing a template set for a
    Micro800 project when the set only covers ControlLogix is a mistake worth
    catching before any logic is generated, not per expansion.
    """

    __tablename__ = "template_sets"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "key", "version", name="uq_template_sets_project_id_key_version"
        ),
    )

    #: Stable dotted identifier, e.g. ``esco.templates.discrete``.
    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    version: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Template identifiers with their versions, e.g.
    #: ``["esco.motor.standard.v1", "esco.valve.twoposition.v2"]`` — the same strings
    #: ``object_evidence_links.template_id`` cites.
    template_ids: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    #: sha256 hex of the serialised template payload.
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    #: :class:`~app.core.enums.ControllerFamily` values this set is valid for.
    supported_controller_families: Mapped[list[Any]] = mapped_column(
        JSONList, default=list, nullable=False
    )


class ProjectQuestion(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """Something the platform needs answered before it can proceed.

    A question is how the product refuses to guess. When an agent hits an
    ambiguity it raises one of these instead of picking a reading, and
    :attr:`is_blocking` plus ``blocks_task_kinds`` say precisely what stops in
    the meantime — so the workflow can keep doing the work that is unaffected
    rather than halting the whole project.

    Project-scoped, and that is the whole distinction from
    ``app.models.requirements.Clarification``: this is "which controller family,
    which naming standard, is the HMI in scope", while a clarification hangs off
    the one requirement or device that can be read two ways. They are separate
    tables because they block different amounts of work and are answered by
    different people.

    ``why_it_matters`` is not decoration. A question with no stated consequence
    is one the answering engineer cannot prioritise, and it is the sentence that
    goes in front of them when they are asked to unblock a project.

    Citations to a question's answer go through ``object_evidence_links``
    pointing at the resulting :class:`ProjectDecision`, so there is no evidence
    column here: the answer is evidence because a named human recorded it, not
    because it is written on this row.
    """

    __tablename__ = "project_questions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "reference", name="uq_project_questions_project_id_reference"
        ),
        Index("ix_project_questions_project_id_status", "project_id", "status"),
    )

    #: Short human-facing key, e.g. ``Q-001``. Stable for the life of the
    #: project so it can be quoted in an email or a meeting and still resolve.
    reference: Mapped[str] = mapped_column(String(40), nullable=False)

    category: Mapped[QuestionCategory] = mapped_column(
        default=QuestionCategory.OTHER, nullable=False, index=True
    )
    #: The question as it will be shown to the person answering it.
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: What is blocked until this is answered, and what goes wrong if it is
    #: answered incorrectly. Written for the engineer, not for the agent.
    why_it_matters: Mapped[str] = mapped_column(Text, default="", nullable=False)

    status: Mapped[ClarificationStatus] = mapped_column(
        default=ClarificationStatus.OPEN, nullable=False, index=True
    )
    #: Mandatory by default. An agent that wants to proceed without an answer has
    #: to say so explicitly, rather than raising a question that quietly does
    #: nothing.
    is_mandatory: Mapped[bool] = mapped_column(default=True, nullable=False)
    #: :class:`~app.core.enums.TaskKind` values held up by this question. Empty
    #: means "blocks nothing specific"; a mandatory question with an empty list
    #: still blocks the profile gate via :attr:`is_blocking`.
    blocks_task_kinds: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    answer: Mapped[str] = mapped_column(Text, default="", nullable=False)
    answered_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    #: Agent kind that raised the question. Distinct from the mixin's
    #: ``created_by_agent``, which records who wrote the row: a question is
    #: raised inside one agent's proposal and persisted when a change set is
    #: merged, so the two are not always the same agent.
    asked_by_agent: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    #: The task that was doing the work when the ambiguity surfaced. Soft, since
    #: the question outlives the task that found it.
    raised_in_task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )

    @property
    def is_blocking(self) -> bool:
        """Whether this question currently stops work.

        ``DEFERRED`` still blocks a mandatory question. Deferring records that a
        human looked at it and chose to come back later; it does not supply the
        missing answer, and a mandatory question is by definition one the
        platform cannot proceed without. ``DISMISSED`` and ``OBSOLETE`` do not
        block, because both are explicit statements that the answer is no longer
        needed.
        """
        if not self.is_mandatory:
            return False
        return self.status in {ClarificationStatus.OPEN, ClarificationStatus.DEFERRED}


class ProjectDecision(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A named human's recorded engineering decision, citable as evidence.

    This is what :data:`~app.core.enums.EvidenceKind.HUMAN_DECISION` resolves to.
    It exists so that "an engineer decided this" is a row with a name, a date and
    a quotable sentence, rather than a claim an agent makes about a conversation
    it cannot produce.

    ``statement`` and ``rationale`` are separate for the same reason
    :class:`~app.models.finding.Finding` splits ``fact`` from ``inference``: a
    report quotes the decision, and the reasoning behind it must not be able to
    creep into the quotation.
    """

    __tablename__ = "project_decisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "reference", name="uq_project_decisions_project_id_reference"
        ),
        Index("ix_project_decisions_project_id_kind", "project_id", "kind"),
    )

    #: Short human-facing key, e.g. ``DEC-004``. Quoted in reports and in the
    #: delivery package's approval log.
    reference: Mapped[str] = mapped_column(String(40), nullable=False)

    kind: Mapped[DecisionKind] = mapped_column(default=DecisionKind.OTHER, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    #: What was decided, written to be quoted verbatim. Anything downstream that
    #: cites this decision reproduces this text unchanged.
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    #: Why. Never merged into ``statement``, so a citation shows the decision and
    #: not the argument for it.
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: ``RESTRICT`` rather than the usual ``SET NULL``/``CASCADE``: a decision
    #: with no decider is not a decision, and cascading would delete engineering
    #: history along with a user account. Users are deactivated
    #: (``users.is_active``), not deleted, so this is not a practical constraint.
    decided_by_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Denormalised on purpose, and not a caching optimisation.
    #:
    #: A decision is evidence. Its whole value is that a named person stands
    #: behind it, and that name has to still be readable years later — in an
    #: exported delivery package, in a PDF report, on a system where the ``users``
    #: table has been pruned or migrated, or after the engineer has left and
    #: their row has been removed. Resolving the name through the foreign key
    #: would make the attribution dependent on a row that outranks it in
    #: lifetime, which is exactly backwards. The FK stays for "who in this system
    #: made this"; this column is the attribution that survives.
    #:
    #: It is a snapshot: it holds the name as it stood when the decision was
    #: made, and is never rewritten when the user later changes their display
    #: name. That is the correct behaviour for a signed statement.
    decided_by_name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: When the decision was made, which is not when the row was written — a
    #: decision taken in a design review is recorded afterwards. ``created_at``
    #: from :class:`~app.database.base.TimestampMixin` records the write.
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    #: The earlier decision this one replaces. Points backwards rather than
    #: reusing the mixin's ``superseded_by_id``, so the chain is writable at the
    #: moment the new decision is made without touching the protected old row.
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("project_decisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Set when this decision is the answer to a clarification.
    question_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("project_questions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: What the decision applies to, e.g. ``{"area": "Line 3", "device_category":
    #: "motor"}``. Structured so a validator can tell whether a decision actually
    #: covers the object citing it, rather than trusting the citation.
    scope: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
