"""The physical plant: equipment hierarchy, devices, network hardware and signals.

This is the half of the canonical model that describes what exists on the floor,
as opposed to what the controller does about it. It is shaped around one
question, because that question is the one an engineer otherwise answers by
reading an I/O list next to a drawing set for a day: *is every device wired, and
is anything wired twice?*

The chain that makes it a query is
:class:`Device` → :class:`Signal` → :class:`AddressAssignment` → :class:`IOPoint`.
A device declares the signals it exposes irrespective of wiring; an address
assignment is the separate, reviewable act of binding one of those signals to a
physical terminal. Splitting them is deliberate — a signal with no assignment is
an unwired device, and that is a row that exists rather than a fact that has to
be noticed.

The uniqueness constraints below follow from the same idea, and it is worth
being explicit about the direction they point. They are chosen so that *real
errors are detectable*, not so that bad data is refused at insert time. A
duplicate I/O assignment must be **storable** in order to be **reportable**:
if the database rejected the second assignment, the parser would either lose the
customer's actual (wrong) design or would have to guess which of the two to
keep, and the defect this platform exists to surface would vanish at the point
of ingestion. So ``io_points.address`` is unique — one terminal is one row, and
a second mention of it resolves to the same point rather than silently creating
a duplicate — while nothing stops two ``address_assignments`` rows from naming
that one point. The validator finds them; the schema does not hide them.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    AddressAssignmentStatus,
    DeviceCategory,
    FailState,
    ModuleRole,
    NetworkProtocol,
    SignalDirection,
    SignalType,
)
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, JSONList
from app.models.core import EngineeringObjectMixin

# ---------------------------------------------------------------------------
# Equipment hierarchy
# ---------------------------------------------------------------------------


class Area(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A plant area — the coarsest level of the ISA-88-style equipment hierarchy.

    The hierarchy is modelled as three separate tables rather than one
    self-referencing "location" table because the levels are not
    interchangeable: a naming standard, a controller allocation and an HMI
    navigation tree all key off *which* level something sits at, and a generic
    parent pointer would make "which unit is this device in" a recursive query
    with no guarantee of returning a unit.

    Every level is optional in practice — a small project may have devices and
    nothing above them — so the parent links downstream are all nullable. An
    absent area is a documented gap, not a broken row.
    """

    __tablename__ = "areas"
    __table_args__ = (UniqueConstraint("project_id", "code", name="uq_areas_project_id_code"),)

    #: The customer's short designator, e.g. ``PKG``. Distinct from ``name``
    #: because it is what appears inside tag names and on drawings, so it has to
    #: survive round-tripping unchanged.
    code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Display and report order. Source documents have an order that carries
    #: meaning (process flow) and alphabetical sorting destroys it.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class PlantUnit(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A process unit within an area.

    Named ``PlantUnit`` rather than ``Unit`` on purpose: this codebase also
    carries unit-of-measure vocabulary (``engineering_units``, ``UnitSystem``),
    and a class called ``Unit`` sitting next to those would be read as a
    measurement unit by roughly half the people who encounter it. The table is
    ``plant_units`` for the same reason. The corresponding
    :class:`~app.core.enums.ObjectType` member is ``UNIT``, which is contract
    vocabulary and cannot be renamed.
    """

    __tablename__ = "plant_units"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_plant_units_project_id_code"),
    )

    #: Nullable: units are frequently named in an I/O list before anyone has
    #: written down which area they belong to.
    area_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("areas.id", ondelete="CASCADE"), nullable=True, index=True
    )

    code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class EquipmentModule(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A machine or skid within a unit — the level devices actually attach to.

    ``equipment_type`` is a free-form string rather than an enum because it is
    the customer's own classification ("case packer", "infeed conveyor") and
    inventing a closed vocabulary for it would force every unfamiliar machine
    into ``OTHER``, which loses the only information the field carried.
    """

    __tablename__ = "equipment_modules"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_equipment_modules_project_id_code"),
    )

    unit_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("plant_units.id", ondelete="CASCADE"), nullable=True, index=True
    )

    code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: The customer's own words for what kind of machine this is.
    equipment_type: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


# ---------------------------------------------------------------------------
# Device classification and templating
# ---------------------------------------------------------------------------


class DeviceType(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A reusable *class* of device, e.g. "3-wire reversing starter".

    Separate from :class:`Device` because the interesting properties of a class
    of device — what signals it normally has, how it is expected to fail — are
    stated once and then checked against every instance. A device whose signal
    set differs from its type's is a finding worth raising; without the type
    there is nothing to compare against.

    Separate from :class:`DeviceTemplate` because *what a device is* and *what
    logic we intend to generate for it* are different decisions made by
    different people at different times. One type may be implemented by several
    templates (a customer with two motor standards), and a type with no template
    is a perfectly valid state meaning "identified, not yet designed".
    """

    __tablename__ = "device_types"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_device_types_project_id_key"),)

    #: Stable machine-readable identifier used by templates and change sets to
    #: refer to this type without depending on its display name.
    key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[DeviceCategory] = mapped_column(
        default=DeviceCategory.OTHER, nullable=False, index=True
    )

    manufacturer: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    model_number: Mapped[str] = mapped_column(String(80), default="", nullable=False)

    #: :class:`~app.core.enums.SignalType` values this type normally exposes.
    #: The expected shape of an instance, used to report a device that is
    #: missing a signal its own class says it has.
    default_signal_types: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    #: The class-level fail state. ``UNDEFINED`` until a source states one —
    #: never guessed, because a plausible-sounding default here would propagate
    #: to every instance as an engineering decision nobody made.
    default_fail_state: Mapped[FailState] = mapped_column(
        default=FailState.UNDEFINED, nullable=False
    )

    documentation_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    templates: Mapped[list[DeviceTemplate]] = relationship(
        back_populates="device_type", cascade="all, delete-orphan", lazy="select"
    )


class DeviceTemplate(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """Binds a :class:`DeviceType` to the tags and logic pattern that implement it.

    This row is the join between "we have forty motors" and "here is what a
    motor gets". It is the reason generated logic is traceable: an object
    produced by expanding a template cites ``template_id`` and
    ``template_version`` as its basis (see
    :data:`~app.models.core.ACCEPTABLE_BASIS_KINDS`), so "why does this rung
    exist" has an answer that survives the agent run that created it.

    ``template_version`` is stored alongside the identifier rather than folded
    into it because an expansion made against v1 has to stay attributable to v1
    after the library moves to v2 — otherwise re-reading the citation would
    describe logic that was never generated.
    """

    __tablename__ = "device_templates"
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_device_templates_project_id_key"),
    )

    device_type_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("device_types.id", ondelete="CASCADE"), nullable=False, index=True
    )

    key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    #: The logic template identifier, e.g. ``esco.motor.standard.v1``. Matches
    #: the ``template_id`` recorded on an evidence link.
    template_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    template_version: Mapped[str] = mapped_column(String(20), default="", nullable=False)

    #: Signal names the template cannot expand without. A device bound to this
    #: template but missing one of these is a blocking, nameable gap rather than
    #: a generation failure discovered halfway through.
    required_signals: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    #: :class:`~app.core.enums.TagRole` values the expansion will produce. Lets
    #: the tag budget and naming review happen before any logic is generated.
    generated_tag_roles: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    #: Expansion parameters (timer presets, scaling, naming fragments).
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)

    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    device_type: Mapped[DeviceType] = relationship(back_populates="templates")


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


class Device(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One physical thing on the plant floor.

    Everything about a device that the platform will validate lives in a column
    rather than in ``attributes``, because "which devices have no fail state
    recorded" has to be a query an engineer can run before a release, not a scan
    of forty JSON blobs.

    A device carries both a ``device_type_id`` and its own ``category``,
    ``manufacturer`` and ``model_number``. That is not redundancy: the type
    states what this class of device is *supposed* to be and the instance states
    what the source document actually said. Where they disagree, the
    disagreement is the finding.
    """

    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("project_id", "tag_prefix", name="uq_devices_project_id_tag_prefix"),
    )

    equipment_module_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("equipment_modules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    device_type_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("device_types.id", ondelete="SET NULL"), nullable=True, index=True
    )
    device_template_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("device_templates.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: The stem every tag for this device is built from, e.g. ``MTR_101``
    #: yielding ``MTR_101_Run_Cmd``, ``MTR_101_Running``. This column is the join
    #: between the physical model and the tag model: the tag table holds no
    #: device foreign key, so this prefix is what makes "show me every tag for
    #: this motor" answerable, and what makes an orphaned tag detectable. Unique
    #: per project because two devices sharing a prefix would make every tag
    #: derived from it ambiguous.
    tag_prefix: Mapped[str] = mapped_column(String(60), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    category: Mapped[DeviceCategory] = mapped_column(
        default=DeviceCategory.OTHER, nullable=False, index=True
    )

    manufacturer: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    model_number: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    #: Where to find it: panel, room, drawing reference. Free text because it is
    #: transcribed from a drawing and its structure varies per customer.
    location_description: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    #: True for a device that legitimately has no I/O points — a drive or
    #: instrument that exchanges everything over the network. A device with no
    #: I/O points is otherwise a validation error, and this flag exists as an
    #: explicit declaration rather than being inferred from the absence of
    #: points, because "network device" and "someone forgot to wire it" look
    #: identical from the data. Inferring would silence exactly the finding the
    #: check is for.
    is_network_only: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: What this device does when its source is lost. ``UNDEFINED`` means no
    #: source stated it; the validator reports that, and nothing fills it in.
    fail_state: Mapped[FailState] = mapped_column(default=FailState.UNDEFINED, nullable=False)
    #: The value held or driven to when ``fail_state`` is ``GO_TO_VALUE``.
    #: A string because it may be a number, a state name or an engineering
    #: quantity with units, and the source rarely says which.
    fail_state_value: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    #: Marks the device as in scope for a safety review. Never used to gate
    #: generation on its own — see ``RestrictedDomain`` for that — but it is what
    #: makes "list everything safety-related in this project" a query.
    is_safety_related: Mapped[bool] = mapped_column(default=False, nullable=False)

    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    signals: Mapped[list[Signal]] = relationship(
        back_populates="device", cascade="all, delete-orphan", lazy="select"
    )

    @property
    def requires_io_points(self) -> bool:
        """Whether an absence of wired I/O is a defect for this device."""
        return not self.is_network_only


# ---------------------------------------------------------------------------
# Control network and I/O hardware
# ---------------------------------------------------------------------------


class NetworkNode(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A node on a control network.

    Deliberately carries no uniqueness constraint. A node is identified
    differently by every protocol — an IP address, a DeviceNet MAC ID, a
    backplane slot path — and the same designator legitimately recurs on
    different segments of a real plant. Forcing uniqueness here would reject
    correct imports, which is the opposite of the trade made everywhere else in
    this module: the constraints exist to expose defects, not to refuse data.
    """

    __tablename__ = "network_nodes"

    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    protocol: Mapped[NetworkProtocol] = mapped_column(
        default=NetworkProtocol.OTHER, nullable=False, index=True
    )
    #: IP, node number or slot path. Free-form because its meaning is defined by
    #: ``protocol``; parsing it into typed fields would need a column set per
    #: protocol and would still have to store the unrecognised ones verbatim.
    address: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    #: Upstream node, for a bridged or nested network. ``SET NULL`` rather than
    #: cascade: removing a bridge should orphan what sat behind it, visibly, not
    #: delete an entire branch of the plant's network on one delete.
    parent_node_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("network_nodes.id", ondelete="SET NULL"), nullable=True, index=True
    )

    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class HardwareModule(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One physical module: controller, I/O card, comms adapter, drive.

    Named ``HardwareModule`` rather than ``Module`` because ``Module`` is
    hopelessly generic in a Python codebase — it collides with the language's own
    central concept, and ``from app.models.plant import Module`` reads as an
    import of an import. The table is ``hardware_modules`` to match. The
    corresponding :class:`~app.core.enums.ObjectType` member is ``MODULE``,
    which is published contract and cannot be renamed.

    Revision and keying fields are stored as separate nullable integers rather
    than a parsed version string because ``electronic_keying`` decides whether a
    revision mismatch stops an import, and a comparison needs numbers.
    """

    __tablename__ = "hardware_modules"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_hardware_modules_project_id_name"),
    )

    network_node_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("network_nodes.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: The module name as the controller knows it; it is what appears at the
    #: front of every I/O address, which is why it is unique per project.
    name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    catalog_number: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    role: Mapped[ModuleRole] = mapped_column(default=ModuleRole.OTHER, nullable=False, index=True)

    #: Nullable: a module discovered through an address may have no stated slot,
    #: and slot 0 is a real slot, so zero cannot stand in for "unknown".
    slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chassis: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    major_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minor_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    electronic_keying: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    #: Declared point count, used against the number of :class:`IOPoint` rows to
    #: report a module that is over-subscribed or largely unused.
    point_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Parent module for a rack-in-rack or adapter-plus-cards arrangement.
    parent_module_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("hardware_modules.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: True for a module that an I/O assignment named but that nothing ever
    #: declared. Keeping it as a flagged row beats dropping the reference: the
    #: address is real and someone wrote it down, so the useful output is "this
    #: module is referenced but undeclared", which needs the row to exist. A
    #: dropped reference would leave an I/O point pointing at nothing and turn a
    #: precise finding into a mystery.
    is_referenced_only: Mapped[bool] = mapped_column(default=False, nullable=False)

    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    io_points: Mapped[list[IOPoint]] = relationship(back_populates="hardware_module", lazy="select")


class IOPoint(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One physical terminal.

    ``address`` is unique per project, and that constraint is doing real work:
    it is what makes a duplicate I/O assignment a *detectable* error rather than
    a silent overwrite. One terminal is one row, so a second document mentioning
    ``Local:2:I.Data.3`` resolves to the same point instead of quietly creating a
    twin — and the two signals that both claim it end up as two
    :class:`AddressAssignment` rows against one point, which is a countable
    conflict. Without this constraint the same over-subscription would look like
    two devices each correctly wired to their own terminal.

    Scaling is stored as engineering range plus raw range rather than a computed
    slope and offset so that a mis-scaled analog input can be reported in the
    terms the drawing uses, and so a partially stated range stays partial rather
    than becoming a wrong number.
    """

    __tablename__ = "io_points"
    __table_args__ = (
        UniqueConstraint("project_id", "address", name="uq_io_points_project_id_address"),
    )

    #: Nullable: an address can be read out of an I/O list before the module it
    #: belongs to has been identified. ``CASCADE`` because a point cannot outlive
    #: the module it is a terminal on once that module is genuinely removed.
    hardware_module_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("hardware_modules.id", ondelete="CASCADE"), nullable=True, index=True
    )

    #: The controller-visible address, e.g. ``Local:2:I.Data.3``.
    address: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    #: Nullable, and not derived from ``address``: channel numbering is a module
    #: property, and deriving it would mean inventing a parse for every catalog
    #: number this platform has never seen.
    channel: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Required, with no default. Neither :class:`~app.core.enums.SignalType` nor
    #: :class:`~app.core.enums.SignalDirection` has an "unknown" member, so any
    #: default here would be a fabricated engineering statement — and one that
    #: reads as deliberate afterwards. Making the writer say it means a source
    #: that does not state the type surfaces as a clarification instead.
    signal_type: Mapped[SignalType] = mapped_column(nullable=False, index=True)
    direction: Mapped[SignalDirection] = mapped_column(nullable=False)

    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    engineering_units: Mapped[str] = mapped_column(String(24), default="", nullable=False)

    #: Engineering range. Nullable because an unstated range must not be
    #: confused with a range of 0.0 to 0.0.
    range_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    range_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Raw counts the engineering range maps from.
    raw_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_max: Mapped[int | None] = mapped_column(Integer, nullable=True)

    fail_state: Mapped[FailState] = mapped_column(default=FailState.UNDEFINED, nullable=False)
    #: Declared spare. Distinguishes a terminal deliberately left free from one
    #: that should have been wired and was not.
    is_spare: Mapped[bool] = mapped_column(default=False, nullable=False)

    hardware_module: Mapped[HardwareModule | None] = relationship(back_populates="io_points")

    @property
    def is_assignable(self) -> bool:
        """Whether binding a signal to this point is expected rather than a defect."""
        return not self.is_spare


# ---------------------------------------------------------------------------
# Signals and their binding to hardware
# ---------------------------------------------------------------------------


class Signal(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A logical signal a device exposes, independent of where it is wired.

    The separation from :class:`IOPoint` is the point of this table. A motor has
    a run command and a running feedback whether or not anyone has decided which
    card they land on, so signals can be designed from a functional description
    alone and the I/O assignment becomes a later, reviewable step. It also means
    an unwired device is a signal with no assignment — a row that is missing,
    which is something a query returns, rather than an absence someone has to
    spot by comparing two documents.

    ``is_required`` is what stops that query being useless. An optional
    diagnostic signal that nobody wired is not a finding; a required feedback
    that nobody wired is.
    """

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "device_id", "name", name="uq_signals_project_id_device_id_name"
        ),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Role-shaped, not tag-shaped: ``Run_Cmd``, not ``MTR_101_Run_Cmd``. The
    #: device's ``tag_prefix`` supplies the stem, so the same template can name
    #: the same signal on every device it expands onto.
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    #: Required, with no default, for the same reason as on :class:`IOPoint`:
    #: there is no "unknown" member to fall back to, and ``VIRTUAL`` in
    #: particular decides whether an unwired signal is a defect, so guessing it
    #: would suppress findings rather than produce them.
    signal_type: Mapped[SignalType] = mapped_column(nullable=False, index=True)
    direction: Mapped[SignalDirection] = mapped_column(nullable=False)

    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    engineering_units: Mapped[str] = mapped_column(String(24), default="", nullable=False)

    #: Whether the device is incomplete without this signal. Defaults to True:
    #: a signal someone bothered to write down is assumed to matter until an
    #: engineer says otherwise, which fails towards reporting rather than
    #: towards silence.
    is_required: Mapped[bool] = mapped_column(default=True, nullable=False)
    fail_state: Mapped[FailState] = mapped_column(default=FailState.UNDEFINED, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    device: Mapped[Device] = relationship(back_populates="signals")
    assignments: Mapped[list[AddressAssignment]] = relationship(
        back_populates="signal", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def requires_terminal(self) -> bool:
        """Whether this signal is expected to reach physical or network hardware.

        A ``VIRTUAL`` signal exists only in logic, so having no address
        assignment is correct for it and must not be counted as unwired.
        """
        return self.signal_type is not SignalType.VIRTUAL

    @property
    def is_wired(self) -> bool:
        """Whether some assignment binds this signal to an actual I/O point."""
        return any(
            assignment.is_resolved and assignment.status is not AddressAssignmentStatus.CONFLICTED
            for assignment in self.assignments
        )


class AddressAssignment(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """Binds one :class:`Signal` to one :class:`IOPoint`.

    A table rather than a foreign key on ``Signal`` because the binding is an
    engineering act with its own state: it is proposed, then reviewed, then
    assigned, and it can be in conflict. Attributing it (``assigned_by_agent``,
    ``is_manual_override``) and explaining it (``conflict_reason``) needs
    somewhere to live, and none of that belongs on the signal.

    The uniqueness here is asymmetric on purpose. ``(project_id, signal_id)`` is
    unique — one signal lands in one place, and a second attempt is a
    contradiction the merge validator should reject at source. ``io_point_id``
    gets only a plain index, so two signals *can* point at one terminal. That is
    precisely the duplicate-assignment defect the validator looks for, and it
    has to be **representable** in order to be **reportable**: a unique
    constraint on the point would push the conflict back into the importer,
    where the only available outcomes are losing one of the two rows or refusing
    the customer's file. Neither tells anyone their I/O is double-booked. So the
    schema stores the contradiction and ``status`` records that we found it.
    """

    __tablename__ = "address_assignments"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "signal_id", name="uq_address_assignments_project_id_signal_id"
        ),
        # Deliberately not unique: see the class docstring. This index exists so
        # that finding every signal claiming one terminal is cheap, which is
        # what turns the duplicate check from a full scan into a lookup.
        Index("ix_address_assignments_io_point_id", "io_point_id"),
    )

    signal_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("signals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Nullable: a ``PROPOSED`` or ``RESERVED`` assignment records the intent to
    #: wire a signal before the terminal has been chosen.
    io_point_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("io_points.id", ondelete="CASCADE"), nullable=True
    )

    status: Mapped[AddressAssignmentStatus] = mapped_column(
        default=AddressAssignmentStatus.PROPOSED, nullable=False, index=True
    )
    #: Which agent proposed the binding. Empty for a human-entered assignment.
    assigned_by_agent: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    #: Why this assignment is in conflict, in the terms an engineer needs to
    #: resolve it — which other signal, which terminal, which document said so.
    conflict_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Set when a human overrode the proposed binding. Kept distinct from
    #: ``origin`` on the mixin, which records who created the row: an assignment
    #: an agent created and an engineer then corrected is both agent-originated
    #: and manually overridden, and flattening the two would lose the correction.
    is_manual_override: Mapped[bool] = mapped_column(default=False, nullable=False)

    signal: Mapped[Signal] = relationship(back_populates="assignments")

    @property
    def is_resolved(self) -> bool:
        """Whether this assignment actually names a terminal."""
        return self.io_point_id is not None
