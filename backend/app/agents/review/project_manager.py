"""Project Manager Agent.

Inventories what was supplied, names what is missing, and decides which
specialist reviews the evidence actually supports.

Its permissions are deliberately the narrowest of the four: it may create tasks,
post messages and report blockers. It **cannot** create a finding or a
recommendation, so it is structurally incapable of drawing a technical
engineering conclusion — a boundary the spec sets and that is worth enforcing in
code rather than asking a prompt to respect.
"""

from __future__ import annotations

from typing import Any

from app.agents.base.agent import SHARED_RULES, AgentContext, BaseAgent
from app.core.enums import ActionType, AgentKind

PROMPT = f"""{SHARED_RULES}

You are the Project Manager Agent.

Your job is to plan, not to review. You decide which specialist reviews the
supplied evidence can actually support, and you say plainly what is missing.

You may:
- create_task, to schedule a specialist review that the evidence supports;
- post_message, to tell the engineer what was supplied, what was not, and what
  could not be read;
- report_blocker, when there is not enough to review at all.

You must NOT:
- assess whether any logic is correct, safe or complete;
- comment on a threshold, an interlock, a tag name or a rung;
- schedule a specialist review for evidence that is not there. Planning an
  interlock review with no interlock schedule wastes the run and produces a
  review of nothing.

Available specialist task kinds:
- review_plc (agent_kind "plc_review") — needs normalised L5X logic.
- review_interlocks (agent_kind "interlock_review") — needs BOTH an interlock
  schedule and logic to compare it against.

Say explicitly which expected inputs are absent and which files could not be
read. An engineer needs to know the review covered four documents when six were
expected — that is not a footnote, it is the scope of the answer.
"""


class ProjectManagerAgent(BaseAgent):
    kind = AgentKind.PROJECT_MANAGER

    @property
    def allowed_actions(self) -> set[ActionType]:
        return {
            ActionType.CREATE_TASK,
            ActionType.POST_MESSAGE,
            ActionType.REPORT_BLOCKER,
        }

    def system_prompt(self, context: AgentContext) -> str:
        return PROMPT

    def build_context(self, context: AgentContext) -> dict[str, Any]:
        graph = context.graph
        return {
            "agent_kind": self.kind.value,
            "project": {
                "name": context.project.name,
                "client_reference": context.project.client_reference,
            },
            "graph": {
                "counts": graph.counts(),
                "missing_inputs": graph.missing_inputs,
                "unreadable_files": graph.unreadable_files,
                # Documents and limitations only — no rung text, no interlock
                # rows. The planner has no business reading the engineering
                # content it is deciding whether to schedule a review of.
                "documents": [
                    {
                        "filename": document.filename,
                        "document_role": document.document_role,
                        "statement_count": len(document.statements),
                    }
                    for document in graph.documents
                ],
                "limitations": [item.as_dict() for item in graph.limitations],
            },
        }
