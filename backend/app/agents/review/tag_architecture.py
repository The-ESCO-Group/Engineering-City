"""Tag Architecture Agent.

Builds the canonical tag model — the vocabulary every other discipline binds to.
It reads the approved devices, signals, I/O assignments, modes, alarms and
documented interlocks, applies the naming standard the project was scoped under,
and proposes a change set creating :class:`~app.models.tags.Tag`,
:class:`~app.models.tags.TagAlias`, :class:`~app.models.tags.TagGroup`,
:class:`~app.models.tags.UDTDefinition` and
:class:`~app.models.tags.AOIDefinitionReference` objects.

Three things shape this agent more than anything else it does.

**A rename is the most expensive edit in the system.** The tag name is the only
thing the physical model, the logic, the HMI, the alarms and the tests have in
common, so renaming one is a change to a contract five deliverables already
reference. It is therefore never a field update: it is a ``RENAME_TAG``
operation, in a change set with ``requires_human_approval`` set, carrying the
impact. :class:`~app.agents.base.actions.ProposeChangesetPayload` refuses the
alternative outright, and this agent leans on that refusal rather than
duplicating it.

**A naming-standard violation on an existing tag is not a defect to correct.**
The customer's tags are theirs. The ESCO standard governs what this platform
*creates*; against what it was *given*, the standard produces a proposal with an
impact report and a human decides. That distinction is the difference between a
review tool and a tool that quietly rewrites somebody's project.

**``source_name`` is populated on every tag, always.** It is the name as first
encountered, and it is what makes a rename explicable six months later: with it,
"the P&ID says ``MTR_101_RUN`` and the export says ``M101_Run_Cmd``" is a
traceable transformation. Without it, it is a discrepancy nobody can close.

One stated limitation, because an omission that looks like an oversight is worse
than one that is written down: :class:`~app.core.enums.ObjectType` has no member
type, so a change operation cannot author a UDT's member list. This agent
proposes the :class:`~app.models.tags.UDTDefinition` and says so in the change
set's warnings; it does not write a ``member_count`` for members no operation
created, because that would be a claim about a type nobody can look at.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.agent import SHARED_RULES, AgentContext, BaseAgent
from app.core.enums import (
    ActionType,
    AgentKind,
    ClarificationStatus,
    ObjectApprovalState,
    RequirementStatus,
    TagScope,
)
from app.generators.plc.template_registry import Template, get_registry
from app.models.delivery import AlarmDefinition
from app.models.interlocks import Interlock, ModeDefinition, Permissive
from app.models.plant import AddressAssignment, Device, IOPoint, Signal
from app.models.profile import ProjectProfile, TargetPlatform, TemplateSet
from app.models.requirements import Clarification, Requirement
from app.models.tags import (
    AOIDefinitionReference,
    Tag,
    TagGroup,
    TagNamingRule,
    UDTDefinition,
)
from app.validation.base import fold
from app.validation.tags import MAX_TAG_NAME_CHARS, RESERVED_TAG_NAMES

#: Devices handed to the model in one turn. A tag design run covers a line, not
#: a plant, and a context holding every device in a site spends the whole budget
#: restating hardware the run is not designing tags for.
MAX_DEVICES = 200

#: Existing tags handed over. Large enough to hold a real imported controller's
#: dictionary; past this the run is told it saw a subset, which is what stops a
#: duplicate-name claim being made against a list the agent only partly read.
MAX_EXISTING_TAGS = 1500

#: Reserved words shown in the context. The full set is long, repetitive and
#: already enforced by :mod:`app.validation.tags`; the model needs enough to
#: recognise the shape of the rule, not the authoritative copy.
RESERVED_WORDS_SHOWN = 40

PROMPT = f"""{SHARED_RULES}

You are the Tag Architecture Agent.

You design the canonical tag model: the vocabulary the logic, the HMI, the
alarms, the tests and the documentation all bind to. You propose it as a single
change set. You write nothing yourself — deterministic code validates every
operation, detects conflicts, applies the human gate and merges.

WHAT YOU MAY PROPOSE
- tag, tag_alias, tag_group, udt_definition and aoi_reference objects;
- a rename of an existing tag, as a rename_tag operation and never otherwise;
- a clarification, when the information to design a tag correctly was never
  stated;
- a message, and a task completion stating what you did and what limited you.

EVERY TAG CITES A BASIS. A tag exists because a device needs it, a requirement
demands it, an approved template generates it, or a named human decided it.
State that in `basis`: a requirement_reference, a template_id such as
`esco.motor.standard@1.0.0`, a decision_reference, or an evidence anchor from
the context. A tag with no stated basis is refused, and it should be: an object
that exists for no reason nobody can review.

SET source_name ON EVERY TAG. For a tag read out of a customer document it is
the name that document used. For a tag you are creating it is the name you are
creating it under. It is what makes a later rename explicable and what lets the
generated package be cross-referenced against the customer's own drawings.

THE NAMING STANDARD APPLIES TO WHAT YOU CREATE, NOT TO WHAT YOU WERE GIVEN.
The rules in `naming_standard` govern the tags you propose. An existing tag that
breaks one of them is the customer's tag and stays as it is unless a human says
otherwise: report it, propose the rename with its impact, and let them decide.
Rewriting somebody's approved vocabulary because it does not match house style
is the single fastest way to make this platform untrustworthy.

A RENAME IS NEVER SILENT. Use rename_tag, supply from_name and to_name, set the
change set's requires_human_approval to true, and put the impact in the
operation description and in the change set summary: which rungs, HMI bindings,
alarms, interlocks and test steps reference the current name, counted from the
`references` figures in the context. The payload model refuses a rename in a
change set that claims to need no approval. Do not try to route around it — the
impact report in front of the reviewer is the entire point of the gate.

ROCKWELL IDENTIFIER RULES are facts about the target tool, not house style. A
name that breaks one of them produces a package that will not import:
- it must start with a letter or an underscore;
- it may contain only letters, digits and underscores;
- it must not end with an underscore and must not contain two in a row;
- it must be {MAX_TAG_NAME_CHARS} characters or fewer;
- it must not be a reserved word (a data type such as TIMER, a scope keyword
  such as PROGRAM, or a structured-text operator such as NOT).
Check every name you propose against all five, and check the names already in
the project against them too — an illegal name that arrived in an import is a
finding for a human, reported with its impact, not something you quietly fix.

NEVER INVENT ENGINEERING INFORMATION. If nobody stated a data type, a scope, an
engineering unit, a fail state, an alias target, an array dimension or what a
tag is for, you do not have it. Do not choose the plausible value — REAL because
it sounds analog, seconds because it is a timer, `Read/Write` because it is the
usual answer. Raise a request_clarification naming what is missing and what it
blocks. A missing unit reported is a question somebody answers in a minute; a
guessed unit is a number on a faceplate that looks deliberate and is wrong.

AN INFERRED REQUIREMENT IS NOT AN ACCEPTED ONE. Requirements in the context
carry `is_inferred` and `is_accepted`. Something this platform derived and no
human has ruled on is not a basis for creating a tag: propose nothing from it
and raise a clarification instead. Treating it as settled is how a design nobody
asked for ends up in a delivered package.

DUPLICATES AND CONFLICTS. Logix identity is (scope, name), so the same name may
legitimately exist controller-scoped and once per program — and a
program-scoped tag that shadows a controller-scoped one is legal, occasionally
deliberate, and worth reporting because no reader can see it from the name.
Report a duplicate name within one scope, a name that differs from another only
by case, and two tags that plainly denote the same signal under different names.
Report them; do not resolve them by deleting one.

WHAT THIS EVIDENCE CANNOT ESTABLISH — do not claim any of it:
- that a tag does not exist because it is absent from what you were shown. It
  may be declared in another program, another controller, or be a member of a
  UDT this context does not expand;
- that an imported tag list reflects a running controller. It describes an
  offline export at export time and nothing else;
- that a tag is unused. `references` counts only what this project holds, and
  hard-wired circuitry never appears in it;
- that a UDT is correct because its name matches an ESCO standard type. Two
  types with one name and different members have diverged, and only the member
  list settles it.

A UDT's member list cannot be authored through a change operation — there is no
member object type. Propose the udt_definition, cite the template or imported
definition its shape comes from, and say in the change set warnings that the
members still have to be established. Do not state a member_count you cannot
back with members.

RESTRICTED DOMAINS. If `restricted_domains` is not empty and the mode is not
`qualified_workflow_enabled`, this project is review-only. Describe what the tag
model would need and raise the clarification; propose nothing.

Report what you could not do. A completion that states no limitations is a
stronger claim than most runs can support.
"""


class TagArchitectureAgent(BaseAgent):
    """Designs the project's tag vocabulary and proposes it as one change set."""

    kind = AgentKind.TAG_ARCHITECTURE

    @property
    def allowed_actions(self) -> set[ActionType]:
        """Propose, ask, narrate, finish, or stop.

        Deliberately excludes ``create_finding``: a tag-model defect this agent
        notices belongs in the change set's warnings or in a clarification,
        where it stays attached to the design decision that produced it. It also
        excludes ``request_approval`` — the approval a rename needs is carried by
        the change set itself, and a second, separate route to a human gate is a
        second thing to keep correct.
        """
        return {
            ActionType.PROPOSE_CHANGESET,
            ActionType.REQUEST_CLARIFICATION,
            ActionType.POST_MESSAGE,
            ActionType.COMPLETE_TASK,
            ActionType.REPORT_BLOCKER,
        }

    def system_prompt(self, context: AgentContext) -> str:
        return PROMPT

    def build_context(self, context: AgentContext) -> dict[str, Any]:
        db = context.db
        project_id = context.project.id

        tags = _existing_tags(db, project_id)
        references = _reference_counts(db, project_id)
        device_count = _count(db, Device, project_id)

        return {
            "agent_kind": self.kind.value,
            # The revision every operation in the proposed change set must be
            # written against. Handed over rather than left to the model to
            # guess: a change set based on the wrong revision is a conflict
            # discovered at merge instead of one avoided here.
            "base_revision_number": context.project.current_revision_number,
            "profile": _profile(db, project_id),
            "target_platform": _target_platform(db, project_id),
            "identifier_rules": _identifier_rules(),
            "naming_standard": _naming_standard(db, project_id),
            "templates": _templates(db, project_id),
            "devices": _devices(db, project_id),
            "existing_tags": [_tag_view(tag, references) for tag in tags[:MAX_EXISTING_TAGS]],
            "tag_groups": _tag_groups(db, project_id),
            "udts": _udts(db, project_id),
            "aoi_references": _aoi_references(db, project_id),
            "requirements": _requirements(db, project_id),
            "open_clarifications": _open_clarifications(db, project_id),
            # Names the rest of the model already writes down. This is what makes
            # "a tag the logic needs but nobody declared" and "two names for one
            # signal" answerable without re-deriving either from a convention.
            "referenced_tag_names": _referenced_names(references, tags),
            "anchor_index": self.anchor_index(db, project_id),
            "limitations": _limitations(context, tags, device_count),
            "counts": {
                "devices": device_count,
                "signals": _count(db, Signal, project_id),
                "io_points": _count(db, IOPoint, project_id),
                "existing_tags": len(tags),
                "tag_groups": _count(db, TagGroup, project_id),
                "udts": _count(db, UDTDefinition, project_id),
                "aoi_references": _count(db, AOIDefinitionReference, project_id),
                "requirements": _count(db, Requirement, project_id),
                "naming_rules": _count(db, TagNamingRule, project_id),
            },
        }


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------
def _count(db: Session, model: type[Any], project_id: uuid.UUID) -> int:
    return len(list(db.execute(select(model.id).where(model.project_id == project_id)).scalars()))


def _identifier_rules() -> dict[str, Any]:
    """The Logix constraints, read from the validator that enforces them.

    Imported rather than restated so there is one answer to "what is a legal tag
    name". A second copy in a prompt would drift from the validator, and the
    drift would show up as a change set that passes the agent and fails the
    merge.
    """
    return {
        "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
        "max_characters": MAX_TAG_NAME_CHARS,
        "no_trailing_underscore": True,
        "no_double_underscore": True,
        "reserved_words": sorted(RESERVED_TAG_NAMES)[:RESERVED_WORDS_SHOWN],
        "reserved_words_total": len(RESERVED_TAG_NAMES),
        "note": (
            "Studio 5000 is the authority on what it will accept; this list holds "
            "the cases that occur and is not claimed to be exhaustive."
        ),
    }


def _profile(db: Session, project_id: uuid.UUID) -> dict[str, Any]:
    profile = db.execute(
        select(ProjectProfile).where(ProjectProfile.project_id == project_id)
    ).scalar_one_or_none()
    if profile is None:
        # Not an empty dict: "there is no profile" and "the profile says nothing"
        # are different situations, and only the first one means the standards
        # governing this design were never established at all.
        return {"present": False, "blocks_generation": True}

    return {
        "present": True,
        "naming_standard_version": profile.naming_standard_version,
        "locale": profile.locale,
        "units": profile.units.value,
        "release_mode": profile.release_mode.value,
        "restricted_domains": list(profile.restricted_domains),
        "restricted_domain_mode": profile.restricted_domain_mode.value,
        "is_complete_for_generation": profile.is_complete_for_generation,
        "missing_required_fields": list(profile.missing_required_fields),
        "blocks_generation": profile.blocks_generation,
    }


def _target_platform(db: Session, project_id: uuid.UUID) -> dict[str, Any]:
    platform = db.execute(
        select(TargetPlatform).where(TargetPlatform.project_id == project_id)
    ).scalar_one_or_none()
    if platform is None:
        return {"present": False}
    return {
        "present": True,
        "controller_family": platform.controller_family.value,
        "controller_catalog_number": platform.controller_catalog_number,
        "firmware_revision": platform.firmware_revision,
        "safety_classification": platform.safety_classification.value,
        "is_safety_controller": platform.is_safety_controller,
        "known_limitations": list(platform.known_limitations),
    }


def _naming_standard(db: Session, project_id: uuid.UUID) -> dict[str, Any]:
    """The rules this project was scoped under, as rows rather than as a guess.

    Only active rules: a deactivated rule is kept so that an old result citing
    its ``rule_key`` still resolves, but it is not a rule anyone is designing
    against today.
    """
    rows = list(
        db.execute(
            select(TagNamingRule)
            .where(TagNamingRule.project_id == project_id, TagNamingRule.is_active.is_(True))
            .order_by(TagNamingRule.standard_key, TagNamingRule.rule_key)
        ).scalars()
    )
    return {
        "standards": sorted({f"{row.standard_key}@{row.standard_version}" for row in rows}),
        "rules": [
            {
                "standard_key": row.standard_key,
                "standard_version": row.standard_version,
                "rule_key": row.rule_key,
                # None means "every role". There is no TagRole member meaning
                # "any", so the null carries the meaning and is passed through
                # rather than flattened into a default.
                "applies_to_role": row.applies_to_role.value if row.applies_to_role else None,
                "pattern": row.pattern,
                "example": row.example,
                "description": row.description,
                "severity": row.severity.value,
            }
            for row in rows
        ],
        "note": (
            "No rules means no naming standard was pinned for this project. Design "
            "to the identifier rules, state that the standard was not established, "
            "and raise a clarification rather than assuming a house convention."
        )
        if not rows
        else "",
    }


def _templates(db: Session, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """The approved patterns, with the tag shapes each one requires.

    A template names the *shape* of a tag — a role, a suffix and a data type —
    and the device supplies the stem. That is what makes ``template_id`` an
    honest basis for a generated tag: the reviewer can open the template and see
    the rule the tag came from.
    """
    pinned = list(
        db.execute(
            select(TemplateSet).where(
                TemplateSet.project_id == project_id, TemplateSet.is_active.is_(True)
            )
        ).scalars()
    )
    allowed = {str(item) for entry in pinned for item in entry.template_ids}

    registry = get_registry()
    templates: list[Template] = registry.latest()
    if allowed:
        # A pinned set is a decision somebody made. Offering the whole shipped
        # library alongside it would invite an expansion from a template this
        # project was never scoped for.
        templates = [
            template
            for template in templates
            if template.id in allowed or template.qualified_id() in allowed
        ]

    return [
        {
            "template_id": template.qualified_id(),
            "category": template.category.value,
            "display_name": template.display_name,
            "approval_level": template.approval_level.value,
            "restricted_domains": [domain.value for domain in template.restricted_domains],
            "supported_families": [family.value for family in template.supported_families],
            "required_tags": [
                {
                    "role": required.role.value,
                    "suffix": required.suffix,
                    "data_type": required.data_type.value,
                    "type_name": required.type_name,
                    "units": required.units,
                    "description": required.description,
                    # Named so a validator — and a reader — can tell "this tag is
                    # missing" from "this tag does not apply to this device".
                    "gated_by_parameter": required.when_parameter,
                }
                for required in template.required_tags
            ],
        }
        for template in templates
    ]


def _devices(db: Session, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """Devices, their signals, and where each signal is wired.

    The signal list is what a tag is designed from; the assignment is what makes
    an I/O alias tag legitimate. A signal with no resolved assignment gets no
    alias — an alias to an address nobody chose is a fabricated I/O assignment.
    """
    devices = list(
        db.execute(
            select(Device)
            .where(Device.project_id == project_id, Device.deleted_at.is_(None))
            .order_by(Device.tag_prefix)
        ).scalars()
    )[:MAX_DEVICES]

    signals_by_device: dict[uuid.UUID, list[Signal]] = {}
    for signal in db.execute(
        select(Signal).where(Signal.project_id == project_id).order_by(Signal.ordinal, Signal.name)
    ).scalars():
        signals_by_device.setdefault(signal.device_id, []).append(signal)

    points = {
        point.id: point
        for point in db.execute(select(IOPoint).where(IOPoint.project_id == project_id)).scalars()
    }
    assignments_by_signal: dict[uuid.UUID, list[AddressAssignment]] = {}
    for assignment in db.execute(
        select(AddressAssignment).where(AddressAssignment.project_id == project_id)
    ).scalars():
        assignments_by_signal.setdefault(assignment.signal_id, []).append(assignment)

    return [
        {
            "id": str(device.id),
            "tag_prefix": device.tag_prefix,
            "name": device.name,
            "description": device.description,
            "category": device.category.value,
            "fail_state": device.fail_state.value,
            "fail_state_value": device.fail_state_value,
            "is_network_only": device.is_network_only,
            "is_safety_related": device.is_safety_related,
            "approval_state": device.approval_state.value,
            "object_revision": device.object_revision,
            "signals": [
                _signal_view(signal, assignments_by_signal.get(signal.id, []), points)
                for signal in signals_by_device.get(device.id, [])
            ],
        }
        for device in devices
    ]


def _signal_view(
    signal: Signal,
    assignments: list[AddressAssignment],
    points: dict[uuid.UUID, IOPoint],
) -> dict[str, Any]:
    resolved = [item for item in assignments if item.io_point_id is not None]
    point = points.get(resolved[0].io_point_id) if resolved and resolved[0].io_point_id else None
    return {
        "name": signal.name,
        "signal_type": signal.signal_type.value,
        "direction": signal.direction.value,
        "description": signal.description,
        "engineering_units": signal.engineering_units,
        "is_required": signal.is_required,
        "fail_state": signal.fail_state.value,
        "requires_terminal": signal.requires_terminal,
        "io_assignment": (
            {
                "address": point.address,
                "status": resolved[0].status.value,
                "signal_type": point.signal_type.value,
                "direction": point.direction.value,
                "engineering_units": point.engineering_units,
                # Nullable throughout: an unstated range is not a range of zero,
                # and a scaled tag designed from a fabricated range is wrong in a
                # way nobody sees until the line runs.
                "range_min": point.range_min,
                "range_max": point.range_max,
                "raw_min": point.raw_min,
                "raw_max": point.raw_max,
            }
            if point is not None
            else None
        ),
    }


def _existing_tags(db: Session, project_id: uuid.UUID) -> list[Tag]:
    return list(
        db.execute(
            select(Tag)
            .where(Tag.project_id == project_id, Tag.deleted_at.is_(None))
            .order_by(Tag.name)
        ).scalars()
    )


def _tag_view(tag: Tag, references: dict[str, dict[str, int]]) -> dict[str, Any]:
    """One existing tag, with everything a rename or an edit would need.

    ``object_revision`` is here because an update or a rename cannot be proposed
    without it — the merge has nothing to detect a concurrent edit against
    otherwise. ``approval_state`` is here because an approved tag is protected
    and editing it in place is not available at any revision.
    """
    return {
        "id": str(tag.id),
        "name": tag.name,
        "source_name": tag.source_name,
        "scope": tag.scope.value,
        "scope_program_id": str(tag.scope_program_id) if tag.scope_program_id else None,
        "role": tag.role.value,
        "base_data_type": tag.base_data_type.value,
        "data_type_name": tag.data_type_name,
        "dimensions": tag.dimensions,
        "description": tag.description,
        "engineering_units": tag.engineering_units,
        "external_access": tag.external_access,
        "is_alias": tag.is_alias,
        "alias_for": tag.alias_for,
        "is_io_bound": tag.is_io_bound,
        "hmi_exposed": tag.hmi_exposed,
        "is_generated": tag.is_generated,
        "origin": tag.origin.value,
        "approval_state": tag.approval_state.value,
        "is_protected": tag.is_protected,
        "object_revision": tag.object_revision,
        # What a rename would break, counted from what this project actually
        # holds. It is the impact report's raw material; it is not a claim that
        # nothing outside this project references the name.
        "references": references.get(fold(tag.name), {}),
    }


def _reference_counts(db: Session, project_id: uuid.UUID) -> dict[str, dict[str, int]]:
    """How many objects of each kind name each tag, keyed by folded name.

    Counted rather than inferred from ``Tag.usage_count``: that column is a
    denormalised ranking aid, explicitly not proof of use, and an impact report
    built on it would understate exactly the case it exists for.
    """
    counts: dict[str, dict[str, int]] = {}

    def tally(kind: str, name: str) -> None:
        key = fold(name)
        if key:
            bucket = counts.setdefault(key, {})
            bucket[kind] = bucket.get(kind, 0) + 1

    for interlock in db.execute(
        select(Interlock).where(Interlock.project_id == project_id)
    ).scalars():
        tally("interlocks", interlock.trip_tag_name)
        tally("interlocks", interlock.affected_tag_name)
    for permissive in db.execute(
        select(Permissive).where(Permissive.project_id == project_id)
    ).scalars():
        tally("permissives", permissive.condition_tag_name)
    for mode in db.execute(
        select(ModeDefinition).where(ModeDefinition.project_id == project_id)
    ).scalars():
        tally("modes", mode.selection_tag_name)
    for alarm in db.execute(
        select(AlarmDefinition).where(AlarmDefinition.project_id == project_id)
    ).scalars():
        tally("alarms", alarm.trigger_tag_name)

    return counts


def _referenced_names(counts: dict[str, dict[str, int]], tags: list[Tag]) -> list[dict[str, Any]]:
    """Tag names the rest of the model writes down, and whether a tag exists.

    ``declared`` is a statement about this project's tag table and nothing else.
    A false here means the tag model does not hold the name yet — not that the
    signal does not exist, and not that the reference is wrong.
    """
    declared = {fold(tag.name) for tag in tags}
    return [
        {
            "name": name,
            "declared": name in declared,
            "referenced_by": dict(sorted(kinds.items())),
        }
        for name, kinds in sorted(counts.items())
    ]


def _tag_groups(db: Session, project_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        {
            "id": str(group.id),
            "key": group.key,
            "name": group.name,
            "purpose": group.purpose,
            "description": group.description,
            "member_count": group.member_count,
            "object_revision": group.object_revision,
            "approval_state": group.approval_state.value,
        }
        for group in db.execute(
            select(TagGroup)
            .where(TagGroup.project_id == project_id, TagGroup.deleted_at.is_(None))
            .order_by(TagGroup.key)
        ).scalars()
    ]


def _udts(db: Session, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """User-defined types already held, with their member lists.

    The members are shown because a UDT's identity is its member list: two types
    called ``ESCO_Motor`` with different members have diverged, and that is a
    comparison nobody can make on the name.
    """
    return [
        {
            "id": str(udt.id),
            "name": udt.name,
            "family": udt.family,
            "description": udt.description,
            "template_id": udt.template_id,
            "checksum": udt.checksum,
            "member_count": udt.member_count,
            "object_revision": udt.object_revision,
            "approval_state": udt.approval_state.value,
            "members": [
                {
                    "name": member.name,
                    "base_data_type": member.base_data_type.value,
                    "data_type_name": member.data_type_name,
                    "dimensions": member.dimensions,
                    "ordinal": member.ordinal,
                    "description": member.description,
                    "hidden": member.hidden,
                }
                for member in udt.members
            ],
        }
        for udt in db.execute(
            select(UDTDefinition)
            .where(UDTDefinition.project_id == project_id, UDTDefinition.deleted_at.is_(None))
            .order_by(UDTDefinition.name)
        ).scalars()
    ]


def _aoi_references(db: Session, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """Add-On Instructions the project uses, and whether we hold the definition.

    ``is_available`` is the load-bearing field. This platform references AOIs and
    never fabricates them, so a tag of an AOI type whose definition was not
    supplied is a tag whose interface could not be checked — which the agent must
    say rather than assume is fine.
    """
    return [
        {
            "id": str(reference.id),
            "name": reference.name,
            "revision": reference.revision,
            "description": reference.description,
            "parameter_count": reference.parameter_count,
            "parameters": list(reference.parameters),
            "is_available": reference.is_available,
            "availability_note": reference.availability_note,
            "object_revision": reference.object_revision,
        }
        for reference in db.execute(
            select(AOIDefinitionReference)
            .where(
                AOIDefinitionReference.project_id == project_id,
                AOIDefinitionReference.deleted_at.is_(None),
            )
            .order_by(AOIDefinitionReference.name)
        ).scalars()
    ]


def _requirements(db: Session, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """Requirements, each carrying whether a human has actually accepted it.

    ``is_accepted`` is computed here rather than left to the model to work out
    from a status string, because the rule that hangs off it — an inferred,
    unruled requirement is not a basis for creating anything — is too important
    to depend on a model reading an enum correctly.
    """
    return [
        {
            "reference": requirement.reference,
            "title": requirement.title,
            "statement": requirement.statement,
            "kind": requirement.kind.value,
            "priority": requirement.priority.value,
            "status": requirement.status.value,
            "is_inferred": requirement.is_inferred,
            "is_accepted": requirement.status
            in {
                RequirementStatus.ACCEPTED,
                RequirementStatus.IMPLEMENTED,
                RequirementStatus.VERIFIED,
            },
            "needs_clarification": requirement.needs_clarification,
            "confidence": requirement.confidence,
        }
        for requirement in db.execute(
            select(Requirement)
            .where(Requirement.project_id == project_id, Requirement.deleted_at.is_(None))
            .order_by(Requirement.reference)
        ).scalars()
    ]


def _open_clarifications(db: Session, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """Questions already asked, so the same one is not asked twice.

    ``blocks_generation`` is carried through because it is the difference
    between "somebody still owes us an answer" and "nothing derived from this
    may be proposed at all".
    """
    return [
        {
            "id": str(item.id),
            "question": item.question,
            "ambiguity": item.ambiguity,
            "requirement_id": str(item.requirement_id) if item.requirement_id else None,
            "object_type": item.object_type.value if item.object_type else None,
            "object_id": str(item.object_id) if item.object_id else None,
            "blocks_generation": item.blocks_generation,
            "status": item.status.value,
        }
        for item in db.execute(
            select(Clarification)
            .where(
                Clarification.project_id == project_id,
                Clarification.status == ClarificationStatus.OPEN,
            )
            .order_by(Clarification.created_at)
        ).scalars()
    ]


def _limitations(context: AgentContext, tags: list[Tag], device_count: int) -> list[str]:
    """What this turn could not see, stated in the context rather than implied.

    Truncation in particular has to be said out loud: an agent that saw 1500 of
    4000 tags and reported a duplicate-name analysis would be describing a list
    it only partly read.
    """
    limitations = [item.message for item in context.graph.limitations]

    if len(tags) > MAX_EXISTING_TAGS:
        limitations.append(
            f"Only {MAX_EXISTING_TAGS} of {len(tags)} existing tags are shown. Any "
            f"statement about duplicates, orphans or naming consistency covers the "
            f"subset shown and not the project."
        )

    if device_count > MAX_DEVICES:
        limitations.append(
            f"Only {MAX_DEVICES} of {device_count} devices are shown. The tag model "
            f"proposed from this turn covers those devices and no others."
        )

    program_scoped = sum(1 for tag in tags if tag.scope is TagScope.PROGRAM)
    if program_scoped:
        limitations.append(
            f"{program_scoped} tag(s) are program-scoped. Logix identity is "
            f"(scope, name), so a name appearing twice here may be two legitimate "
            f"tags rather than a duplicate."
        )

    approved = sum(1 for tag in tags if tag.approval_state is ObjectApprovalState.APPROVED)
    if approved:
        limitations.append(
            f"{approved} tag(s) are approved and therefore protected. They cannot be "
            f"edited in place; a change to one is a rename or a supersede, with a "
            f"fresh human approval."
        )

    return limitations
