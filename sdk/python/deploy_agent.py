"""Deploy one external SDK worker into an ESCO project.

Required environment:
  ESCO_ACCESS_TOKEN, ESCO_PROJECT_ID, ESCO_PROVIDER, ESCO_AGENT_MISSION
Optional:
  ESCO_BASE_URL, ESCO_AGENT_NAME, ESCO_AGENT_SPECIALTY, ESCO_AGENT_MODEL,
  ESCO_AGENT_TTL_SECONDS, ESCO_AGENT_LOCATION

The returned capability token is intentionally printed once so it can be
passed directly to the isolated provider worker as ESCO_AGENT_TOKEN.
"""

from __future__ import annotations

import json
import os
import sys

from esco_city import EscoCityOperatorClient


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main() -> None:
    provider = required("ESCO_PROVIDER").lower()
    default_model = "gpt-5.6-sol" if provider == "openai" else ""
    model = os.environ.get("ESCO_AGENT_MODEL", default_model).strip()
    if not model:
        raise RuntimeError(
            "ESCO_AGENT_MODEL is required for Anthropic deployments; use a model "
            "available to your Anthropic account."
        )

    operator = EscoCityOperatorClient(
        os.environ.get("ESCO_BASE_URL", "http://127.0.0.1:8010"),
        required("ESCO_ACCESS_TOKEN"),
    )
    deployment = operator.deploy(
        project_id=required("ESCO_PROJECT_ID"),
        provider=provider,
        display_name=os.environ.get("ESCO_AGENT_NAME", "Orbit"),
        specialty=os.environ.get("ESCO_AGENT_SPECIALTY", "External engineering reviewer"),
        mission=required("ESCO_AGENT_MISSION"),
        model=model,
        ttl_seconds=int(os.environ.get("ESCO_AGENT_TTL_SECONDS", "3600")),
        initial_location=os.environ.get("ESCO_AGENT_LOCATION", "project_intake"),
    )
    output = {
        "instance_id": deployment["instance"]["id"],
        "display_name": deployment["instance"]["display_name"],
        "expires_at": deployment["token_expires_at"],
        "agent_token": deployment["agent_token"],
    }
    sys.stdout.write(f"{json.dumps(output, indent=2)}\n")


if __name__ == "__main__":
    main()
