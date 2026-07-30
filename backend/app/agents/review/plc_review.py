"""PLC Review Agent.

Reviews normalised L5X content: routines, rungs, instructions, tags, outputs,
permissives, alarms and mode logic. Reports structural observations, each citing
the rung or tag it came from.

The hard boundary, stated in the prompt and true of the input: an L5X export
describes offline logic at export time. It cannot establish what a controller is
running, what its firmware does, or whether the program was ever downloaded. Any
finding that implied otherwise would be asserting something the evidence cannot
support.
"""

from __future__ import annotations

from typing import Any

from app.agents.base.agent import SHARED_RULES, AgentContext, BaseAgent
from app.core.enums import ActionType, AgentKind
from app.core.services.evidence_graph import tag_usage_summary

#: Rungs handed to the model. Enough for a real review of a line's logic without
#: spending the whole token budget on one program.
MAX_RUNGS = 400

PROMPT = f"""{SHARED_RULES}

You are the PLC Review Agent.

You review normalised Studio 5000 L5X content. Look for:
- outputs energised without an expected permissive or interlock contact;
- operands referenced but never declared;
- duplicated or inconsistent rung patterns;
- alarm bits with no corresponding condition, or conditions with no alarm;
- mode logic that does not gate what the description says it gates.

WHAT THIS EVIDENCE CANNOT ESTABLISH — do not claim any of it:
- what a controller is currently running, or its firmware behaviour;
- whether the exported program was ever downloaded;
- that a signal does not exist because it is absent from this export. It may be
  declared in another program, another controller, or hard-wired.

Record instructions as they are. GRT(AIR_PRESS_PSI, 40.0) is a comparison
instruction with two operands; that it constitutes a low-pressure trip at 40 psi
is an interpretation, and it belongs in `inference`, never in `fact`.

Cite the rung. A finding about logic that does not name the program, routine and
rung number cannot be checked, and an engineer will not act on it.

You may create_finding, create_recommendation, post_message and
report_no_findings. Every finding needs at least one evidence anchor from the
context. If you find nothing, use report_no_findings and say what you examined.
"""


class PLCReviewAgent(BaseAgent):
    kind = AgentKind.PLC_REVIEW

    @property
    def allowed_actions(self) -> set[ActionType]:
        return {
            ActionType.CREATE_FINDING,
            ActionType.CREATE_RECOMMENDATION,
            ActionType.POST_MESSAGE,
            ActionType.REPORT_NO_FINDINGS,
        }

    def system_prompt(self, context: AgentContext) -> str:
        return PROMPT

    def build_context(self, context: AgentContext) -> dict[str, Any]:
        graph = context.graph
        anchors = self.anchor_index(context.db, context.project.id)

        rungs = [
            {
                "anchor_key": fact.anchor_key,
                "anchor_id": anchors.get(fact.anchor_key),
                "program": fact.program,
                "routine": fact.routine,
                "rung": fact.rung,
                "comment": fact.comment,
                "text": fact.text,
                "instructions": fact.instructions,
                "referenced_tags": fact.referenced_tags,
                "outputs": fact.outputs,
            }
            for fact in graph.logic[:MAX_RUNGS]
        ]

        return {
            "agent_kind": self.kind.value,
            "counts": graph.counts(),
            "anchor_index": anchors,
            "graph": {
                "counts": graph.counts(),
                "logic": rungs,
                "tags": [graph.tags[key].as_dict() for key in sorted(graph.tags)],
                "tag_usage": tag_usage_summary(graph),
                "alarms": graph.alarms,
                "limitations": [item.as_dict() for item in graph.limitations],
            },
            "truncated": len(graph.logic) > MAX_RUNGS,
            "note": (
                f"Showing {len(rungs)} of {len(graph.logic)} rungs."
                if len(graph.logic) > MAX_RUNGS
                else ""
            ),
        }
