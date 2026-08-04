"""The tag dictionary: the vocabulary every other discipline binds to.

A tag row is the join between the three halves of a project. The physical model
(device, signal, I/O point) arrives on one side, the logic, HMI, alarms, tests
and documentation hang off the other, and the tag name is the only thing they
have in common. That makes a rename the most expensive edit in the system: it
is not a string change, it is a change to a contract that five deliverables
already reference.

Three columns exist purely so that expense stays visible rather than surprising:

* ``Tag.source_name`` keeps the name as first encountered, before the ESCO
  naming standard was applied. Without it a renamed tag is unexplainable — the
  customer's drawing says one thing, the export says another, and nothing in the
  database connects the two.
* :class:`TagAlias` keeps a row even when the alias target cannot be resolved.
  An unresolvable alias is a finding to report, and a finding that was silently
  dropped at import is a finding nobody ever sees.
* ``TagValidationResult.suggested_name`` is a proposal for a human and is never
  applied by the platform. Renaming an approved tag on the strength of a regular
  expression is precisely the class of unsupervised change this product exists
  to prevent.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, event, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.core.enums import (
    BaseDataType,
    TagRole,
    TagScope,
    TagValidationCode,
    ValidationSeverity,
)
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, JSONList
from app.models.core import EngineeringObjectMixin


class Tag(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One controller, program or Add-On Instruction tag.

    The canonical vocabulary of the project. Every rung operand, HMI binding,
    alarm source and test step ultimately names a row in this table, which is
    why the table carries the physical bindings (``device_id``, ``signal_id``,
    ``io_point_id``) rather than leaving them to be re-derived from the name: a
    binding recovered by string-matching a tag name is a guess, and it stops
    being correct the first time somebody renames something.
    """

    __tablename__ = "tags"
    __table_args__ = (
        # Logix identity is (scope, name): the same name may legitimately exist
        # controller-scoped and once per program.
        #
        # Note the SQL NULL semantics, which SQLite and PostgreSQL share.
        # ``scope_program_id`` is NULL for controller-scoped tags and NULL never
        # equals NULL, so this constraint does *not* catch a duplicate
        # controller-scoped name on either backend. A test that inserts two
        # controller-scoped ``Motor_Run`` tags passes in demo mode because the
        # constraint is inert there too, not because SQLite is the permissive
        # one; do not read a green SQLite run as evidence that the constraint
        # works. The tag validator raises ``TagValidationCode.DUPLICATE_NAME``
        # for that case, which is an advisory finding rather than a write
        # barrier; the constraint is the backstop for the program-scoped half.
        UniqueConstraint(
            "project_id",
            "scope",
            "scope_program_id",
            "name",
            name="uq_tags_project_id_scope_name",
        ),
        # Every lookup is "this name, within this project" — cross-project name
        # search is not a thing anyone does, so project_id leads.
        Index("ix_tags_project_id_name", "project_id", "name"),
    )

    #: The Logix identifier, exactly as it will be written to the export.
    name: Mapped[str] = mapped_column(String(80), nullable=False)

    scope: Mapped[TagScope] = mapped_column(default=TagScope.CONTROLLER, nullable=False, index=True)
    #: Set only for program-scoped tags. SET NULL rather than CASCADE: losing the
    #: program should orphan the tag visibly, not delete engineering content.
    scope_program_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("programs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: What the tag is *for*. Drives naming rules, HMI exposure and validation,
    #: so ``OTHER`` is a real answer meaning "not yet classified", not a default
    #: dressed up as a decision.
    role: Mapped[TagRole] = mapped_column(default=TagRole.OTHER, nullable=False, index=True)

    base_data_type: Mapped[BaseDataType] = mapped_column(
        default=BaseDataType.UNKNOWN, nullable=False
    )
    #: The concrete type name when ``base_data_type`` is ``UDT`` or
    #: ``ADDON_INSTRUCTION`` — e.g. ``ESCO_Motor``. A string rather than an enum
    #: because these names are project data: they are invented per customer and
    #: per site, so no fixed vocabulary could ever enumerate them.
    data_type_name: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    #: Array dimensions as written, e.g. ``[16]`` or ``[4,8]``. Empty for a
    #: scalar. Kept verbatim rather than parsed into integers, because a
    #: dimension list we re-render is a dimension list we may have altered.
    dimensions: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    engineering_units: Mapped[str] = mapped_column(String(24), default="", nullable=False)

    #: Logix vocabulary — ``Read/Write``, ``Read Only``, ``None``. Left as a
    #: string on purpose: it is a value we round-trip into the export unchanged,
    #: and an enum here would force us to reject an export we can otherwise
    #: reproduce faithfully.
    #:
    #: Empty means the source did not state it, matching
    #: :attr:`UDTMember.external_access`. Not defaulted to ``Read/Write``: that
    #: is the most permissive setting Logix offers, it would be written verbatim
    #: onto every generated tag — interlock and permissive status bits included —
    #: by the L5X and CSV writers, and it would arrive in the customer's package
    #: indistinguishable from a value they actually chose. Round-tripping a value
    #: unchanged only means something if an unstated one stays unstated; both
    #: generators already omit the attribute when this is empty.
    external_access: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    is_constant: Mapped[bool] = mapped_column(default=False, nullable=False)

    is_alias: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: The alias target expression as written, e.g. ``Local:1:I.Data[0].3``. The
    #: resolved form, when there is one, lives in :class:`TagAlias`.
    alias_for: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    # -- Physical bindings -------------------------------------------------
    # All three are soft references: deleting a device must not silently delete
    # the tag that logic and HMI still reference.
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("signals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    io_point_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("io_points.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Whether the tag is published to the HMI. A column rather than a derived
    #: query because it is an engineering decision that outlives whatever screen
    #: happens to reference the tag today.
    hmi_exposed: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: As written. ``""``, ``"0"`` and ``"0.0"`` are three different statements
    #: about what the source actually said, so none of them is normalised away.
    initial_value: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    #: The name as first encountered, before the naming standard was applied.
    #: This is what makes a rename explicable: with it, "the P&ID says
    #: ``MTR_101_RUN`` and the export says ``M101_Run_Cmd``" is a traceable
    #: transformation; without it, it is a discrepancy nobody can close.
    source_name: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    #: False for tags read out of a customer export; True for tags this platform
    #: created. Kept alongside ``origin`` because "we generated this" and "an
    #: agent proposed this change set" are different questions.
    is_generated: Mapped[bool] = mapped_column(default=True, nullable=False)
    #: Denormalised reference count, refreshed by the cross-reference pass. A
    #: ranking convenience only — the cross-reference rows are authoritative, so
    #: never treat 0 here as proof a tag is unused.
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @property
    def is_program_scoped(self) -> bool:
        return self.scope is TagScope.PROGRAM

    @property
    def is_io_bound(self) -> bool:
        """Whether the tag is tied to something physical."""
        return bool(self.device_id or self.signal_id or self.io_point_id)

    @property
    def qualified_name(self) -> str:
        """The name as logic would reference it.

        For controller scope that is simply ``name``. For program scope the true
        reference is ``Program:<program>.<name>``, and the program's name is not
        on this row — only ``scope_program_id`` is. Loading the program here
        would mean a query per tag in every export loop, so this property
        returns the bare name and the caller is expected to resolve the program
        itself (the export writer does, once, from a program map). Treating this
        value as globally unique is a bug: check :attr:`is_program_scoped` first.
        """
        return self.name


class TagAlias(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """An alias tag and what it points at.

    Separate from ``Tag.alias_for`` because resolution is a fallible step with
    its own outcome. The tag row records what the source said; this row records
    what we managed to make of it.
    """

    __tablename__ = "tag_aliases"

    alias_tag_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Set only once the expression resolves to a tag we actually hold.
    target_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: The target as written, which may not resolve — a module was not exported,
    #: a base tag was deleted, the expression indexes past the end of an array.
    #: An unresolvable alias must survive as a row so it can be reported as a
    #: finding; dropping it at import would turn a defect in the customer's
    #: project into a silent omission in ours.
    target_expression: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    is_resolved: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Why resolution failed, in one sentence, written for the engineer who has
    #: to decide whether it matters.
    resolution_note: Mapped[str] = mapped_column(Text, default="", nullable=False)


class TagGroup(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A named set of tags assembled for one downstream purpose.

    Groups are how a delivery discipline states its own selection — "these are
    the tags the HMI exports", "these feed the alarm builder" — instead of
    re-deriving it from a naming convention every time. A convention-based
    selection breaks quietly the first time a tag is renamed; a membership row
    does not.
    """

    __tablename__ = "tag_groups"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_tag_groups_project_id_key"),)

    #: Stable machine identifier, e.g. ``hmi.motor_faceplate_tags``. Referenced
    #: by templates and exporters, so it must not track the display name.
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: What consumes the group, e.g. ``hmi_export``, ``alarm_source``. A string
    #: rather than an enum: new consumers are added by configuration, and a
    #: closed vocabulary here would make that a schema migration.
    purpose: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    #: Denormalised for listing screens; :class:`TagGroupMember` is the truth.
    member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    members: Mapped[list[TagGroupMember]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="TagGroupMember.ordinal",
        lazy="select",
    )


class TagGroupMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Membership of one tag in one group.

    A join table rather than an engineering object: membership is not something
    that gets approved or superseded on its own, the group is.
    """

    __tablename__ = "tag_group_members"
    __table_args__ = (
        UniqueConstraint("tag_group_id", "tag_id", name="uq_tag_group_members_tag_group_id_tag_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_group_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("tag_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Display and export order. Faceplate tag order is meaningful to the HMI
    #: builder, so it is stored rather than left to whatever the query returns.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    group: Mapped[TagGroup] = relationship(back_populates="members")


class DataTypeDefinition(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """Any data type the project knows about, built-in or otherwise.

    Built-in types are held as rows too, so "is this type known?" is one query
    against one table rather than a lookup that has to remember to also check a
    hard-coded list. A tag naming a type with no row here is reported as
    ``TagValidationCode.UNKNOWN_DATA_TYPE`` — which only works if the known set
    is data.
    """

    __tablename__ = "data_type_definitions"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_data_type_definitions_project_id_name"),
    )

    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    base_data_type: Mapped[BaseDataType] = mapped_column(
        default=BaseDataType.UNKNOWN, nullable=False
    )
    is_builtin: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Nullable, unlike the strings on this model: a size we were never told is
    #: genuinely absent, and 0 would be a claim about memory layout we cannot
    #: support.
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Where the definition came from, e.g. ``l5x_import``, ``template``,
    #: ``generated``. Determines how much a validator may assume about it.
    source: Mapped[str] = mapped_column(String(40), default="", nullable=False)


#: Separates the fields of one member, and one member from the next, inside the
#: string a UDT checksum is taken over. Unit/record separators rather than
#: something typeable, so no member name, type name or dimension can contain one
#: and shift a field boundary — ``("A", "B_C")`` and ``("A_B", "C")`` must not
#: canonicalise to the same bytes.
_UDT_FIELD_SEPARATOR = "\x1f"
_UDT_MEMBER_SEPARATOR = "\x1e"


def udt_checksum(members: Iterable[UDTMember]) -> str:
    """SHA-256 over a normalised member list: name, type, dimensions, order.

    This is the function :attr:`UDTDefinition.checksum` documents. Two types that
    claim to be the same standard type but hash differently have diverged, and a
    divergent ``ESCO_Motor`` between two controllers is exactly the defect that
    surfaces as a broken faceplate months later.

    What is in it, and why:

    * **name, base type, concrete type name, dimensions** — the four things that
      decide the member's meaning and its memory layout. A nested UDT member is
      identified by ``data_type_name``, so ``Motor`` and ``Valve`` at the same
      ordinal do not collide.
    * **position, not the stored ordinal.** Order is part of the type's identity
      because it determines BOOL bit packing, but the ordinal *numbers* are not:
      renumbering ``0, 1, 2`` to ``10, 20, 30`` without moving anything is not a
      divergence and must not read as one.
    * **not description, not external_access, not the ``hidden`` flag.** A
      re-worded member comment is not a change to the shape a faceplate binds
      against, and reporting it as one would train people to ignore the check.
      A Logix hidden backing member still appears here as a member with a name
      and a type, so a difference in the packed set is caught by the list itself.

    Soft-deleted members are excluded: a member with a ``deleted_at`` is not part
    of the type any more, and a checksum that still counted it would report two
    types as divergent after one of them had a member removed and re-added.
    """
    live = sorted(
        (member for member in members if member.deleted_at is None),
        key=lambda member: (member.ordinal or 0, member.name or ""),
    )
    canonical = _UDT_MEMBER_SEPARATOR.join(
        _UDT_FIELD_SEPARATOR.join(
            (
                str(position),
                (member.name or "").strip(),
                str(member.base_data_type or ""),
                (member.data_type_name or "").strip(),
                (member.dimensions or "").strip(),
            )
        )
        for position, member in enumerate(live)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class UDTDefinition(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A user-defined type and the shape it imposes on every tag of that type.

    Kept as structure rather than as an opaque blob because a UDT is the unit in
    which motor, valve and drive interfaces are standardised — the member list
    is what an HMI faceplate and a test case both bind against.
    """

    __tablename__ = "udt_definitions"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_udt_definitions_project_id_name"),
    )

    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Grouping used by the ESCO standard library, e.g. ``motor``, ``valve``.
    family: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    #: Denormalised; :class:`UDTMember` is the truth.
    member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: The library template this type was instantiated from, when it was.
    template_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    #: sha256 of the normalised member list (name, type, dimensions, order), as
    #: computed by :func:`udt_checksum`. Two types that claim to be the same
    #: standard type but hash differently have diverged, which is a comparison
    #: you cannot make on the name alone — and a divergent ``ESCO_Motor`` between
    #: two controllers is exactly the defect that surfaces as a broken faceplate
    #: months later.
    #:
    #: Maintained by :func:`_refresh_udt_checksums` on every flush that touches
    #: a type or one of its members, rather than by whichever code path happens
    #: to write the rows. A field that documents an integrity guarantee and is
    #: left at ``""`` by one writer is worse than no field: two divergent types
    #: then read as two identical empty strings, and the comparison that was
    #: supposed to make the divergence obvious silently agrees.
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    members: Mapped[list[UDTMember]] = relationship(
        back_populates="udt",
        cascade="all, delete-orphan",
        order_by="UDTMember.ordinal",
        lazy="selectin",
    )


class UDTMember(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One member of a user-defined type."""

    __tablename__ = "udt_members"
    __table_args__ = (UniqueConstraint("udt_id", "name", name="uq_udt_members_udt_id_name"),)

    udt_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("udt_definitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    base_data_type: Mapped[BaseDataType] = mapped_column(
        default=BaseDataType.UNKNOWN, nullable=False
    )
    #: Concrete type name for a nested UDT member; project data, so a string.
    data_type_name: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    dimensions: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    #: Member order is part of the type's identity — it feeds the checksum and,
    #: for BOOL members, determines bit packing.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Empty means the source did not state it, in which case the member takes
    #: the containing type's access. Not defaulted to ``Read/Write``: that would
    #: record a decision the source never made.
    external_access: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    #: Logix marks the backing member of a packed BOOL set as hidden. Kept so a
    #: member list can be round-tripped without inventing or losing members.
    hidden: Mapped[bool] = mapped_column(default=False, nullable=False)

    udt: Mapped[UDTDefinition] = relationship(back_populates="members")


def _owns(member: UDTMember, udt: UDTDefinition) -> bool:
    """Whether ``member`` will belong to ``udt`` once the flush completes.

    Both spellings are checked because both happen. The changeset engine builds
    a row as ``UDTMember(**fields)`` from column values, so it sets ``udt_id``
    and never touches the relationship; a parser or a test appending to
    ``udt.members`` sets the relationship and leaves ``udt_id`` for the flush.
    """
    if member.udt is udt:
        return True
    return udt.id is not None and member.udt_id == udt.id


def _members_after_flush(session: Session, udt: UDTDefinition) -> list[UDTMember]:
    """Every member ``udt`` will have once this flush completes.

    Deliberately not ``udt.members``. That collection is loaded once and is not
    refreshed by a flush, so after the changeset engine has written members one
    operation at a time — each with its own ``flush()`` — it is stale by however
    many members were added since it was first read. Querying inside
    ``before_flush`` is safe and current: autoflush is suppressed during a flush,
    so the query returns exactly what is already in the transaction, and the rows
    still pending are the ones in ``session.new``.
    """
    doomed = set(session.deleted)
    members: list[UDTMember] = []
    if udt.id is not None:
        members.extend(
            member
            for member in session.execute(
                select(UDTMember).where(UDTMember.udt_id == udt.id)
            ).scalars()
            if member not in doomed
        )
    known = set(members)
    members.extend(
        obj
        for obj in session.new
        if isinstance(obj, UDTMember) and obj not in known and _owns(obj, udt)
    )
    return members


@event.listens_for(Session, "before_flush")
def _refresh_udt_checksums(session: Session, flush_context: Any, instances: Any) -> None:
    """Recompute :attr:`UDTDefinition.checksum` for every type this flush touches.

    Placed here, on the model, rather than in whichever service writes the rows,
    because there is more than one such writer — the changeset engine creates
    UDTs and members generically from column values, an L5X import will create
    them from a parsed export — and a guarantee that each writer has to remember
    to uphold is a guarantee that one of them will not.

    ``before_flush`` is the documented hook for deriving column values: anything
    set here joins the flush that is about to run, so the checksum is written in
    the same transaction as the members it describes and there is no window in
    which the two disagree.
    """
    affected: list[UDTDefinition] = []
    seen: set[int] = set()
    doomed = set(session.deleted)

    def consider(udt: UDTDefinition | None) -> None:
        if udt is None or udt in doomed or id(udt) in seen:
            return
        seen.add(id(udt))
        affected.append(udt)

    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, UDTDefinition):
            consider(obj)
        elif isinstance(obj, UDTMember):
            # A pending member carries no loaded relationship, so fall back to
            # the foreign key. ``Session.get`` reads the identity map first and
            # only queries when it has to.
            parent = obj.udt
            if parent is None and obj.udt_id is not None:
                parent = session.get(UDTDefinition, obj.udt_id)
            consider(parent)

    for udt in affected:
        members = _members_after_flush(session, udt)
        digest = udt_checksum(members)
        if udt.checksum != digest:
            udt.checksum = digest


class AOIDefinitionReference(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A reference to an Add-On Instruction the project uses.

    This platform **references AOIs but never fabricates them.** That is a
    product boundary, not a backlog item: an AOI is compiled logic with its own
    revision history and its own approval, and generating one would mean
    shipping untested control code under someone else's name. So this table
    records that an AOI is *used*, what its interface looks like, and — through
    ``is_available`` — whether we were even given the definition. When we were
    not, tags of that type are still modelled, and validation says plainly that
    the interface could not be checked rather than assuming it is fine.
    """

    __tablename__ = "aoi_definition_references"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_aoi_definition_references_project_id_name"),
    )

    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    #: The AOI's own revision string, e.g. ``1.3``. Two projects using the same
    #: AOI name at different revisions is a real and common situation.
    revision: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    parameter_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Parameter descriptors as supplied: name, usage, data type, required.
    #: JSON rather than a child table because we never author these — they are
    #: read, displayed and compared as a whole, never edited member by member.
    parameters: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    #: Whether the definition is actually present in the supplied inputs.
    #: Defaults to False so an AOI we have only seen *used* is never mistaken
    #: for one we can check against.
    is_available: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Why not, when not — "referenced by MainRoutine, not present in the L5X".
    availability_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("uploaded_files.id", ondelete="SET NULL"), nullable=True
    )


class TagNamingRule(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One rule from the ESCO naming standard selected for this project.

    Rules are per-project rows, not application constants, because the standard
    is versioned and a project is validated against the version it was scoped
    under. Re-validating a two-year-old project against today's standard would
    manufacture violations in work that was correct when it was delivered.
    """

    __tablename__ = "tag_naming_rules"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "standard_key",
            "rule_key",
            name="uq_tag_naming_rules_project_id_standard_key_rule_key",
        ),
    )

    #: Which standard, e.g. ``esco.tag_naming``, and which version of it.
    standard_key: Mapped[str] = mapped_column(String(60), nullable=False)
    standard_version: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    #: Stable identifier for this rule within the standard, e.g.
    #: ``command_suffix``. Reported alongside every violation so a result can be
    #: traced back to the clause that produced it.
    rule_key: Mapped[str] = mapped_column(String(60), nullable=False)

    #: NULL means the rule applies to every role. This is the one place in the
    #: module where a NULL carries meaning, and it is deliberate: "all roles"
    #: and "this specific role" are different statements, and there is no
    #: TagRole member that means "any".
    applies_to_role: Mapped[TagRole | None] = mapped_column(nullable=True, index=True)

    #: Regular expression the tag name must match. Stored as data so the
    #: standard can be revised without a release of this application.
    pattern: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: A conforming name, shown next to a violation. Far more use to an engineer
    #: than the expression that rejected them.
    example: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: WARNING by default: a naming deviation is a house-style matter, not a
    #: reason to block a release. Promoting a rule to ERROR is a per-project
    #: decision somebody has to make explicitly.
    severity: Mapped[ValidationSeverity] = mapped_column(
        default=ValidationSeverity.WARNING, nullable=False
    )
    #: Rules are deactivated rather than deleted, so a past result that cites a
    #: rule_key still resolves to the rule that produced it.
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)


class TagValidationResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One validation outcome for one tag.

    A result row, not an engineering object: it is produced by a validation run,
    it is never approved or superseded, and it is expected to be regenerated
    wholesale on the next run.
    """

    __tablename__ = "tag_validation_results"
    __table_args__ = (
        # The review screen opens on "errors first, in this project".
        Index("ix_tag_validation_results_project_id_severity", "project_id", "severity"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The run that produced this result. SET NULL rather than CASCADE so
    #: pruning run history does not erase the findings the runs produced.
    validation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("validation_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    code: Mapped[TagValidationCode] = mapped_column(nullable=False, index=True)
    severity: Mapped[ValidationSeverity] = mapped_column(
        default=ValidationSeverity.WARNING, nullable=False
    )
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Structured supporting detail — the pattern that failed, the colliding
    #: tag's id, the data type that did not match.
    detail: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    #: The rule that produced this result, when a rule did. Matches
    #: ``TagNamingRule.rule_key``; left empty for structural codes that come
    #: from the validator itself rather than from the standard.
    rule_key: Mapped[str] = mapped_column(String(60), default="", nullable=False)

    #: A conforming name the validator believes would satisfy the rule.
    #:
    #: **Never applied automatically.** It is a proposal for a human, and it is
    #: stored next to the violation so that accepting it is one deliberate act
    #: with an author and a timestamp. Silently renaming an approved tag —
    #: breaking the logic, HMI, alarm and test references that already point at
    #: it — is one of the things this product must never do, whatever the
    #: validator's confidence in its own regular expression.
    suggested_name: Mapped[str] = mapped_column(String(80), default="", nullable=False)
