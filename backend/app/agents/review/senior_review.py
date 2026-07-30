"""Senior Review Agent.

Independently checks every finding before it reaches a human: rejects
unsupported claims, merges duplicates, verifies the evidence anchors resolve,
challenges severity, and lowers confidence where the evidence is thin.

Two things it is structurally prevented from doing. It cannot create a finding —
a reviewer that could add its own claims is not reviewing, it is producing. And
it cannot raise a confidence: the runner refuses an increase and the executor
refuses it again. Review exists to add doubt; letting a second pass raise
confidence would let a weak claim be laundered into a strong one.

It is not the final engineer. Its approval means "worth a human's attention",
never "correct".
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.agents.base.agent import SHARED_RULES, AgentContext, BaseAgent
from app.core.enums import ActionType, AgentKind
from app.models.finding import Finding

PROMPT = f"""{SHARED_RULES}

You are the Senior Review Agent.

You review the findings other agents produced. You do not produce findings of
your own, and you cannot: the platform will refuse the attempt.

For each finding, return exactly one verdict:

- supported — the evidence resolves, includes a direct quotation, and the fact
  genuinely states what that evidence shows;
- unsupported — no evidence, or the evidence does not show what the fact claims.
  Say which. An unsupported finding is rejected and never reaches a human;
- duplicate — the same issue as another finding. Name which one;
- needs_more_evidence — plausible, but the evidence is thin, indirect, or
  affected by a parser limitation.

Check specifically:

1. Does `fact` contain a conclusion? "Rung 12 has no permissive contact" is a
   fact. "Rung 12 is unsafe" is not, and belongs in `inference`.
2. Does the evidence actually support the claim, or merely sit near it?
3. Was the finding derived from a document the parser could not fully read? If
   so it is at best provisional — say so and lower the confidence.
4. Is the severity proportionate? A naming mismatch on an interlock that IS
   implemented is not high severity; an interlock with no implementation at all
   may well be. Inflated severity buries real problems.
5. Do two findings describe the same underlying issue from different angles?

You may lower a confidence. You may not raise one — the platform refuses it.
You may lower a severity; a proposed increase is recorded as a note rather than
applied, so state your reasoning in `notes`.

Record every parser limitation that bears on a finding. An engineer reading a
finding derived from a partially-read document needs to know that.

You are not the final engineer and your approval is not sign-off. It means the
finding is worth a human's attention.
"""


class SeniorReviewAgent(BaseAgent):
    kind = AgentKind.SENIOR_REVIEW

    @property
    def allowed_actions(self) -> set[ActionType]:
        return {
            ActionType.UPDATE_FINDING_REVIEW,
            ActionType.REQUEST_APPROVAL,
            ActionType.POST_MESSAGE,
            ActionType.REPORT_NO_FINDINGS,
        }

    def system_prompt(self, context: AgentContext) -> str:
        return PROMPT

    def build_context(self, context: AgentContext) -> dict[str, Any]:
        findings = list(
            context.db.execute(
                select(Finding)
                .where(Finding.project_id == context.project.id)
                .order_by(Finding.created_at)
            ).scalars()
        )

        limitations_by_file = {item.file_id: item.message for item in context.graph.limitations}

        payload: list[dict[str, Any]] = []
        for finding in findings:
            links = finding.evidence_links
            anchors = [link.anchor for link in links if link.anchor is not None]
            payload.append(
                {
                    "id": str(finding.id),
                    "dedupe_key": finding.dedupe_key,
                    "title": finding.title,
                    "category": finding.category.value,
                    "severity": finding.severity.value,
                    "confidence": finding.confidence,
                    "created_by_agent_kind": finding.created_by_agent_kind,
                    "fact": finding.fact,
                    "inference": finding.inference,
                    "suggested_review_action": finding.suggested_review_action,
                    "limitations": finding.limitations,
                    "is_reviewed": finding.is_reviewed,
                    "evidence_count": len(links),
                    "has_direct_evidence": finding.has_direct_evidence,
                    "evidence": [
                        {
                            "anchor_id": str(link.anchor_id),
                            "role": link.role,
                            "evidence_kind": link.evidence_kind.value,
                            "source_label": link.anchor.source_label if link.anchor else "",
                            "display_location": (
                                link.anchor.display_location if link.anchor else ""
                            ),
                            "excerpt": link.anchor.excerpt if link.anchor else "",
                        }
                        for link in links
                    ],
                    # Surfaced per finding so the reviewer can see that a claim
                    # rests on a document the parser could not fully read.
                    "parser_limitations": sorted(
                        {
                            limitations_by_file[anchor.file_id]
                            for anchor in anchors
                            if anchor.file_id in limitations_by_file
                        }
                    ),
                }
            )

        return {
            "agent_kind": self.kind.value,
            "findings": payload,
            "counts": context.graph.counts(),
            "project_limitations": [item.as_dict() for item in context.graph.limitations],
        }
