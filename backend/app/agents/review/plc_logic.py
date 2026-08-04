"""PLC Logic Agent.

Creates the controller organisation — tasks, programs, routines — and the ladder
logic underneath it, by selecting an approved ESCO control pattern and supplying
its parameters.

**It never emits XML. It writes IR.** The change set it proposes creates
:class:`~app.models.plc.ControllerTask`, :class:`~app.models.plc.Program`,
:class:`~app.models.plc.Routine`, :class:`~app.models.plc.LogicNetwork`,
:class:`~app.models.plc.LogicRung`, :class:`~app.models.plc.LogicInstruction`,
:class:`~app.models.plc.LogicOperand` and :class:`~app.models.plc.LogicBranch`
rows; L5X is generated from those rows afterwards by deterministic code in
:mod:`app.generators.rockwell.l5x`. That ordering is the reason this whole layer
exists (ADR 0001 decision 3): the same approved revision then produces
byte-identical artifacts, a generated rung can answer "which requirement is this
from", and no model sits anywhere in the emission path. A model that wrote XML
would be producing markup whose only adjudicator is Studio 5000.

What the *agent* proposes is narrower than what the change set creates, and
deliberately so. The agent names a template and its parameters on a
``LogicNetwork``; deterministic expansion turns that into the rungs,
instructions, operands and branches beneath it. An agent that wrote rungs
freehand would be producing logic whose *pattern* nobody reviewed — and the
platform would have nothing to tell an engineer except that a model thought it
looked right. Selecting from ``templates/`` means the review already happened,
once, against a versioned and checksummed file.

The contract between the two halves is small enough to state here:

* ``LogicNetwork.template_id`` / ``template_version`` are columns, so the
  selection is queryable rather than buried in a blob;
* the supplied parameters and any operand the agent could not resolve travel in
  the operation's ``attributes`` under :data:`EXPANSION_KEY`, because there is no
  column for them and inventing one per template would make the schema a
  function of the pattern library;
* :func:`check_selection` is the deterministic gate the expansion runs first, and
  :func:`render_ladder_preview` is what a reviewer actually reads.

Three refusals are enforced here rather than requested in the prompt.
:func:`generation_block` decides whether this project may generate at all and, if
not, strips every template and every instruction out of the context — an agent
with no template in front of it cannot select one, and every rung it could
propose would have no template rule to cite. :func:`check_selection` refuses a
mnemonic the target family cannot run or that this platform will not emit at all.
And an operand the agent could not bind is carried as a stated reason or not at
all: :class:`~app.core.enums.OperandKind.UNRESOLVED` with an empty
``unresolved_reason`` tells an engineer something is wrong and gives them nothing
to do about it.
"""

from __future__ import annotations

import re
import textwrap
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.actions import ChangeOperationProposal
from app.agents.base.agent import SHARED_RULES, AgentContext, BaseAgent
from app.core.config import Settings
from app.core.enums import (
    ActionType,
    AgentKind,
    ClarificationStatus,
    ControllerFamily,
    QuestionCategory,
    RequirementStatus,
)
from app.generators.plc.instructions import (
    INSTRUCTIONS,
    LOGIX_FAMILIES,
    RESTRICTED_INSTRUCTIONS,
    VARIADIC_OPERAND_LIMIT,
    is_known,
    is_restricted,
    restriction_reason,
    supports,
)
from app.generators.plc.template_registry import (
    RungElement,
    Template,
    TemplateRegistry,
    get_registry,
    load_templates,
)
from app.models.plc import ControllerTask, LogicNetwork, Program, Routine
from app.models.profile import ProjectDecision, ProjectProfile, TargetPlatform
from app.models.requirements import Clarification, Requirement
from app.models.tags import Tag

__all__ = [
    "EXPANSION_KEY",
    "MAX_REQUIREMENTS",
    "MAX_TAGS",
    "PROMPT",
    "GenerationBlock",
    "PLCLogicAgent",
    "TemplateSelection",
    "UnresolvedOperand",
    "check_selection",
    "generation_block",
    "render_ladder_preview",
    "selection_of",
    "unresolved_operand_reports",
]

#: Key inside a ``LogicNetwork`` operation's ``attributes`` holding the template
#: parameters and the operands the agent could not resolve. ``attributes`` is the
#: right home for exactly this: the parameter *set* is a function of which
#: pattern was chosen, so a column per parameter would make the schema depend on
#: the template library. Everything queried or validated — the template id, its
#: version, the requirement reference — is a real column.
EXPANSION_KEY: Final = "template_expansion"

#: Tags handed to the model. Enough to bind a line's worth of equipment without
#: spending the budget listing a controller's entire namespace.
MAX_TAGS: Final = 800

#: Requirements handed to the model, newest last so a truncation drops the tail
#: rather than a silently chosen middle.
MAX_REQUIREMENTS: Final = 300

#: Requirement statuses a generated object may cite. ``PROPOSED`` is absent on
#: purpose: a proposed requirement is something nobody has ruled on yet, and
#: building logic on one promotes a guess into the deliverable.
CITABLE_REQUIREMENT_STATUSES: Final[frozenset[RequirementStatus]] = frozenset(
    {
        RequirementStatus.ACCEPTED,
        RequirementStatus.IMPLEMENTED,
        RequirementStatus.VERIFIED,
    }
)

#: ``{parameter}`` in a template's operand or title strings. Matched rather than
#: passed to ``str.format`` so that an unsupplied parameter renders as itself and
#: is visible as a hole in the preview, instead of raising or — far worse —
#: silently rendering as a plausible value.
_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")

#: Preview line width. Comments are wrapped; rung text never is — a rung broken
#: across lines stops being the single neutral rendering a reader can compare
#: against the export.
_PREVIEW_WIDTH: Final = 92


PROMPT = f"""{SHARED_RULES}

You are the PLC Logic Agent.

You create the controller organisation — tasks, programs, routines — and the
logic that hangs off it. You do it by choosing an approved ESCO control pattern
and supplying its parameters. You do not write ladder freehand: a rung you
composed yourself is a pattern nobody reviewed, and the platform would have
nothing to tell an engineer about it except that a model produced it.

HOW YOU WORK

1. Read the accepted requirements and the equipment they describe.
2. For each piece of equipment, choose ONE template from `templates` in your
   context and supply its parameters. Do not adapt a template that nearly fits,
   and never name a template id that is not in your context — an id you did not
   see does not exist.
3. Propose one change set: the controller task, the programs, the routines, and
   one logic network per block. Each network names its template, its version and
   its parameters. Deterministic code expands that into rungs, instructions,
   operands and branches. You do not write those rows.
4. Propose a `generate_preview` action of kind `ladder_text`, so a human reads
   the result as ladder rather than as forty change operations.
5. Finish with `complete_task`, stating in `limitations` what you could not do.

WHAT YOU NEVER DO

- Emit XML. You write the intermediate representation; L5X is generated from it
  by deterministic code. A rung is a structure, never a string of markup.
- Claim anything about runtime. Generated logic has never been compiled,
  downloaded, scanned or tested. It is a proposal. You know nothing about the
  controller's firmware behaviour, and an offline artifact cannot establish any.
- Invent a threshold, a timer preset, a fail state or a reset behaviour. If a
  value was never stated, use request_clarification. A plausible default inside
  a generated rung is indistinguishable from an engineered one, and that is the
  single worst thing this product can do.
- Rename an approved tag, change an I/O assignment, or alter approved logic.
  Propose the replacement and say what it supersedes; a human decides.
- Propose an instruction absent from `instruction_set.supported`, or any
  instruction listed in `instruction_set.refused`.
- Guess an operand. Where a template needs a signal this project has no tag for,
  record it in `unresolved_operands` with the reason it is unresolved. An
  unresolved operand with no stated reason costs review time and buys nothing:
  "no tag of that name in controller scope" and "the parameter naming this
  signal was never supplied" lead to completely different next steps.

EVERY CREATED OBJECT CITES A BASIS

Each operation carries `basis`, naming the requirement, the approved template
rule (`template_id`, e.g. "esco.motor.standard@1.0.0"), the recorded human
decision, or the document anchor it comes from. The platform refuses an object
that exists for no stated reason, and rightly: it cannot be reviewed.

Cite only what your context contains. `requirements` is the citable set.
`requirements_awaiting_decision` is not — those are unaccepted or inferred, and
building logic on one would promote something nobody approved into the delivered
program.

SAFETY, AND WHEN TO STOP

Set `is_safety_related` on any logic network whose pattern implements a
protective function, and set `requires_human_approval` with it. The expansion
stamps both onto every rung it creates. Neither says the logic is wrong; both say
a human must read it before it goes anywhere, and a safety-related network that
claims to need no approval is refused.

If `generation_blocked` is present in your context you generate nothing at all.
Propose no change set. Reply with request_clarification carrying the stated
question and category, and report_blocker naming the reason. Producing a
plausible artifact for a project that is not permitted to have one is worse than
producing nothing, because it looks finished.
"""


# ---------------------------------------------------------------------------
# Whether this project may generate at all
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GenerationBlock:
    """Why this project may not generate logic, and what to ask instead.

    Carries the question as well as the reason because a refusal an engineer
    cannot act on is indistinguishable from a broken tool. The question is
    written to be put to a human unedited.
    """

    reason: str
    question: str
    category: QuestionCategory
    #: Restricted domains that are in scope without a qualified workflow, if any.
    restricted_domains: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "question": self.question,
            "question_category": self.category.value,
            "restricted_domains": list(self.restricted_domains),
        }


def generation_block(
    profile: ProjectProfile | None,
    platform: TargetPlatform | None,
    settings: Settings,
) -> GenerationBlock | None:
    """Whether logic generation is permitted for this project, and why not.

    Deterministic, and read before the model sees anything: when this returns a
    block, :meth:`PLCLogicAgent.build_context` hands over no templates and no
    instruction set, so there is nothing for a model to select from even if a
    prompt injection persuaded it to try.

    The checks are ordered most-specific first, because the message is the
    product here — "motion is in scope and no qualified motion workflow is
    enabled" is actionable and "the profile blocks generation" is not.
    """
    if profile is None:
        return GenerationBlock(
            reason=(
                "This project has no engineering profile, so the naming standard, "
                "template set and standards that would govern generated logic are "
                "not established."
            ),
            question=(
                "Which ESCO naming standard version, template set and engineering "
                "standard set apply to this project?"
            ),
            category=QuestionCategory.SCOPE,
        )

    if platform is None:
        return GenerationBlock(
            reason=(
                "No target platform is recorded, so there is no controller family to "
                "check the instruction set against. An L5X is only correct relative "
                "to a family and a firmware revision."
            ),
            question=(
                "Which controller family, catalogue number and firmware revision is "
                "this project targeting?"
            ),
            category=QuestionCategory.TARGET_PLATFORM,
        )

    # Every mnemonic in the catalogue answers ``supports() == False`` outside the
    # Logix families, so there is no instruction this platform could honestly
    # claim the target can run. Micro800 is the live case: Connected Components
    # Workbench is a different instruction set, not a subset of this one.
    if platform.controller_family not in LOGIX_FAMILIES:
        return GenerationBlock(
            reason=(
                f"The target controller family is "
                f"{platform.controller_family.value!r}, whose ladder instruction set "
                "this platform does not describe. Nothing generated for it could be "
                "checked against an instruction catalogue."
            ),
            question=(
                "Is this project targeting a Logix family (CompactLogix, "
                "ControlLogix, GuardLogix), or should logic generation be out of "
                "scope for it?"
            ),
            category=QuestionCategory.TARGET_PLATFORM,
        )

    # An unrecognised domain name blocks by construction: ``Settings`` validates
    # that every enabled name is a real RestrictedDomain, so a typo in the
    # profile can never be found in the enabled list.
    enabled = set(settings.enabled_restricted_domains)
    in_scope = {str(value) for value in profile.restricted_domains if str(value)}
    if platform.is_safety_controller:
        # The family and the classification both decide this, and either alone is
        # enough: a GuardLogix is a safety controller whatever the profile says.
        in_scope.add("safety_logic")
    outstanding = tuple(sorted(in_scope - enabled))
    if outstanding:
        named = ", ".join(outstanding)
        return GenerationBlock(
            reason=(
                f"This project is in a restricted domain ({named}) for which no "
                "administrator has enabled a qualified workflow. Reviewing generated "
                "logic in these domains is a formal qualification rather than general "
                "controls experience, so the platform reads and reports here and "
                "generates nothing."
            ),
            question=(
                f"Should {named} stay in scope for this project — in which case an "
                "administrator must enable the qualified workflow and name the "
                "qualified reviewer — or should the affected equipment be excluded "
                "from generation?"
            ),
            category=QuestionCategory.RESTRICTED_DOMAIN,
            restricted_domains=outstanding,
        )

    if profile.blocks_generation:
        # The profile's own gate. Reached only after the checks above, so the
        # remaining causes are the release mode and an incomplete profile — and
        # naming the missing fields is what turns this into something answerable.
        missing = [str(name) for name in profile.missing_required_fields]
        detail = (
            f" The profile is missing: {', '.join(sorted(missing))}."
            if missing
            else f" The project's release mode is {profile.release_mode.value!r}."
        )
        return GenerationBlock(
            reason=f"The project profile does not permit generation.{detail}",
            question=(
                "Which of the outstanding profile fields apply to this project, and "
                "what are their values?"
                if missing
                else "Should this project's scope be widened beyond review?"
            ),
            category=QuestionCategory.SCOPE,
        )

    return None


# ---------------------------------------------------------------------------
# The template selection an operation carries
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class UnresolvedOperand:
    """An operand the agent could not bind to a project tag, and why.

    Both fields are required in spirit. The reason is what an engineer acts on;
    without it the report is only "something is wrong somewhere in this rung",
    which is worse than saying nothing because it still costs a review.
    """

    operand: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"operand": self.operand, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class TemplateSelection:
    """One approved pattern, chosen and parameterised.

    This is the whole of what a model contributes to a rung's content. Everything
    else about the rung — its instructions, their operands, the branch topology —
    comes from the template, which was reviewed once and is checksummed.
    """

    template_id: str
    version: str
    parameters: Mapping[str, Any]
    unresolved_operands: tuple[UnresolvedOperand, ...] = ()
    #: Stamped by the expansion onto ``LogicRung.is_safety_related`` and
    #: ``LogicRung.requires_human_approval`` for every rung it creates. They are
    #: declared on the network rather than on the rungs because the network is
    #: what a model chooses; the rungs are what the template produces.
    is_safety_related: bool = False
    requires_human_approval: bool = False

    @property
    def qualified_id(self) -> str:
        return f"{self.template_id}@{self.version}" if self.version else self.template_id


def selection_of(operation: ChangeOperationProposal) -> TemplateSelection | None:
    """The template selection a proposed logic-network operation carries.

    ``None`` when the operation names no template, which is a legitimate state:
    a change set also creates tasks, programs and routines, and none of those is
    a pattern expansion.
    """
    values = operation.values
    template_id = str(values.get("template_id") or "").strip()
    if not template_id:
        return None

    attributes = values.get("attributes")
    expansion = attributes.get(EXPANSION_KEY) if isinstance(attributes, Mapping) else None
    body: Mapping[str, Any] = expansion if isinstance(expansion, Mapping) else {}

    raw_parameters = body.get("parameters")
    parameters: Mapping[str, Any] = raw_parameters if isinstance(raw_parameters, Mapping) else {}

    unresolved: list[UnresolvedOperand] = []
    for entry in body.get("unresolved_operands") or []:
        if not isinstance(entry, Mapping):
            continue
        unresolved.append(
            UnresolvedOperand(
                operand=str(entry.get("operand") or "").strip(),
                reason=str(entry.get("reason") or "").strip(),
            )
        )

    return TemplateSelection(
        template_id=template_id,
        version=str(values.get("template_version") or "").strip(),
        parameters=parameters,
        unresolved_operands=tuple(unresolved),
        is_safety_related=bool(body.get("is_safety_related")),
        requires_human_approval=bool(body.get("requires_human_approval")),
    )


def check_selection(
    selection: TemplateSelection,
    *,
    registry: TemplateRegistry,
    family: ControllerFamily,
    enabled_domains: Collection[str] = (),
) -> list[str]:
    """Everything wrong with a selection, in words a human can act on.

    Empty means the selection may be expanded. Run before anything is written,
    so a bad selection is a validation message naming the template rather than a
    half-expanded routine.

    The instruction check is not redundant with the template being approved. A
    template's ``supported_families`` is a claim about the *pattern*; whether
    each mnemonic in it runs on this particular target is a claim about the
    instruction set, and the two are decided by different people at different
    times. Only the applicable rungs are examined — a rung gated off by a
    parameter contributes no instruction to this expansion.
    """
    problems: list[str] = []

    template = registry.get(selection.template_id, selection.version or None)
    if template is None:
        known = registry.versions(selection.template_id)
        detail = (
            f"; loaded versions of {selection.template_id!r}: {', '.join(known)}" if known else ""
        )
        problems.append(
            f"no approved template {selection.qualified_id!r} is loaded{detail}. "
            "A template that is not in the registry has not been reviewed, so "
            "nothing may be expanded from it."
        )
        return problems

    if not template.supports(family):
        supported = ", ".join(sorted(item.value for item in template.supported_families))
        problems.append(
            f"template {template.qualified_id()} is approved for {supported}, not for "
            f"{family.value}"
        )

    outstanding = [
        domain.value
        for domain in template.restricted_domains
        if domain.value not in enabled_domains
    ]
    if outstanding:
        problems.append(
            f"template {template.qualified_id()} covers restricted domain(s) "
            f"{', '.join(sorted(outstanding))}, for which no qualified workflow is enabled"
        )

    problems.extend(template.validate_parameters(selection.parameters))

    for mnemonic in _mnemonics_of(template, selection.parameters):
        if is_restricted(mnemonic):
            problems.append(
                f"template {template.qualified_id()} uses {mnemonic}, which this "
                f"platform never generates: {restriction_reason(mnemonic)}"
            )
        elif not is_known(mnemonic):
            # Unknown and unsupported end in the same place — nothing is
            # expanded — but only one of them is a statement about the target,
            # and an engineer reading "not supported on ControlLogix" about an
            # instruction nobody catalogued would go looking in the wrong place.
            problems.append(
                f"{mnemonic} is not in this platform's instruction catalogue, so "
                f"nothing here can claim {family.value} runs it"
            )
        elif not supports(mnemonic, family):
            problems.append(
                f"the catalogue does not list {family.value} among the families that run {mnemonic}"
            )

    for item in selection.unresolved_operands:
        if not item.reason:
            problems.append(
                f"operand {item.operand!r} is reported unresolved with no stated "
                "reason; an unresolved operand nobody can act on is worse than none"
            )

    # The same gate the runner applies to a safety-related recommendation, at the
    # only other place logic can reach a human without one. A network that
    # implements a protective function and claims to need no approval is asking
    # to skip the single step that makes generated safety logic reviewable.
    if selection.is_safety_related and not selection.requires_human_approval:
        problems.append(
            "the selection is marked safety-related but does not require human "
            "approval; protective logic cannot be self-approved"
        )
    if template.needs_senior_review and not selection.requires_human_approval:
        problems.append(
            f"template {template.qualified_id()} is approved at "
            f"{template.approval_level.value}, so an expansion of it must require "
            "human approval"
        )

    return problems


def _mnemonics_of(template: Template, parameters: Mapping[str, Any]) -> list[str]:
    """Every distinct mnemonic the applicable rungs of ``template`` would emit."""
    seen: dict[str, None] = {}
    for rung in template.rungs_for(parameters):
        for element in rung.iter_instructions():
            seen.setdefault(element.instruction, None)
    return list(seen)


# ---------------------------------------------------------------------------
# The preview a reviewer actually reads
# ---------------------------------------------------------------------------
def render_ladder_preview(
    template: Template,
    parameters: Mapping[str, Any],
    *,
    network_name: str = "",
    start_ordinal: int = 0,
) -> str:
    """Render what an expansion of ``template`` would produce, as ladder.

    A reviewer approving generated logic has to read it as logic. Forty change
    operations describing instructions and operand positions are technically the
    same information and are not reviewable by anybody, so this exists.

    Derived from the template and the supplied parameters rather than written by
    a model, for the same reason the IR is the source of truth: a preview a model
    composed would be a second, undiffed account of the change set, and the one
    thing a preview must not be able to do is disagree with what will be built.

    The rung rendering matches
    :func:`~app.generators.rockwell.l5x.render_rung_text` — instructions in
    series, branches as ``[leg,leg]``, a trailing semicolon — so the preview and
    the eventual export read identically. It is computed from the template tree
    rather than from rows, which do not exist yet.

    A parameter that was not supplied and has no default renders as its own
    ``{placeholder}``. That is deliberate: a hole an engineer can see is a
    question, and a plausible substituted value is a fabrication.
    """
    lines: list[str] = []
    if network_name:
        lines.append(f"Network: {network_name}")
    lines.append(f"Template: {template.qualified_id()}  (sha256 {template.checksum[:12]})")

    ordinal = start_ordinal
    routine = ""
    for rung in template.rungs_for(parameters):
        if rung.routine != routine:
            routine = rung.routine
            lines.append("")
            lines.append(f"Routine: {routine}")
        title = _substitute(rung.title, parameters, template)
        lines.append("")
        lines.append(f"  {ordinal:>3}  {title}")
        for comment_line in _wrap(_substitute(rung.comment, parameters, template)):
            lines.append(f"       ; {comment_line}")
        body = "".join(_render_element(element, parameters, template) for element in rung.elements)
        lines.append(f"       {body};")
        ordinal += 1

    if ordinal == start_ordinal:
        # An expansion that produces nothing is a real outcome — every rung in the
        # template is gated off by the supplied parameters — and it must read as
        # that rather than as an empty preview somebody assumes failed to render.
        lines.append("")
        lines.append("  (no rungs: every rung in this template is gated off by these parameters)")

    return "\n".join(lines)


def _render_element(element: RungElement, parameters: Mapping[str, Any], template: Template) -> str:
    if element.is_branch:
        legs = [
            "".join(_render_element(item, parameters, template) for item in leg)
            for leg in element.branch
        ]
        return f"[{','.join(legs)}]"
    operands = ",".join(_substitute(operand, parameters, template) for operand in element.operands)
    return f"{element.instruction}({operands})"


def _substitute(text: str, parameters: Mapping[str, Any], template: Template) -> str:
    """Fill ``{parameter}`` placeholders, leaving unsupplied ones visible.

    Values are rendered through the template's own rule rather than with
    ``str()``. A ``type: number`` parameter accepts both ``100`` and ``100.0``,
    and ``str()`` would carry that distinction into the operand text — so the
    same engineering value would produce two different rungs, two different L5X
    byte streams and two different package checksums, and a browser client
    cannot even express the second (``JSON.stringify(100.0)`` is ``"100"``).
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in parameters:
            return template.render_parameter(name, parameters[name])
        declared = template.parameter(name)
        if declared is not None and declared.default is not None:
            return declared.render(declared.default)
        return match.group(0)

    return _PLACEHOLDER_RE.sub(replace, text)


def _wrap(text: str) -> list[str]:
    collapsed = " ".join(text.split())
    return textwrap.wrap(collapsed, width=_PREVIEW_WIDTH) if collapsed else []


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------
class PLCLogicAgent(BaseAgent):
    kind = AgentKind.PLC_LOGIC

    @property
    def allowed_actions(self) -> set[ActionType]:
        return {
            ActionType.PROPOSE_CHANGESET,
            ActionType.REQUEST_CLARIFICATION,
            ActionType.GENERATE_PREVIEW,
            ActionType.POST_MESSAGE,
            ActionType.COMPLETE_TASK,
            ActionType.REPORT_BLOCKER,
        }

    def system_prompt(self, context: AgentContext) -> str:
        """The static rules, plus the limits this particular project imposes.

        The project-specific half is generated rather than written because the
        limits are facts about the target — which family, which templates, which
        revision — and a prompt that stated them in general terms would leave the
        model to work out which apply.
        """
        profile = _profile(context.db, context.project.id)
        platform = _target_platform(context.db, context.project.id)
        block = generation_block(profile, platform, context.settings)
        return f"{PROMPT}\n\n{self._project_limits(context, platform, block)}"

    def _project_limits(
        self,
        context: AgentContext,
        platform: TargetPlatform | None,
        block: GenerationBlock | None,
    ) -> str:
        if block is not None:
            return (
                "GENERATION IS BLOCKED FOR THIS PROJECT\n\n"
                f"{block.reason}\n\n"
                "Propose no change set and select no template — your context "
                "contains none. Use request_clarification with category "
                f"'{block.category.value}' and this question, and report_blocker:\n\n"
                f"  {block.question}"
            )

        family = platform.controller_family if platform is not None else ControllerFamily.OTHER
        templates = len(_available_templates(context.settings, family))
        return (
            "THIS PROJECT\n\n"
            f"Target controller family: {family.value}. Every instruction you "
            "propose must be one the catalogue lists for that family; "
            "`instruction_set.supported` in your context is the entire vocabulary "
            "available to you, and `instruction_set.refused` is never generated at "
            "all.\n\n"
            f"Approved templates for this family: {templates}. If none of them is "
            "the pattern a requirement needs, say so with request_clarification "
            "rather than choosing the nearest one.\n\n"
            f"Base every change set on revision "
            f"{context.project.current_revision_number} "
            "(`base_revision_number`). Generated logic here is an offline proposal: "
            "it has not been compiled, imported or downloaded, and nothing you "
            "produce establishes what any controller is running."
        )

    def build_context(self, context: AgentContext) -> dict[str, Any]:
        db = context.db
        project_id = context.project.id
        profile = _profile(db, project_id)
        platform = _target_platform(db, project_id)
        block = generation_block(profile, platform, context.settings)
        family = platform.controller_family if platform is not None else ControllerFamily.OTHER

        payload: dict[str, Any] = {
            "agent_kind": self.kind.value,
            "project": {
                "name": context.project.name,
                "client_reference": context.project.client_reference,
                "base_revision_number": context.project.current_revision_number,
            },
            "profile": _profile_row(profile),
            "target_platform": _platform_row(platform),
            "anchor_index": self.anchor_index(db, project_id),
            "existing": _organisation(db, project_id),
            "tags": _tag_rows(db, project_id),
            "decisions": _decision_rows(db, project_id),
            **_requirement_rows(db, project_id),
        }

        if block is not None:
            # No templates and no instruction set. The refusal is structural: an
            # agent with nothing to select from cannot select, and every rung it
            # could invent would have no approved template rule to cite as its
            # basis, so the action schema would refuse it at the boundary.
            payload["generation_blocked"] = block.as_dict()
            payload["templates"] = []
            payload["instruction_set"] = {}
            return payload

        registry = _registry_for(context.settings)
        payload["templates"] = [
            _template_row(template) for template in _available_templates(context.settings, family)
        ]
        payload["template_load_errors"] = [
            {"path": error.path, "problem": error.message} for error in registry.load_errors
        ]
        payload["instruction_set"] = {
            "controller_family": family.value,
            "supported": _instruction_rows(family),
            "refused": sorted(RESTRICTED_INSTRUCTIONS),
            "note": (
                "`refused` instructions are read and reported by this platform and "
                "never generated. The reason is stated when one is refused; do not "
                "propose one."
            ),
        }
        return payload


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------
def _profile(db: Session, project_id: uuid.UUID) -> ProjectProfile | None:
    return db.execute(
        select(ProjectProfile).where(
            ProjectProfile.project_id == project_id,
            ProjectProfile.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


def _target_platform(db: Session, project_id: uuid.UUID) -> TargetPlatform | None:
    return db.execute(
        select(TargetPlatform).where(
            TargetPlatform.project_id == project_id,
            TargetPlatform.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


def _registry_for(settings: Settings) -> TemplateRegistry:
    """The template registry, honouring a configured root.

    An administrator who points the platform at a different template tree must
    not be silently served the shipped one; the cached registry is only correct
    when no override is set.
    """
    if settings.template_root is not None:
        return load_templates(settings.template_root)
    return get_registry()


def _available_templates(settings: Settings, family: ControllerFamily) -> list[Template]:
    """The newest version of every template approved for this controller family."""
    return _registry_for(settings).for_family(family)


def _profile_row(profile: ProjectProfile | None) -> dict[str, Any]:
    if profile is None:
        return {}
    return {
        "project_name": profile.project_name,
        "project_type": profile.project_type.value,
        "naming_standard_version": profile.naming_standard_version,
        "alarm_philosophy_version": profile.alarm_philosophy_version,
        "units": profile.units.value,
        "locale": profile.locale,
        "release_mode": profile.release_mode.value,
        "restricted_domains": [str(item) for item in profile.restricted_domains],
        "restricted_domain_mode": profile.restricted_domain_mode.value,
        "is_complete_for_generation": profile.is_complete_for_generation,
        "missing_required_fields": [str(item) for item in profile.missing_required_fields],
        # The profile's own gate, carried through as the other production agents
        # carry it. It is one of several reasons generation may be blocked here,
        # so it is reported rather than relied on: see `generation_blocked`.
        "blocks_generation": profile.blocks_generation,
    }


def _platform_row(platform: TargetPlatform | None) -> dict[str, Any]:
    if platform is None:
        return {}
    return {
        "controller_family": platform.controller_family.value,
        "controller_catalog_number": platform.controller_catalog_number,
        "firmware_revision": platform.firmware_revision,
        "studio_5000_version": platform.studio_5000_version,
        "safety_classification": platform.safety_classification.value,
        "is_safety_controller": platform.is_safety_controller,
        "supported_instruction_set_key": platform.supported_instruction_set_key,
        # Stated up front, because a limitation discovered after the work is done
        # is a rewrite and one read beforehand is a design constraint.
        "known_limitations": [str(item) for item in platform.known_limitations],
    }


def _template_row(template: Template) -> dict[str, Any]:
    """One template as the model sees it.

    ``restrictions`` and ``engineering_notes`` are included in full and are not
    decoration: they are the template's own statement of what it does *not* do,
    which is the part a model choosing between two patterns most needs and the
    part it is least able to infer.
    """
    return {
        "template_id": template.id,
        "version": template.version,
        "qualified_id": template.qualified_id(),
        "checksum": template.checksum,
        "category": template.category.value,
        "display_name": template.display_name,
        "description": template.description,
        "approval_level": template.approval_level.value,
        "needs_senior_review": template.needs_senior_review,
        "restricted_domains": [item.value for item in template.restricted_domains],
        "parameters": [
            {
                "name": parameter.name,
                "type": parameter.type,
                "required": parameter.required,
                "default": parameter.default,
                "choices": list(parameter.choices),
                "description": parameter.description,
            }
            for parameter in template.parameters
        ],
        "required_tags": [
            {
                "role": tag.role.value,
                "suffix": tag.suffix,
                "data_type": tag.data_type.value,
                "type_name": tag.type_name,
                "units": tag.units,
                "when_parameter": tag.when_parameter,
                "description": tag.description,
            }
            for tag in template.required_tags
        ],
        "creates": {
            "tasks": [item.name for item in template.generated_objects.tasks],
            "programs": [item.name for item in template.generated_objects.programs],
            "routines": [item.qualified_name for item in template.generated_objects.routines],
            "rungs": [item.title for item in template.generated_objects.rungs],
        },
        "restrictions": list(template.restrictions),
        "engineering_notes": template.engineering_notes,
    }


def _instruction_rows(family: ControllerFamily) -> list[dict[str, Any]]:
    """The mnemonics this platform will generate for ``family``.

    Arity is included because an instruction proposed with the wrong number of
    operands is refused at validation, and the model can only avoid that if it is
    told. ``requires_human_review`` is included for the same reason it exists:
    the instruction is still generated, and a human is told to look at it.
    """
    rows: list[dict[str, Any]] = []
    for spec in sorted(INSTRUCTIONS.values(), key=lambda item: item.mnemonic):
        if not supports(spec.mnemonic, family):
            continue
        upper = "n" if spec.max_operands >= VARIADIC_OPERAND_LIMIT else str(spec.max_operands)
        rows.append(
            {
                "mnemonic": spec.mnemonic,
                "name": spec.name,
                "category": spec.category.value,
                "operands": f"{spec.min_operands}..{upper}",
                "is_output": spec.is_output,
                "requires_human_review": spec.requires_human_review,
            }
        )
    return rows


def _organisation(db: Session, project_id: uuid.UUID) -> dict[str, Any]:
    """The controller organisation that already exists.

    Carries ``object_revision`` and ``approval_state`` on every row. Both are
    load-bearing: an update has to state the revision it was written against or
    it would silently overwrite concurrent work, and an approved object may not
    be edited at all — which the model can only respect if it can see it.
    """
    tasks = list(
        db.execute(
            select(ControllerTask)
            .where(ControllerTask.project_id == project_id, ControllerTask.deleted_at.is_(None))
            .order_by(ControllerTask.ordinal)
        ).scalars()
    )
    programs = list(
        db.execute(
            select(Program)
            .where(Program.project_id == project_id, Program.deleted_at.is_(None))
            .order_by(Program.ordinal)
        ).scalars()
    )
    program_names = {program.id: program.name for program in programs}
    routines = list(
        db.execute(
            select(Routine)
            .where(Routine.project_id == project_id, Routine.deleted_at.is_(None))
            .order_by(Routine.ordinal)
        ).scalars()
    )
    routine_names = {
        routine.id: f"{program_names.get(routine.program_id, '?')}/{routine.name}"
        for routine in routines
    }
    networks = list(
        db.execute(
            select(LogicNetwork)
            .where(LogicNetwork.project_id == project_id, LogicNetwork.deleted_at.is_(None))
            .order_by(LogicNetwork.ordinal)
        ).scalars()
    )

    return {
        "controller_tasks": [
            {
                "id": str(task.id),
                "name": task.name,
                "task_type": task.task_type.value,
                "rate_ms": task.rate_ms,
                "inhibit_task": task.inhibit_task,
                "object_revision": task.object_revision,
                "approval_state": task.approval_state.value,
            }
            for task in tasks
        ],
        "programs": [
            {
                "id": str(program.id),
                "name": program.name,
                "program_type": program.program_type.value,
                "controller_task_id": (
                    str(program.controller_task_id) if program.controller_task_id else None
                ),
                "main_routine_name": program.main_routine_name,
                "object_revision": program.object_revision,
                "approval_state": program.approval_state.value,
            }
            for program in programs
        ],
        "routines": [
            {
                "id": str(routine.id),
                "qualified_name": routine_names[routine.id],
                "language": routine.language.value,
                "is_main": routine.is_main,
                "rung_count": routine.rung_count,
                "template_id": routine.template_id,
                "object_revision": routine.object_revision,
                "approval_state": routine.approval_state.value,
            }
            for routine in routines
        ],
        "logic_networks": [
            {
                "id": str(network.id),
                "name": network.name,
                "routine": routine_names.get(network.routine_id, "?"),
                "template_id": network.template_id,
                "template_version": network.template_version,
                "requirement_reference": network.requirement_reference,
                "rung_count": network.rung_count,
                "object_revision": network.object_revision,
                "approval_state": network.approval_state.value,
            }
            for network in networks
        ],
    }


def _tag_rows(db: Session, project_id: uuid.UUID) -> dict[str, Any]:
    """Declared tags, so an operand can be bound rather than guessed."""
    tags = list(
        db.execute(
            select(Tag)
            .where(Tag.project_id == project_id, Tag.deleted_at.is_(None))
            .order_by(Tag.name)
        ).scalars()
    )
    rows = [
        {
            "name": tag.name,
            "scope": tag.scope.value,
            "role": tag.role.value,
            "data_type": tag.base_data_type.value,
            "data_type_name": tag.data_type_name,
            "object_revision": tag.object_revision,
            "approval_state": tag.approval_state.value,
        }
        for tag in tags[:MAX_TAGS]
    ]
    return {
        "tags": rows,
        "tags_truncated": len(tags) > MAX_TAGS,
        "tag_count": len(tags),
    }


def _decision_rows(db: Session, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """Recorded human decisions, which are a citable basis in their own right."""
    decisions = list(
        db.execute(
            select(ProjectDecision)
            .where(
                ProjectDecision.project_id == project_id,
                ProjectDecision.deleted_at.is_(None),
            )
            .order_by(ProjectDecision.reference)
        ).scalars()
    )
    return [
        {
            "reference": decision.reference,
            "kind": decision.kind.value,
            "title": decision.title,
            "statement": decision.statement,
            "decided_by": decision.decided_by_name,
        }
        for decision in decisions
    ]


def _requirement_rows(db: Session, project_id: uuid.UUID) -> dict[str, Any]:
    """Requirements, split into what may be cited and what may not.

    The split is the point. An inferred requirement nobody has ruled on, and a
    requirement with an open blocking clarification against it, are both things
    this platform proposed or misread — building logic on either promotes a guess
    into the delivered program. Handing them over in one list and asking the
    prompt to be careful would make that a matter of model discipline; handing
    them over in two lists, each row saying why it is where it is, does not.
    """
    requirements = list(
        db.execute(
            select(Requirement)
            .where(Requirement.project_id == project_id, Requirement.deleted_at.is_(None))
            .order_by(Requirement.reference)
        ).scalars()
    )
    blocked_ids = {
        clarification.requirement_id
        for clarification in db.execute(
            select(Clarification).where(
                Clarification.project_id == project_id,
                Clarification.status == ClarificationStatus.OPEN,
                Clarification.blocks_generation.is_(True),
            )
        ).scalars()
        if clarification.requirement_id is not None
    }

    citable: list[dict[str, Any]] = []
    awaiting: list[dict[str, Any]] = []
    for requirement in requirements[:MAX_REQUIREMENTS]:
        row = {
            "reference": requirement.reference,
            "title": requirement.title,
            "statement": requirement.statement,
            "kind": requirement.kind.value,
            "priority": requirement.priority.value,
            "status": requirement.status.value,
            "is_inferred": requirement.is_inferred,
            "is_safety_related": requirement.is_safety_related,
        }
        if requirement.id in blocked_ids:
            awaiting.append(
                {
                    **row,
                    "why_not_citable": (
                        "an open clarification against this requirement blocks "
                        "anything derived from it"
                    ),
                }
            )
        elif requirement.status not in CITABLE_REQUIREMENT_STATUSES:
            awaiting.append(
                {
                    **row,
                    "why_not_citable": (
                        f"status is {requirement.status.value}"
                        + (
                            ", and it was inferred rather than read"
                            if requirement.is_inferred
                            else ""
                        )
                    ),
                }
            )
        else:
            citable.append(row)

    return {
        "requirements": citable,
        "requirements_awaiting_decision": awaiting,
        "requirements_truncated": len(requirements) > MAX_REQUIREMENTS,
    }


def unresolved_operand_reports(
    operations: Sequence[ChangeOperationProposal],
) -> list[UnresolvedOperand]:
    """Every unresolved operand a change set reports, across all its operations.

    Provided so the wiring stage can put them in front of a human as one list.
    An operand this platform could not bind is the most useful thing a generation
    run produces after the logic itself, and it is worthless if it is only
    discoverable by opening each operation in turn.
    """
    return [
        item
        for operation in operations
        if (selection := selection_of(operation)) is not None
        for item in selection.unresolved_operands
    ]
