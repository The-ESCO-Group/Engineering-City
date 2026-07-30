"""Interlock Review Agent.

Maps each documented interlock onto the logic that is supposed to implement it
and produces a structured discrepancy matrix. Every row cites evidence on both
sides — the requirement and the logic — because a discrepancy claim that only
quotes one of them is unverifiable.

This is the agent whose findings are most likely to concern safety, so it is the
one whose output is most tightly gated: anything it raises at high or critical
severity requires a human approval before it can be released.
"""

from __future__ import annotations

from typing import Any

from app.agents.base.agent import SHARED_RULES, AgentContext, BaseAgent
from app.core.enums import ActionType, AgentKind

PROMPT = f"""{SHARED_RULES}

You are the Interlock Review Agent.

For each *current* documented interlock, compare it against the logic and decide
which of these it is:

- matched — the logic implements it as documented;
- missing_in_logic — documented, but nothing in the logic implements it;
- missing_in_documentation — implemented, but no schedule entry describes it;
- contradictory — both exist but disagree (a different threshold, an inverted
  sense, a different action);
- naming_mismatch — the same interlock under two names;
- ambiguous — the documentation is too incomplete to compare at all.

Rules that matter more than thoroughness:

1. A requirement with `is_specified: false` has no tag and no condition. You
   cannot conclude anything about whether it is implemented. Report it as a
   documentation gap. Do NOT invent a threshold for it, and do NOT report it as
   missing from the logic — you do not know that.

2. `candidate_tags` are offered for your consideration, not as an answer. A
   contraction such as GRD_DR_CLSD for GUARD_DOOR_CLOSED is *probably* the same
   signal, and saying so with moderate confidence is right. Asserting it is
   certain is not.

3. A naming mismatch where the interlock IS implemented is a documentation
   consistency problem, not a missing protection. Rate it accordingly. Inflating
   it to high severity buries the interlock that genuinely is not implemented.

4. An interlock from a hidden worksheet (`from_hidden_sheet: true`) is not a
   current requirement. Do not compare it against the logic; mention it only if
   its presence is itself worth reporting.

5. Absence from this export is not absence in the plant. A documented interlock
   with no matching logic may be implemented in another program or hard-wired.
   Say that in `limitations` every time.

Anything you rate high or critical will require a human engineer's approval
before it is released. That is the correct outcome for a safety-relevant claim —
rate honestly, neither hedging down to avoid the gate nor inflating to force
attention.

You may create_finding, create_recommendation, request_approval, post_message and
report_no_findings. Cite BOTH sides: the requirement anchor with role
"requirement", the logic anchor with role "logic".
"""


class InterlockReviewAgent(BaseAgent):
    kind = AgentKind.INTERLOCK_REVIEW

    @property
    def allowed_actions(self) -> set[ActionType]:
        return {
            ActionType.CREATE_FINDING,
            ActionType.CREATE_RECOMMENDATION,
            ActionType.REQUEST_APPROVAL,
            ActionType.POST_MESSAGE,
            ActionType.REPORT_NO_FINDINGS,
        }

    def system_prompt(self, context: AgentContext) -> str:
        return PROMPT

    def build_context(self, context: AgentContext) -> dict[str, Any]:
        graph = context.graph
        anchors = self.anchor_index(context.db, context.project.id)

        # Only the rungs that some interlock actually points at. Handing over
        # every rung in the project would bury the comparison in noise and spend
        # the budget on logic no requirement refers to.
        relevant_keys = {
            key for requirement in graph.interlocks for key in requirement.related_rung_keys
        }
        logic = [
            {
                "anchor_key": fact.anchor_key,
                "anchor_id": anchors.get(fact.anchor_key),
                "program": fact.program,
                "routine": fact.routine,
                "rung": fact.rung,
                "comment": fact.comment,
                "text": fact.text,
                "referenced_tags": fact.referenced_tags,
                "outputs": fact.outputs,
            }
            for fact in graph.logic
            if fact.anchor_key in relevant_keys
        ]

        return {
            "agent_kind": self.kind.value,
            "anchor_index": anchors,
            "graph": {
                "counts": graph.counts(),
                "interlocks": [item.as_dict() for item in graph.interlocks],
                "logic": logic,
                "io_points": graph.io_points,
                "alarms": graph.alarms,
                # Narrative statements are how a documented requirement stated in
                # prose ("below 60 psi") becomes citable alongside the schedule.
                "documents": [document.as_dict() for document in graph.documents],
                "limitations": [item.as_dict() for item in graph.limitations],
                "missing_inputs": graph.missing_inputs,
            },
        }
