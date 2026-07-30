"""Providers that talk to a model over HTTP.

Ollama runs on this machine; OpenAI and Anthropic do not. All three live in one
module because they share the same shape and the same obligations, and because
having exactly one file to audit for "what can leave this machine" is worth more
than a tidier class hierarchy.

Every one of them is refused unless an administrator has explicitly enabled it.
For the two cloud providers, enabling also requires supplying a credential, and
``Settings`` refuses to start if the provider is listed without one — a provider
that is enabled but unusable would fail at the worst possible moment, mid-review.

Reasoning content is never requested and never retained. If a model returns one,
it is dropped here.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.agents.base.provider import (
    ChatMessage,
    ProviderResponse,
    classify_http_error,
    estimate_tokens,
    extract_json,
    guard_enabled,
)
from app.core.config import Settings
from app.core.enums import ProviderKind
from app.core.errors import ProviderError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Appended to the system prompt. Belt and braces alongside each provider's
#: native JSON mode, because local models honour the native switch unevenly.
JSON_INSTRUCTION = (
    "Reply with a single JSON object and nothing else. No prose, no code fence, "
    "no explanation before or after."
)


def _schema_hint(schema: dict[str, Any]) -> str:
    return f"The JSON object must conform to this JSON Schema:\n{json.dumps(schema)}"


class _HttpProvider:
    """Shared request handling: timeouts, error classification, logging."""

    kind: ProviderKind
    model: str

    def __init__(self, settings: Settings) -> None:
        guard_enabled(self.kind, settings)
        self.settings = settings

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.settings.provider_timeout_seconds) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"{self.kind.value} did not respond within "
                f"{self.settings.provider_timeout_seconds:.0f}s.",
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Could not reach {self.kind.value}: {exc}",
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            raise classify_http_error(response.status_code, response.text, self.kind.value)

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(f"{self.kind.value} returned a response that is not JSON.") from exc

        logger.info(
            "provider call",
            extra={
                "provider": self.kind.value,
                "model": self.model,
                "latency_ms": latency_ms,
            },
        )
        return body, latency_ms


class OllamaProvider(_HttpProvider):
    """A model served by a local Ollama instance.

    The recommended non-mock option: it keeps project content on this machine,
    which is the whole point of a local-first product.
    """

    kind = ProviderKind.OLLAMA

    def __init__(self, settings: Settings, model: str | None = None) -> None:
        super().__init__(settings)
        self.model = model or settings.ollama_model
        self.base_url = settings.ollama_base_url.rstrip("/")

    def complete_structured(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": f"{system}\n\n{JSON_INSTRUCTION}\n{_schema_hint(schema)}",
                },
                *(message.as_dict() for message in messages),
            ],
            "stream": False,
            # Ollama accepts a JSON Schema here on recent versions and falls back
            # to plain JSON mode on older ones.
            "format": schema,
            "options": {"temperature": temperature, "num_predict": max_output_tokens},
        }
        body, latency_ms = self._post(f"{self.base_url}/api/chat", payload, {})

        content = str((body.get("message") or {}).get("content") or "")
        return ProviderResponse(
            data=extract_json(content),
            provider=self.kind.value,
            model=self.model,
            prompt_tokens=int(body.get("prompt_eval_count") or estimate_tokens(system)),
            completion_tokens=int(body.get("eval_count") or estimate_tokens(content)),
            latency_ms=latency_ms,
        )


class OpenAIProvider(_HttpProvider):
    """OpenAI. **Sends project content off this machine.**"""

    kind = ProviderKind.OPENAI

    def __init__(self, settings: Settings, model: str | None = None) -> None:
        super().__init__(settings)
        if settings.openai_api_key is None:
            raise ProviderError("The OpenAI provider is enabled but no API key is configured.")
        self.model = model or settings.openai_model
        self.base_url = settings.openai_base_url.rstrip("/")
        self._key = settings.openai_api_key.get_secret_value()

    def complete_structured(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"{system}\n\n{JSON_INSTRUCTION}"},
                *(message.as_dict() for message in messages),
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        body, latency_ms = self._post(
            f"{self.base_url}/chat/completions",
            payload,
            {"Authorization": f"Bearer {self._key}"},
        )

        choices = body.get("choices") or []
        if not choices:
            raise ProviderError("OpenAI returned no choices.")
        content = str((choices[0].get("message") or {}).get("content") or "")
        usage = body.get("usage") or {}

        return ProviderResponse(
            data=extract_json(content),
            provider=self.kind.value,
            model=self.model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
        )


class AnthropicProvider(_HttpProvider):
    """Anthropic. **Sends project content off this machine.**"""

    kind = ProviderKind.ANTHROPIC
    api_version = "2023-06-01"

    def __init__(self, settings: Settings, model: str | None = None) -> None:
        super().__init__(settings)
        if settings.anthropic_api_key is None:
            raise ProviderError("The Anthropic provider is enabled but no API key is configured.")
        self.model = model or settings.anthropic_model
        self.base_url = settings.anthropic_base_url.rstrip("/")
        self._key = settings.anthropic_api_key.get_secret_value()

    def complete_structured(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        payload = {
            "model": self.model,
            "system": f"{system}\n\n{JSON_INSTRUCTION}\n{_schema_hint(schema)}",
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
                if message.role != "system"
            ],
            "max_tokens": max_output_tokens,
            "temperature": temperature,
        }
        body, latency_ms = self._post(
            f"{self.base_url}/v1/messages",
            payload,
            {"x-api-key": self._key, "anthropic-version": self.api_version},
        )

        # Only text blocks are read. A thinking block, if the model produced one,
        # is dropped here and never reaches storage.
        text = "".join(
            str(block.get("text") or "")
            for block in (body.get("content") or [])
            if block.get("type") == "text"
        )
        usage = body.get("usage") or {}

        return ProviderResponse(
            data=extract_json(text),
            provider=self.kind.value,
            model=self.model,
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            latency_ms=latency_ms,
        )
