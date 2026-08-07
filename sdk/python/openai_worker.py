"""Run one ESCO mission with the OpenAI Agents SDK.

Required environment:
  ESCO_AGENT_TOKEN, OPENAI_API_KEY
Optional:
  ESCO_BASE_URL (default http://127.0.0.1:8010), OPENAI_MODEL
"""

from __future__ import annotations

import json
import os
import sys

from agents import Agent, AgentOutputSchema, Runner
from esco_city import EscoCityClient
from pydantic import BaseModel, Field


class ProposedAction(BaseModel):
    action_type: str
    summary: str
    target_type: str = ""
    target_id: str | None = None
    evidence_anchor_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    requires_approval: bool = False
    payload: dict[str, object] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class MissionResult(BaseModel):
    rationale: str
    actions: list[ProposedAction]
    warnings: list[str] = Field(default_factory=list)


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main() -> None:
    city = EscoCityClient(
        os.environ.get("ESCO_BASE_URL", "http://127.0.0.1:8010"),
        required("ESCO_AGENT_TOKEN"),
    )
    context = city.context(evidence_limit=500)
    city.report_progress(
        status="working",
        location="qa_campus",
        activity="OpenAI SDK agent is reviewing project evidence",
        message="OpenAI SDK mission started.",
    )
    agent = Agent(
        name=context["instance"]["display_name"],
        model=os.environ.get("OPENAI_MODEL", "gpt-5.6-sol"),
        instructions=(
            f"{context['rules']}\n\n"
            f"Mission: {context['mission']}\n"
            f"Allowed actions: {', '.join(context['allowed_actions'])}."
        ),
        output_type=AgentOutputSchema(MissionResult, strict_json_schema=False),
    )
    result = Runner.run_sync(agent, json.dumps(context, ensure_ascii=False, default=str))
    output = result.final_output_as(MissionResult, raise_if_incorrect_type=True)
    submitted = city.submit(
        rationale=output.rationale,
        actions=[item.model_dump(mode="json") for item in output.actions],
        warnings=output.warnings,
    )
    summary = {
        "agent_run": submitted["run"]["id"],
        "status": submitted["run"]["status"],
    }
    sys.stdout.write(f"{json.dumps(summary)}\n")


if __name__ == "__main__":
    main()
