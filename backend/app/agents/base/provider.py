"""Model provider abstraction.

Agents never talk to a model directly. They ask for a structured completion and
get back either a validated object or an error — so the provider layer is the
only place that knows a network exists, and it is the only place that has to be
audited for what leaves this machine.

Three properties the rest of the system depends on:

* **Off by default.** A provider that is not in ``settings.enabled_providers``
  cannot be constructed. Turning on OpenAI or Anthropic sends project content to
  a third party, so it takes an explicit administrator decision, not a fallback.
* **Structured output only.** :meth:`ModelProvider.complete_structured` returns
  parsed JSON validated against a schema. Free text never reaches the action
  layer.
* **No reasoning is retained.** Providers return the answer and the token counts.
  If a model emits a reasoning trace, it is discarded here and never persisted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from app.core.config import Settings
from app.core.enums import ProviderKind
from app.core.errors import (
    ProviderError,
    ProviderNotEnabledError,
    ProviderRateLimitedError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class ProviderResponse:
    """What a provider returns.

    Deliberately has no field for reasoning or a raw transcript: there is nowhere
    to put one, so nothing downstream can accidentally persist it.
    """

    #: The parsed structured payload.
    data: dict[str, Any]
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    #: Anything the provider itself reported as a problem.
    warnings: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@runtime_checkable
class ModelProvider(Protocol):
    """What an agent runner needs from a model."""

    kind: ProviderKind
    model: str

    def complete_structured(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> ProviderResponse: ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model's reply.

    Local models in particular wrap JSON in prose or a code fence. Recovering the
    object is worth doing, but *inventing* one is not: if nothing parses, this
    raises rather than returning an empty dict, because an empty dict would look
    downstream exactly like an agent that legitimately found nothing.
    """
    stripped = text.strip()
    if not stripped:
        raise ProviderError("The model returned an empty response.")

    candidates: list[str] = []
    fenced = _FENCE.search(stripped)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(stripped)

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ProviderError(
        "The model did not return a JSON object.",
        details={"preview": stripped[:400]},
    )


def estimate_tokens(text: str) -> int:
    """Rough token count for providers that do not report usage.

    Four characters per token is the usual English approximation. Only used for
    budget accounting, and only when the provider gave us nothing better — the
    alternative is recording zero, which would make a budget unenforceable.
    """
    return max(1, len(text) // 4)


def guard_enabled(kind: ProviderKind, settings: Settings) -> None:
    """Refuse to build a provider an administrator has not enabled."""
    if not settings.provider_enabled(kind):
        raise ProviderNotEnabledError(
            f"The '{kind.value}' provider is not enabled on this installation. "
            f"An administrator must add it to ESCO_ENABLED_PROVIDERS. "
            f"Currently enabled: "
            f"{', '.join(p.value for p in settings.enabled_providers)}.",
            details={"provider": kind.value},
        )


def classify_http_error(status_code: int, body: str, provider: str) -> ProviderError:
    """Turn a provider HTTP failure into the right domain error.

    Rate limiting is distinguished from everything else because the runner can
    retry it, while a 401 or a 400 will fail identically forever and retrying
    just burns the budget.
    """
    preview = body[:300]
    if status_code == 429:
        return ProviderRateLimitedError(
            f"{provider} is rate limiting this installation.",
        )
    if status_code in (401, 403):
        return ProviderError(
            f"{provider} rejected the credentials configured for this installation.",
            details={"status": status_code},
        )
    if 500 <= status_code < 600:
        return ProviderError(
            f"{provider} returned a server error ({status_code}); it may be temporary.",
            details={"status": status_code, "body": preview},
        )
    return ProviderError(
        f"{provider} rejected the request ({status_code}).",
        details={"status": status_code, "body": preview},
    )
