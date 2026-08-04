"""Agent catalogue.

Declarative metadata for every agent the platform knows about, kept separate
from the implementations so the API can list agents (and report which are
unavailable) without importing agent logic or depending on optional plugins.

``building`` is a display label. ``home_location`` is a :class:`SemanticLocation`
member: the backend broadcasts semantic district names and never pixel
coordinates, so moving a building on screen stays a frontend change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.enums import AgentKind, SemanticLocation, TaskKind


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Static description of an agent."""

    kind: AgentKind
    display_name: str
    description: str
    building: str
    home_location: SemanticLocation
    handles_task_kinds: tuple[TaskKind, ...]
    #: True when supplied by a plugin rather than the core product.
    is_plugin: bool = False
    #: Responsibilities the agent explicitly must not take on. Surfaced in the
    #: UI so an engineer knows where an agent's remit stops.
    boundaries: tuple[str, ...] = field(default_factory=tuple)


CORE_AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        kind=AgentKind.PROJECT_MANAGER,
        display_name="Project Manager",
        description=(
            "Inventories the uploaded files, identifies missing expected inputs, "
            "and builds the task plan with its dependencies."
        ),
        building="Intake Hall",
        home_location=SemanticLocation.PROJECT_INTAKE,
        handles_task_kinds=(TaskKind.INVENTORY_FILES, TaskKind.PLAN_PROJECT),
        boundaries=(
            "Does not draw technical engineering conclusions.",
            "Does not assess whether logic is correct.",
        ),
    ),
    AgentSpec(
        kind=AgentKind.PLC_REVIEW,
        display_name="PLC Review",
        description=(
            "Reviews normalised L5X content: routines, rungs, instructions, tags, "
            "outputs, permissives, interlocks, alarms and mode logic. Cites exact "
            "evidence and separates observation from inference."
        ),
        building="Logic Works",
        home_location=SemanticLocation.LADDER_LOGIC_LAB,
        handles_task_kinds=(TaskKind.REVIEW_PLC,),
        boundaries=(
            "Does not connect to a controller.",
            "Does not compile or execute ladder logic.",
            "Cannot establish runtime or firmware behaviour from an L5X export alone.",
        ),
    ),
    AgentSpec(
        kind=AgentKind.INTERLOCK_REVIEW,
        display_name="Interlock Review",
        description=(
            "Maps documented interlocks to the logic that implements them and "
            "produces a structured discrepancy matrix with evidence on both sides."
        ),
        building="Safety Annex",
        home_location=SemanticLocation.AUTOMATION_CORE,
        handles_task_kinds=(TaskKind.REVIEW_INTERLOCKS,),
        boundaries=(
            "Does not approve or certify a safety function.",
            "Every safety-relevant discrepancy is routed to a human engineer.",
        ),
    ),
    AgentSpec(
        kind=AgentKind.SENIOR_REVIEW,
        display_name="Senior Review",
        description=(
            "Independently checks each finding: rejects unsupported claims, merges "
            "duplicates, verifies evidence anchors, challenges severity, and lowers "
            "confidence where evidence is incomplete."
        ),
        building="Review Hall",
        home_location=SemanticLocation.QA_CAMPUS,
        handles_task_kinds=(TaskKind.SENIOR_REVIEW, TaskKind.REQUEST_APPROVALS),
        boundaries=(
            "Is not the final human engineer and never signs off on its own.",
            "Can only reduce confidence, never raise it above what was claimed.",
        ),
    ),
    # -- Production pipeline ------------------------------------------------
    AgentSpec(
        kind=AgentKind.REQUIREMENTS_PLANNING,
        display_name="Requirements and Planning",
        description=(
            "Reads the project description and the supporting documents and produces "
            "a traceable requirements model: requirements, acceptance criteria, "
            "assumptions, constraints and clarifications, each citing the passage it "
            "was read from."
        ),
        building="Requirements Hall",
        home_location=SemanticLocation.REQUIREMENTS_HALL,
        handles_task_kinds=(TaskKind.EXTRACT_REQUIREMENTS,),
        boundaries=(
            "Establishes what was asked for; never judges whether any of it is "
            "correct, safe or achievable.",
            "Never fills a stated gap with a plausible default: a missing threshold "
            "becomes a clarification or an assumption with its consequence stated.",
            "Cannot accept its own work — a requirement stays proposed, an "
            "assumption unconfirmed and a clarification open until a named human "
            "settles it.",
            "Does not design tags, I/O or logic.",
        ),
    ),
    AgentSpec(
        kind=AgentKind.TAG_ARCHITECTURE,
        display_name="Tag Architecture",
        description=(
            "Designs the canonical tag model every other discipline binds to — tags, "
            "aliases, groups, UDTs and AOI references — under the project's naming "
            "standard, with a stated basis for each."
        ),
        building="Tag Foundry",
        home_location=SemanticLocation.TAG_FOUNDRY,
        handles_task_kinds=(TaskKind.DESIGN_TAGS,),
        boundaries=(
            "Never renames an existing tag by editing it: a rename is proposed as "
            "its own operation, with its impact report, for a human to decide.",
            "The ESCO naming standard governs what this platform creates, not the "
            "customer's existing tags.",
            "Cannot author a UDT's member list — no change operation addresses a "
            "member — so a proposed UDT says so rather than claiming a shape.",
        ),
    ),
    AgentSpec(
        kind=AgentKind.PLC_LOGIC,
        display_name="PLC Logic",
        description=(
            "Creates the controller organisation and selects an approved ESCO control "
            "pattern per network, supplying its parameters. Deterministic code expands "
            "the pattern into rungs and generates the L5X from those rows."
        ),
        building="Automation Core",
        home_location=SemanticLocation.AUTOMATION_CORE,
        handles_task_kinds=(TaskKind.DESIGN_PLC_ARCHITECTURE, TaskKind.GENERATE_LOGIC),
        boundaries=(
            "Never emits XML: it writes the logic model, and the generator emits from that.",
            "Writes no rung freehand — every network names an approved, checksummed "
            "template, so the pattern was reviewed before it was ever selected.",
            "Proposes no instruction the target controller family cannot run, and "
            "none this platform refuses to generate at all.",
            "Generates nothing at all for a project whose profile, target platform or "
            "restricted domains do not permit it.",
        ),
    ),
)

CORE_AGENTS_BY_KIND: dict[AgentKind, AgentSpec] = {spec.kind: spec for spec in CORE_AGENTS}


def spec_for(kind: AgentKind | str) -> AgentSpec | None:
    try:
        return CORE_AGENTS_BY_KIND.get(AgentKind(kind))
    except ValueError:
        return None


def specs_for_task_kind(task_kind: TaskKind) -> list[AgentSpec]:
    return [spec for spec in CORE_AGENTS if task_kind in spec.handles_task_kinds]
