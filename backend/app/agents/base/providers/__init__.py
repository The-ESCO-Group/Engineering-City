"""Model providers and their selection.

Selection is deliberately narrow: a provider must be in
``settings.enabled_providers`` to be constructed at all. There is no fallback
from a failing provider to another one — silently switching to a cloud model
because the local one was unreachable would send project content off this machine
without anyone deciding to.
"""

from __future__ import annotations

from app.agents.base.provider import ModelProvider
from app.agents.base.providers.mock import MOCK_WARNING, MockProvider
from app.agents.base.providers.remote import (
    AnthropicProvider,
    OllamaProvider,
    OpenAIProvider,
)
from app.core.config import Settings
from app.core.enums import ProviderKind
from app.core.errors import ProviderNotEnabledError

__all__ = [
    "MOCK_WARNING",
    "AnthropicProvider",
    "MockProvider",
    "ModelProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "build_provider",
    "provider_status",
]


def build_provider(
    kind: ProviderKind | str,
    settings: Settings,
    *,
    model: str | None = None,
) -> ModelProvider:
    """Construct a provider, or refuse.

    Raises :class:`~app.core.errors.ProviderNotEnabledError` when the provider is
    not enabled, naming what *is*, so an operator can act on the message instead
    of guessing.
    """
    try:
        resolved = ProviderKind(kind)
    except ValueError as exc:
        raise ProviderNotEnabledError(
            f"'{kind}' is not a known provider. Known providers: "
            f"{', '.join(item.value for item in ProviderKind)}.",
            details={"provider": str(kind)},
        ) from exc

    if resolved is ProviderKind.MOCK:
        return MockProvider(model or "mock-deterministic-1")
    if resolved is ProviderKind.OLLAMA:
        return OllamaProvider(settings, model)
    if resolved is ProviderKind.OPENAI:
        return OpenAIProvider(settings, model)
    return AnthropicProvider(settings, model)


def provider_status(settings: Settings) -> list[dict[str, object]]:
    """Which providers are enabled, for the capabilities endpoint.

    ``sends_data_off_machine`` is the property an operator actually needs to know
    before turning one on, so it is stated in the detail rather than left implicit.
    """
    off_machine = {ProviderKind.OPENAI, ProviderKind.ANTHROPIC}
    rows: list[dict[str, object]] = []
    for kind in ProviderKind:
        enabled = settings.provider_enabled(kind)
        detail = "" if enabled else f"Not enabled. Add '{kind.value}' to ESCO_ENABLED_PROVIDERS."
        if enabled and kind in off_machine:
            detail = "Enabled. Sends project content off this machine."
        rows.append(
            {
                "name": f"provider:{kind.value}",
                "available": enabled,
                "provider": kind.value,
                "version": "",
                "detail": detail,
            }
        )
    return rows
