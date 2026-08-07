"""Run one ESCO mission with Anthropic's Claude Python SDK.

Required environment:
  ESCO_AGENT_TOKEN, ANTHROPIC_API_KEY, ANTHROPIC_MODEL
Optional:
  ESCO_BASE_URL (default http://127.0.0.1:8010)
"""

from __future__ import annotations

import json
import os
import sys

from anthropic import Anthropic
from esco_city import EscoCityClient


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
        activity="Claude SDK agent is reviewing project evidence",
        message="Claude SDK mission started.",
    )
    tool = {
        "name": "submit_esco_result",
        "description": "Return the completed ESCO mission as structured proposed actions.",
        "input_schema": context["response_schema"],
    }
    response = Anthropic().messages.create(
        model=required("ANTHROPIC_MODEL"),
        max_tokens=8192,
        system=(
            f"{context['rules']}\n\n"
            f"Mission: {context['mission']}\n"
            f"Allowed actions: {', '.join(context['allowed_actions'])}."
        ),
        messages=[
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)}
        ],
        tools=[tool],
        tool_choice={"type": "tool", "name": "submit_esco_result"},
    )
    result = next(
        block.input for block in response.content if getattr(block, "type", "") == "tool_use"
    )
    usage = response.usage
    submitted = city.submit(
        rationale=str(result["rationale"]),
        actions=list(result["actions"]),
        warnings=list(result.get("warnings") or []),
        prompt_tokens=int(usage.input_tokens),
        completion_tokens=int(usage.output_tokens),
    )
    summary = {
        "agent_run": submitted["run"]["id"],
        "status": submitted["run"]["status"],
    }
    sys.stdout.write(f"{json.dumps(summary)}\n")


if __name__ == "__main__":
    main()
