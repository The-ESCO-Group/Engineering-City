"""Tag-discipline checks: is the project's vocabulary legal, documented and used?

Codes in this module are namespaced ``tags.*`` and are published API — see the
package docstring.

The tag table is the join between the physical model and everything the
controller does about it, so a defect here is a defect in five deliverables at
once. That is why the identifier checks block: a name Logix will not accept is a
package that does not import, and a name that resolves to two different tags is a
package whose behaviour depends on which one the tool picks.

**Nothing here renames anything.** ``TagValidationResult.suggested_name`` exists
so that accepting a proposal is one deliberate human act; a validator that
corrected a name in passing would break every rung, binding, alarm and test that
already points at it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.core.enums import (
    BaseDataType,
    ObjectType,
    TagRole,
    TagScope,
    ValidationDiscipline,
    ValidationSeverity,
)
from app.models.tags import Tag, TagAlias
from app.validation.base import Issue, ValidationContext, fold, label, references

__all__ = [
    "MAX_TAG_NAME_CHARS",
    "RESERVED_TAG_NAMES",
    "TagAliasValidator",
    "TagDocumentationValidator",
    "TagNameValidator",
    "TagUsageValidator",
]

_DISCIPLINE = ValidationDiscipline.TAGS

#: The Logix limit. Forty characters is not a house style: a longer name is
#: refused by the tool at import, so this is a hard fact about the target rather
#: than a preference this platform is imposing.
MAX_TAG_NAME_CHARS = 40

#: A legal Logix identifier: letter or underscore first, then letters, digits and
#: underscores. Trailing and doubled underscores are refused separately so the
#: message can say which rule was broken.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Names a tag may not take. **Not claimed to be exhaustive**, and nothing rests
#: on it being so: Studio 5000 is the authority on what it will accept, and no
#: offline check can stand in for an import. The list holds the cases that
#: actually occur — the data-type names, the scope keywords, and the structured
#: text operators — so that a tag called ``TIMER`` or ``NOT`` is named and
#: refused here rather than at the customer's import dialog.
RESERVED_TAG_NAMES: frozenset[str] = frozenset(
    {
        # Data types. A tag may not take the name of a type.
        "bool",
        "sint",
        "int",
        "dint",
        "lint",
        "usint",
        "uint",
        "udint",
        "ulint",
        "real",
        "lreal",
        "string",
        "timer",
        "counter",
        "control",
        "message",
        "axis",
        "cam",
        "cam_profile",
        "motion_group",
        # Scope and structure keywords.
        "program",
        "controller",
        "task",
        "routine",
        "module",
        "local",
        "this",
        "aoi",
        "map",
        # Structured-text operators and keywords, which double as expression
        # syntax and so cannot be operand names.
        "and",
        "or",
        "xor",
        "not",
        "mod",
        "if",
        "then",
        "else",
        "elsif",
        "end_if",
        "case",
        "of",
        "end_case",
        "for",
        "to",
        "by",
        "do",
        "end_for",
        "while",
        "end_while",
        "repeat",
        "until",
        "end_repeat",
        "exit",
        "return",
        "true",
        "false",
    }
)

#: Roles whose value an operator reads as a physical quantity. A number on a
#: faceplate with no unit beside it is a number nobody can act on.
_ROLES_NEEDING_UNITS = frozenset({TagRole.ANALOG_PROCESS_VALUE, TagRole.SETPOINT})

#: Types that can carry an engineering unit at all. A BOOL setpoint has no unit,
#: and reporting one as missing would be inventing a requirement.
_NUMERIC_TYPES = frozenset(
    {
        BaseDataType.SINT,
        BaseDataType.INT,
        BaseDataType.DINT,
        BaseDataType.LINT,
        BaseDataType.USINT,
        BaseDataType.UINT,
        BaseDataType.UDINT,
        BaseDataType.ULINT,
        BaseDataType.REAL,
        BaseDataType.LREAL,
    }
)


def _tag_label(context: ValidationContext, tag: Tag) -> str:
    """``Controller / Motor_Run_Cmd`` or ``MainProgram / Motor_Run_Cmd``."""
    if tag.scope is TagScope.PROGRAM and tag.scope_program_id is not None:
        program = context.programs_by_id.get(tag.scope_program_id)
        scope = program.name if program is not None else "unknown program"
    else:
        scope = tag.scope.value
    return label(f"{scope} / {tag.name}")


class TagNameValidator:
    """Names Logix will refuse, and names that resolve to more than one tag."""

    key = "tags.names"
    discipline = _DISCIPLINE
    description = "Tag identifier legality, reserved words, length, duplicates and shadowing."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []

        for tag in context.tags:
            issues.extend(self._name_issues(context, tag))

        issues.extend(self._duplicate_issues(context))
        issues.extend(self._scope_collision_issues(context))
        return issues

    def _name_issues(self, context: ValidationContext, tag: Tag) -> list[Issue]:
        issues: list[Issue] = []
        name = tag.name
        location = _tag_label(context, tag)

        reason = ""
        if not _IDENTIFIER.match(name):
            reason = (
                "it must start with a letter or underscore and contain only letters, "
                "digits and underscores"
            )
        elif name.endswith("_"):
            reason = "it must not end with an underscore"
        elif "__" in name:
            reason = "it must not contain two consecutive underscores"

        if reason:
            issues.append(
                Issue(
                    code="tags.invalid_identifier",
                    discipline=_DISCIPLINE,
                    # ERROR: Studio 5000 refuses the name outright, so a package
                    # containing it does not import. Nothing downstream can
                    # recover from that, and the customer meets it as an
                    # unexplained dialog.
                    severity=ValidationSeverity.ERROR,
                    message=f"'{name}' is not a legal Logix tag name: {reason}.",
                    object_type=ObjectType.TAG,
                    object_id=tag.id,
                    object_label=location,
                    suggested_action=(
                        "Rename the tag through the rename impact path so the logic, HMI, "
                        "alarm and test references that already point at it move with it."
                    ),
                    detail={"name": name, "source_name": tag.source_name, "rule": reason},
                )
            )

        if fold(name) in RESERVED_TAG_NAMES:
            issues.append(
                Issue(
                    code="tags.reserved_word",
                    discipline=_DISCIPLINE,
                    # ERROR: same as above — the tool refuses it.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"'{name}' is a reserved Logix word and cannot be used as a tag name."
                    ),
                    object_type=ObjectType.TAG,
                    object_id=tag.id,
                    object_label=location,
                    suggested_action="Rename the tag; the reserved word cannot be escaped.",
                    detail={"name": name},
                )
            )

        if len(name) > MAX_TAG_NAME_CHARS:
            issues.append(
                Issue(
                    code="tags.name_too_long",
                    discipline=_DISCIPLINE,
                    # ERROR: a hard limit of the target platform.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"'{name}' is {len(name)} characters; Logix allows {MAX_TAG_NAME_CHARS}."
                    ),
                    object_type=ObjectType.TAG,
                    object_id=tag.id,
                    object_label=location,
                    suggested_action=(
                        "Shorten the name to 40 characters or fewer through the rename impact path."
                    ),
                    detail={"name": name, "length": len(name), "limit": MAX_TAG_NAME_CHARS},
                )
            )

        return issues

    def _duplicate_issues(self, context: ValidationContext) -> list[Issue]:
        grouped: dict[tuple[str, str, str], list[Tag]] = {}
        for tag in context.tags:
            key = (tag.scope.value, str(tag.scope_program_id or ""), fold(tag.name))
            grouped.setdefault(key, []).append(tag)

        issues: list[Issue] = []
        for (scope, _, _), tags in grouped.items():
            if len(tags) < 2:
                continue
            for tag in tags:
                issues.append(
                    Issue(
                        code="tags.duplicate_name",
                        discipline=_DISCIPLINE,
                        # ERROR: two rows claiming one Logix identity. Every
                        # operand, binding and test step naming it resolves to
                        # whichever the exporter happens to write last, so the
                        # package's behaviour is decided by row order.
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"'{tag.name}' is declared {len(tags)} times in {scope} scope. "
                            "Logix identity is (scope, name), so these are one tag with two "
                            "definitions."
                        ),
                        object_type=ObjectType.TAG,
                        object_id=tag.id,
                        object_label=_tag_label(context, tag),
                        suggested_action=(
                            "Delete or rename all but one of the declarations, after "
                            "checking which definition the logic actually depends on."
                        ),
                        detail={
                            "name": tag.name,
                            "scope": scope,
                            "duplicate_ids": [str(other.id) for other in tags],
                        },
                    )
                )
        return issues

    def _scope_collision_issues(self, context: ValidationContext) -> list[Issue]:
        controller_names = {
            fold(tag.name) for tag in context.tags if tag.scope is TagScope.CONTROLLER
        }
        issues: list[Issue] = []
        for tag in context.tags:
            if tag.scope is not TagScope.PROGRAM:
                continue
            if fold(tag.name) not in controller_names:
                continue
            issues.append(
                Issue(
                    code="tags.scope_collision",
                    discipline=_DISCIPLINE,
                    # WARNING and not ERROR: this is legal Logix and is
                    # occasionally deliberate. It is reported because the
                    # program-scoped tag shadows the controller one, so a rung,
                    # an HMI binding and a test step that all write "the same"
                    # name are writing two different tags — which no reader can
                    # see from the name alone.
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"Program-scoped '{tag.name}' shadows a controller-scoped tag of the "
                        "same name. References inside this program reach the program tag."
                    ),
                    object_type=ObjectType.TAG,
                    object_id=tag.id,
                    object_label=_tag_label(context, tag),
                    suggested_action=(
                        "Confirm the shadowing is intended; if it is not, rename one of the "
                        "two so every reference is unambiguous on its face."
                    ),
                    detail={"name": tag.name},
                )
            )
        return issues


class TagDocumentationValidator:
    """Descriptions and engineering units that were never written down."""

    key = "tags.documentation"
    discipline = _DISCIPLINE
    description = "Tags with no description, and analog values or setpoints with no units."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []

        for tag in context.tags:
            if not tag.description.strip():
                issues.append(
                    Issue(
                        code="tags.missing_description",
                        discipline=_DISCIPLINE,
                        # WARNING, deliberately. An undescribed tag imports and
                        # runs correctly; it costs the next engineer time. A
                        # gate that blocked on it would be bypassed within a
                        # week, and then it would block nothing at all.
                        severity=ValidationSeverity.WARNING,
                        message=f"'{tag.name}' has no description.",
                        object_type=ObjectType.TAG,
                        object_id=tag.id,
                        object_label=_tag_label(context, tag),
                        suggested_action=(
                            "Write one line saying what the tag is for; it is exported into "
                            "the comment CSV and read at the panel."
                        ),
                        detail={"name": tag.name, "role": tag.role.value},
                    )
                )

            needs_units = tag.role in _ROLES_NEEDING_UNITS and tag.base_data_type in _NUMERIC_TYPES
            if needs_units and not tag.engineering_units.strip():
                issues.append(
                    Issue(
                        code="tags.missing_units",
                        discipline=_DISCIPLINE,
                        # WARNING: the finding is that nobody stated the unit.
                        # No unit is inferred from the name, the range or the
                        # device — a guessed unit is an engineering statement
                        # this platform is not entitled to make, and it would
                        # look deliberate on the faceplate.
                        severity=ValidationSeverity.WARNING,
                        message=(
                            f"'{tag.name}' is a {tag.role.value} of type "
                            f"{tag.base_data_type.value} with no engineering units recorded."
                        ),
                        object_type=ObjectType.TAG,
                        object_id=tag.id,
                        object_label=_tag_label(context, tag),
                        suggested_action=(
                            "Record the units the source document states. Do not infer them "
                            "from the tag name or the raw range."
                        ),
                        detail={
                            "name": tag.name,
                            "role": tag.role.value,
                            "base_data_type": tag.base_data_type.value,
                        },
                    )
                )

        return issues


class TagAliasValidator:
    """Aliases whose target cannot be found, and aliases that contradict it."""

    key = "tags.aliases"
    discipline = _DISCIPLINE
    description = "Alias targets that do not resolve, and alias types that disagree."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []
        tags_by_id = {tag.id: tag for tag in context.tags}
        aliases_by_tag = {alias.alias_tag_id: alias for alias in context.tag_aliases}

        for tag in context.tags:
            if not tag.is_alias:
                continue
            alias = aliases_by_tag.get(tag.id)
            expression = (alias.target_expression if alias else "") or tag.alias_for

            if not expression.strip():
                issues.append(
                    Issue(
                        code="tags.alias_target_missing",
                        discipline=_DISCIPLINE,
                        # ERROR: an alias tag with nothing on the other side is
                        # not exportable — Logix requires an AliasFor — and the
                        # logic that reads it reads nothing.
                        severity=ValidationSeverity.ERROR,
                        message=(f"'{tag.name}' is declared an alias but names no target at all."),
                        object_type=ObjectType.TAG,
                        object_id=tag.id,
                        object_label=_tag_label(context, tag),
                        suggested_action=(
                            "Record the address or tag this alias points at, or clear the "
                            "alias flag if it is a base tag."
                        ),
                        detail={"name": tag.name},
                    )
                )
                continue

            if alias is None or not alias.is_resolved:
                issues.append(self._unresolved_issue(context, tag, expression, alias))
                continue

            if alias.target_tag_id is None:
                continue
            target = tags_by_id.get(alias.target_tag_id)
            if target is None:
                continue
            issues.extend(self._type_issues(context, tag, target))

        return issues

    def _unresolved_issue(
        self,
        context: ValidationContext,
        tag: Tag,
        expression: str,
        alias: TagAlias | None,
    ) -> Issue:
        note = alias.resolution_note if alias is not None else ""
        # A module address — ``Local:2:I.Data.3`` — is resolved by the controller
        # file's module configuration, not by the tag table, so its absence here
        # is not evidence of anything. Saying "we did not check this" is the
        # honest answer; reporting it as a broken alias would be a confident
        # error against correct data.
        if ":" in expression:
            return Issue(
                code="tags.unresolved_module_alias",
                discipline=_DISCIPLINE,
                severity=ValidationSeverity.UNSUPPORTED,
                message=(
                    f"'{tag.name}' aliases the module address '{expression}'. Whether that "
                    "address exists depends on the module configuration, which this "
                    "validator does not read."
                ),
                object_type=ObjectType.TAG,
                object_id=tag.id,
                object_label=_tag_label(context, tag),
                suggested_action=(
                    "Check the address against the I/O tree in the controller file before "
                    "the package is imported."
                ),
                detail={"name": tag.name, "target_expression": expression, "note": note},
            )

        return Issue(
            code="tags.stale_alias",
            discipline=_DISCIPLINE,
            # ERROR: a symbolic alias target that this project does not hold
            # will not exist at import either. The alias imports as a broken
            # reference and everything reading through it reads nothing.
            severity=ValidationSeverity.ERROR,
            message=(
                f"'{tag.name}' aliases '{expression}', which resolves to no tag in this "
                f"project.{f' {note}' if note else ''}"
            ),
            object_type=ObjectType.TAG,
            object_id=tag.id,
            object_label=_tag_label(context, tag),
            suggested_action=(
                "Point the alias at a tag that exists, or create the target tag it was "
                "written against."
            ),
            detail={"name": tag.name, "target_expression": expression, "note": note},
        )

    def _type_issues(self, context: ValidationContext, tag: Tag, target: Tag) -> list[Issue]:
        if tag.base_data_type is BaseDataType.UNKNOWN:
            # Nothing was stated about the alias's own type, so there is nothing
            # to disagree with. Inventing the target's type as the alias's would
            # make the check pass by construction.
            return []
        if tag.base_data_type is target.base_data_type and (
            tag.data_type_name.strip().lower() == target.data_type_name.strip().lower()
        ):
            return []

        return [
            Issue(
                code="tags.alias_type_mismatch",
                discipline=_DISCIPLINE,
                # ERROR: an alias takes its target's type in Logix. A
                # declaration that disagrees means one of the two rows is wrong
                # about what the data is, and the logic reading through the
                # alias is reading it as the wrong shape.
                severity=ValidationSeverity.ERROR,
                message=(
                    f"'{tag.name}' is declared as "
                    f"{_type_text(tag)} but aliases '{target.name}', which is "
                    f"{_type_text(target)}."
                ),
                object_type=ObjectType.TAG,
                object_id=tag.id,
                object_label=_tag_label(context, tag),
                suggested_action=(
                    "Correct whichever declaration is wrong; an alias cannot change the "
                    "type of what it points at."
                ),
                detail={
                    "name": tag.name,
                    "alias_type": _type_text(tag),
                    "target_name": target.name,
                    "target_type": _type_text(target),
                },
            )
        ]


class TagUsageValidator:
    """Tags nothing references, and tags this validator cannot judge."""

    key = "tags.usage"
    discipline = _DISCIPLINE
    description = "Declared tags that nothing references, and interfaces that cannot be checked."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = list(self._aoi_issues(context))

        if not context.has_ladder_logic:
            # Without ladder to look in, "nothing references this tag" is not a
            # finding — it is the absence of a search. ``Tag.usage_count`` is a
            # denormalised ranking aid and is explicitly not proof of disuse, so
            # it is not substituted here.
            issues.append(
                Issue(
                    code="tags.cross_reference_unavailable",
                    discipline=_DISCIPLINE,
                    severity=ValidationSeverity.UNSUPPORTED,
                    message=(
                        "No ladder logic is present in the model, so whether a tag is "
                        f"referenced could not be established for any of the {len(context.tags)} "
                        "tags in this project."
                    ),
                    object_label=label(context.project.name),
                    suggested_action=(
                        "Run this check again once the logic has been imported or generated."
                    ),
                    detail={"tags_examined": len(context.tags)},
                )
            )
            return issues

        referenced = _non_logic_references(context)
        for tag in context.tags:
            if context.is_referenced(tag.name):
                continue
            if any(references(name, tag.name) for name in referenced):
                continue
            issues.append(
                Issue(
                    code="tags.orphaned_tag",
                    discipline=_DISCIPLINE,
                    # INFORMATION, and deliberately not a warning. Spares,
                    # future expansion and HMI-only status words are all
                    # legitimately declared and unreferenced, so this is a list
                    # worth reading rather than a defect worth chasing. Raising
                    # it would train people to ignore the whole category.
                    severity=ValidationSeverity.INFORMATION,
                    message=(
                        f"'{tag.name}' is declared but is not referenced by any rung, HMI "
                        "binding, alarm, interlock or test step."
                    ),
                    object_type=ObjectType.TAG,
                    object_id=tag.id,
                    object_label=_tag_label(context, tag),
                    suggested_action=(
                        "Confirm the tag is a deliberate spare; delete it if it is left "
                        "over from an earlier design."
                    ),
                    detail={"name": tag.name, "role": tag.role.value},
                )
            )

        return issues

    def _aoi_issues(self, context: ValidationContext) -> list[Issue]:
        available = {
            fold(reference.name): reference
            for reference in context.aoi_references
            if reference.is_available
        }
        known = {fold(reference.name): reference for reference in context.aoi_references}

        unchecked: dict[str, list[Tag]] = {}
        for tag in context.tags:
            if tag.base_data_type is not BaseDataType.ADDON_INSTRUCTION:
                continue
            key = fold(tag.data_type_name)
            if key in available:
                continue
            unchecked.setdefault(key, []).append(tag)

        issues: list[Issue] = []
        for key, tags in unchecked.items():
            reference = known.get(key)
            note = reference.availability_note if reference is not None else ""
            issues.append(
                Issue(
                    code="tags.aoi_interface_unchecked",
                    discipline=_DISCIPLINE,
                    severity=ValidationSeverity.UNSUPPORTED,
                    message=(
                        f"{len(tags)} tag(s) are of Add-On Instruction type "
                        f"'{tags[0].data_type_name or key}', whose definition was not "
                        "supplied. Their interface could not be checked."
                        f"{f' {note}' if note else ''}"
                    ),
                    object_type=ObjectType.AOI_REFERENCE if reference else None,
                    object_id=reference.id if reference else None,
                    object_label=label(f"Add-On Instruction / {tags[0].data_type_name or key}"),
                    suggested_action=(
                        "Supply the Add-On Instruction definition, or have an engineer check "
                        "these tags against the AOI already installed on the controller."
                    ),
                    detail={
                        "add_on_instruction": tags[0].data_type_name or key,
                        "tag_names": [tag.name for tag in tags],
                        "definition_present": reference is not None,
                    },
                )
            )
        return issues


def _type_text(tag: Tag) -> str:
    if tag.data_type_name.strip():
        return f"{tag.base_data_type.value} '{tag.data_type_name}'"
    return tag.base_data_type.value


def _non_logic_references(context: ValidationContext) -> set[str]:
    """Every tag name written down outside the ladder.

    An HMI binding, an alarm trigger, an interlock schedule entry and a test step
    all reference a tag as surely as a rung does. Leaving them out would report
    an HMI-only status word as an orphan, which is a false positive against
    correct data.
    """
    names: set[str] = set()

    def add(value: str) -> None:
        cleaned = value.strip()
        if cleaned:
            names.add(cleaned)

    for binding in context.hmi_bindings:
        add(binding.tag_name)
        add(binding.expression)
    for alarm in context.alarms:
        add(alarm.trigger_tag_name)
    for fault in context.fault_definitions:
        add(fault.trigger_tag_name)
    for interlock in context.interlocks:
        add(interlock.trip_tag_name)
        add(interlock.affected_tag_name)
    for permissive in context.permissives:
        add(permissive.condition_tag_name)
    for mode in context.modes:
        add(mode.selection_tag_name)
    for state in context.states:
        add(state.state_tag_name)
    for transition in context.transitions:
        add(transition.condition_tag_name)
    for behaviour in context.reset_behaviors:
        add(behaviour.reset_tag_name)
    for step in context.test_steps:
        add(step.tag_name)
    for alias in context.tag_aliases:
        add(alias.target_expression)

    return names
