"""Agent catalogue.

Declarative metadata for every agent the platform knows about, kept separate
from the implementations so the API can list agents (and report which are
unavailable) without importing agent logic or depending on optional plugins.

``building`` and ``home_location`` exist for the city visualisation only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.enums import AgentKind, TaskKind


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Static description of an agent."""

    kind: AgentKind
    display_name: str
    description: str
    building: str
    home_location: str
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
        home_location="intake_dock",
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
        home_location="logic_works",
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
        home_location="safety_annex",
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
        home_location="review_hall",
        handles_task_kinds=(TaskKind.SENIOR_REVIEW, TaskKind.REQUEST_APPROVALS),
        boundaries=(
            "Is not the final human engineer and never signs off on its own.",
            "Can only reduce confidence, never raise it above what was claimed.",
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
