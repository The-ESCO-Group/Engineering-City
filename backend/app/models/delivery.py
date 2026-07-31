"""Delivery-domain objects: HMI, alarms, testing, and the reports a project owes.

These tables are owned by the core engineering model, but nearly every row in
them is *written* by a partner-owned delivery plugin — the delivery agents under
``app.agents.delivery`` and the report providers under ``app.reporting``. Core
owns the schema; the plugins own the population.

They live here rather than with the plugins for one reason. A plugin that owned
its own schema could not be validated against the tag model: "this HMI binding
points at a tag that does not exist" is precisely the cross-discipline defect
this platform exists to catch, and it is only catchable when the binding and the
tag are rows in one database that one query can join. A delivery plugin keeping
its screens in its own store would push that check out to commissioning, which
is exactly where it is caught today and exactly where it costs the most.

**The one design decision that runs through the whole module**, stated here so it
is not repeated on nine columns: every reference from a delivery object to
another canonical object is stored *twice* — as the name that was actually
written, and as a nullable foreign key to the object that name resolves to.
``tag_name`` beside ``tag_id``; ``to_screen_key`` beside ``to_screen_id``. The
text is the record of what the delivery artifact says. The foreign key is filled
in only once a resolver has found a matching object, and an ``is_resolved`` flag
records which of the two happened.

A single non-null foreign key would be the tidier schema and the wrong one: a
binding to a missing tag could then not be inserted at all, so the defect would
surface either as an import failure with no row to point at, or as a binding
quietly dropped on the floor. Keeping the unresolved reference storable makes it
*reportable* — it can be counted, listed in a gap report, assigned to somebody
and closed. An unresolved reference is a finding, not an error.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AlarmSeverity
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONList
from app.models.core import EngineeringObjectMixin

# ---------------------------------------------------------------------------
# HMI
# ---------------------------------------------------------------------------


class HMIScreen(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One operator display.

    ``key`` is the stable identity used by navigation and by the HMI export; it
    is what an engineer types and what other rows point at. ``name`` and
    ``title`` are separate because the title is operator-facing text that gets
    translated and reworded, while the name is the engineering handle — folding
    them together means every wording change looks like a renamed object.

    ``parent_screen_id`` gives the containment hierarchy (a faceplate belongs to
    the detail screen that opens it) and is deliberately ``SET NULL``: losing a
    parent orphans a screen, it does not delete a display somebody built.
    """

    __tablename__ = "hmi_screens"
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_hmi_screens_project_id_key"),
        Index("ix_hmi_screens_project_id_screen_type", "project_id", "screen_type"),
    )

    key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Operator-facing caption. Translated and reworded; not an identity.
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: ``overview`` | ``detail`` | ``faceplate`` | ``alarm`` and whatever else a
    #: given HMI product calls its screen classes. A String rather than an enum:
    #: the vocabulary is per-product and would need a new member per HMI vendor.
    screen_type: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    #: Design resolution. Null means the source never stated one — not zero, and
    #: not a plausible default, since guessing a resolution silently decides how
    #: every object on the screen scales.
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    parent_screen_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("hmi_screens.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: The plant equipment this screen is *for*, when it is for one. The link
    #: that makes "every equipment module has an operator display" answerable.
    equipment_module_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("equipment_modules.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: The ESCO template this screen was expanded from, with the version pinned:
    #: without the version, "regenerate the screens" cannot tell an intentional
    #: template improvement from a difference somebody introduced by hand.
    template_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    template_version: Mapped[str] = mapped_column(String(20), default="", nullable=False)

    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    objects: Mapped[list[HMIObjectInstance]] = relationship(
        back_populates="screen", cascade="all, delete-orphan", lazy="select"
    )
    bindings: Mapped[list[HMIBinding]] = relationship(
        back_populates="screen", cascade="all, delete-orphan", lazy="select"
    )
    #: Only the outgoing edges. ``HMINavigation`` has two foreign keys into this
    #: table, so the join column is named rather than inferred.
    navigation_from: Mapped[list[HMINavigation]] = relationship(
        back_populates="from_screen",
        foreign_keys="HMINavigation.from_screen_id",
        cascade="all, delete-orphan",
        lazy="select",
    )


class HMIObjectInstance(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One graphic object placed on a screen.

    Modelled as a row rather than as a blob of screen geometry because the
    object is what a binding attaches to and what a review comment is written
    against. Position and size are recorded so a regenerated screen can be
    compared with the one an engineer laid out, but they are not the point: the
    engineering content of this table is *which object carries which tag*.
    """

    __tablename__ = "hmi_object_instances"
    __table_args__ = (
        UniqueConstraint("screen_id", "key", name="uq_hmi_object_instances_screen_id_key"),
    )

    screen_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("hmi_screens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    #: ``button`` | ``numeric_display`` | ``indicator`` … Product-specific, so a
    #: String; see the note on :attr:`HMIScreen.screen_type`.
    object_type: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    #: The library object this instance was placed from, e.g. an ESCO global
    #: object. Kept as text: the library is the partner's, not ours to model.
    global_object_reference: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    #: Layout, in the screen's own coordinate space. Null where the source did
    #: not place the object — an unplaced object is still a real object.
    x: Mapped[int | None] = mapped_column(Integer, nullable=True)
    y: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: The device this object represents, when it represents one. Makes "which
    #: devices have no operator representation at all" a query.
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    screen: Mapped[HMIScreen] = relationship(back_populates="objects")
    bindings: Mapped[list[HMIBinding]] = relationship(
        back_populates="object_instance", cascade="all, delete-orphan", lazy="select"
    )


class HMIBinding(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """Binds one property of an HMI object to one controller tag.

    This is the table the cross-discipline validator exists for. ``screen_id``
    is always set, even when ``object_instance_id`` is: a binding whose object
    was never parsed is still attributable to a screen, and an unattributable
    defect is one nobody owns.
    """

    __tablename__ = "hmi_bindings"
    __table_args__ = (
        Index("ix_hmi_bindings_project_id_is_resolved", "project_id", "is_resolved"),
        Index("ix_hmi_bindings_project_id_tag_name", "project_id", "tag_name"),
    )

    #: Nullable: a binding can be screen-level (a screen-open animation, a
    #: display-wide visibility expression) rather than attached to an object.
    object_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("hmi_object_instances.id", ondelete="CASCADE"), nullable=True, index=True
    )
    screen_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("hmi_screens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: ``Value`` | ``Visibility`` | ``Caption`` … the animated property, spelled
    #: as the HMI product spells it.
    property_name: Mapped[str] = mapped_column(String(60), default="", nullable=False)

    #: The tag name exactly as the HMI artifact writes it. Kept as text so that a
    #: binding naming a tag that does not exist still inserts, and so becomes a
    #: reportable row rather than a failed write. See the module docstring.
    tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_resolved: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: ``read`` or ``write``. A String and not an enum: the vocabulary has no
    #: member for HMI access direction, and this module adds none.
    access: Mapped[str] = mapped_column(String(16), default="read", nullable=False)
    #: A binding is not always a bare tag reference; ``Tag1 AND NOT Tag2`` is a
    #: normal animation expression. Stored verbatim, unparsed.
    expression: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    #: Why the tag could not be resolved, in words an engineer can act on:
    #: "no tag of this name", "name matches two program-scoped tags", ...
    unresolved_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    screen: Mapped[HMIScreen] = relationship(back_populates="bindings")
    object_instance: Mapped[HMIObjectInstance | None] = relationship(back_populates="bindings")

    @property
    def is_command(self) -> bool:
        """Whether this binding writes into the controller rather than reading it.

        Worth separating, because an unhandled write is a *different and more
        serious* defect than an unresolved read. An unresolved read shows itself
        on the display — the field is blank or dashed and the operator can see
        something is wrong. A write binding that no PLC logic acts on looks
        completely normal: the button depresses, the value is accepted, and
        nothing happens. That failure reaches the plant floor.
        """
        return self.access == "write"


class HMINavigation(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One navigation edge between screens.

    Held as edges rather than as a menu tree so that reachability is a query: a
    screen that was built and that nothing navigates to is invisible to the
    operator, which no amount of reading the screen list reveals.
    """

    __tablename__ = "hmi_navigation"
    __table_args__ = (
        Index("ix_hmi_navigation_project_id_is_resolved", "project_id", "is_resolved"),
    )

    from_screen_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("hmi_screens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Null until the destination key resolves to a screen this project knows.
    to_screen_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("hmi_screens.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: The destination as written. Text for the same reason as
    #: :attr:`HMIBinding.tag_name`: navigation to a screen nobody built is the
    #: defect, and it has to be storable to be reported.
    to_screen_key: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    #: What causes the navigation — a button press, a menu entry, an alarm
    #: acknowledgement. Product-specific vocabulary, so a String.
    trigger: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    is_resolved: Mapped[bool] = mapped_column(default=False, nullable=False)

    from_screen: Mapped[HMIScreen] = relationship(
        back_populates="navigation_from", foreign_keys=[from_screen_id]
    )


# ---------------------------------------------------------------------------
# Alarms
# ---------------------------------------------------------------------------


class AlarmDefinition(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One alarm the system can raise.

    The alarm is separated from its operator text (:class:`AlarmMessage`)
    because the text is translated and rewritten repeatedly while the alarm's
    engineering identity — what trips it, at what limit, how it clears — must
    stay stable across all of that.

    ``severity`` has no default on purpose. Alarm severity is the statement of
    how fast an operator must respond, which is an engineering judgement made
    against the consequence of not responding; defaulting it would write that
    judgement on somebody's behalf and it would then look like it was made.
    """

    __tablename__ = "alarm_definitions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "reference", name="uq_alarm_definitions_project_id_reference"
        ),
        Index("ix_alarm_definitions_project_id_severity", "project_id", "severity"),
        Index("ix_alarm_definitions_project_id_is_resolved", "project_id", "is_resolved"),
    )

    reference: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Identifies the message set this alarm's text is looked up under.
    message_key: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    severity: Mapped[AlarmSeverity] = mapped_column(nullable=False)
    #: The customer's or the HMI product's alarm class/group taxonomy, which is
    #: per-site vocabulary rather than ours.
    alarm_class: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    #: The tag that raises the alarm, as written; see the module docstring.
    trigger_tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    trigger_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: ``digital`` | ``level_high`` | ``level_low`` | ``deviation``. A String
    #: because the vocabulary has no alarm-trigger enum and this module adds none.
    trigger_type: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    #: Limit and deadband stay text: a source states them as "85", "85 %" or
    #: "2.5 barg", and coercing that to a float silently discards the unit,
    #: which is the part an engineer needs to check.
    limit_value: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    deadband: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    #: On-delay before annunciation. Null means unstated, which is not zero:
    #: zero delay is a decision to alarm on the first scan.
    delay_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    latched: Mapped[bool] = mapped_column(default=False, nullable=False)
    requires_acknowledge: Mapped[bool] = mapped_column(default=True, nullable=False)

    #: The fault this alarm annunciates, when the two were modelled separately.
    fault_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("fault_definitions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: True once logic has been found or generated that actually sets the
    #: trigger. An alarm defined in an alarm list that nothing in the controller
    #: ever drives is a real and common gap, and this is the flag that finds it.
    is_driven: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: True once ``trigger_tag_name`` resolved to ``trigger_tag_id``.
    is_resolved: Mapped[bool] = mapped_column(default=False, nullable=False)

    group_name: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    messages: Mapped[list[AlarmMessage]] = relationship(
        back_populates="alarm", cascade="all, delete-orphan", lazy="selectin"
    )


class AlarmMessage(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """The operator-facing text for one alarm in one language.

    A row per language rather than a JSON blob on the alarm, because each
    translation is reviewed and approved separately — and because the operator
    guidance is the field a commissioning engineer most often has to correct
    without touching the alarm's engineering definition at all.
    """

    __tablename__ = "alarm_messages"
    __table_args__ = (
        UniqueConstraint(
            "alarm_id", "language_code", name="uq_alarm_messages_alarm_id_language_code"
        ),
    )

    alarm_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("alarm_definitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: BCP 47, e.g. ``en-US``. Defaulted rather than nullable so an untranslated
    #: project still has exactly one well-formed message row per alarm.
    language_code: Mapped[str] = mapped_column(String(10), default="en-US", nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: What the operator should actually do. Separate from ``text`` because an
    #: alarm that states a condition but no response is a documented gap, and
    #: merging the two fields would hide it.
    operator_guidance: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    alarm: Mapped[AlarmDefinition] = relationship(back_populates="messages")


# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------


class TestCase(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One test in the FAT/SAT/IAT plan.

    The three optional links — requirement, interlock, device — are what make
    coverage answerable in the direction that matters: not "how many tests are
    there" but "which safety requirements have no test at all".
    """

    __tablename__ = "test_cases"
    __table_args__ = (
        UniqueConstraint("project_id", "reference", name="uq_test_cases_project_id_reference"),
        Index("ix_test_cases_project_id_test_type", "project_id", "test_type"),
    )

    reference: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    objective: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: ``fat`` | ``sat`` | ``iat`` | ``interlock`` | ``functional``. A String:
    #: the vocabulary has no test-type enum and this module adds none.
    test_type: Mapped[str] = mapped_column(String(24), default="", nullable=False)

    requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True, index=True
    )
    interlock_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("interlocks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True
    )

    preconditions: Mapped[str] = mapped_column(Text, default="", nullable=False)
    expected_result: Mapped[str] = mapped_column(Text, default="", nullable=False)
    safety_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Set when running the test means actually tripping a safety device,
    #: dropping power, or otherwise putting the plant in the state the test is
    #: about. Flagged as a field rather than left in prose because *whether this
    #: test may be run* is an engineering judgement about the state of the
    #: machine and who is standing near it — never a scheduling decision, and
    #: never something a test runner should infer from a description.
    is_destructive_test: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Planning estimate. Null means nobody estimated it; zero would claim
    #: somebody did and said it was free.
    estimated_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    #: ``draft`` | ``ready`` | ``approved`` | ``retired``. A String: the
    #: vocabulary's status enums describe requirements and findings, not tests.
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)

    steps: Mapped[list[TestStep]] = relationship(
        back_populates="test_case", cascade="all, delete-orphan", lazy="selectin"
    )


class TestStep(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One numbered step of a test case.

    ``step_number`` is the number printed on the test sheet and signed against,
    so it is unique within the case and is not the same thing as ``ordinal``,
    which is only display order. Renumbering a signed test sheet is not a
    cosmetic change, so the two are kept apart.
    """

    __tablename__ = "test_steps"
    __table_args__ = (
        UniqueConstraint(
            "test_case_id", "step_number", name="uq_test_steps_test_case_id_step_number"
        ),
    )

    test_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, default="", nullable=False)
    expected_observation: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: The tag the step manipulates or watches, as written; see the module
    #: docstring for why the name and the id both exist.
    tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: False for a setup or teardown step. Only verification points count
    #: towards coverage, so a test made entirely of preparation proves nothing
    #: and this flag is what makes that visible.
    is_verification_point: Mapped[bool] = mapped_column(default=True, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    test_case: Mapped[TestCase] = relationship(back_populates="steps")


# ---------------------------------------------------------------------------
# Reports the project owes
# ---------------------------------------------------------------------------


class ReportDefinition(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A report this project is expected to produce.

    Deliberately independent of whether a provider is installed to produce it.
    This table is the *expectation*; :class:`~app.models.report.Report` is the
    record of what was actually generated. Comparing the two is how a release
    package is checked for completeness.
    """

    __tablename__ = "report_definitions"
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_report_definitions_project_id_key"),
    )

    key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    #: Matches ``Report.report_type``, which is how an expectation is paired
    #: with the artifact that satisfied it.
    report_type: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Format keys the deliverable must be produced in, e.g. ``["pdf", "xlsx"]``.
    required_formats: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    is_required_for_release: Mapped[bool] = mapped_column(default=False, nullable=False)

    provider_available: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Why no provider can produce this report — plugin not installed, wrong HMI
    #: product, licence absent. An expected report with no provider is a *stated*
    #: gap, and a stated gap is strictly better than a release package that
    #: silently lacks a document somebody downstream was expecting to find in it.
    unavailable_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
