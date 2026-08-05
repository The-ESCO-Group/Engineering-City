"""Capability-token isolation and expiry for externally hosted agents."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import ProviderKind
from app.core.errors import AuthenticationError
from app.security.external_agent_tokens import (
    create_external_agent_token,
    decode_external_agent_token,
)


def _token(settings: Settings, *, issued_offset: timedelta = timedelta()) -> str:
    token, _ = create_external_agent_token(
        instance_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        deployed_by_id=uuid.uuid4(),
        provider=ProviderKind.OPENAI,
        model="sdk-test-model",
        specialty="Evidence reviewer",
        mission="Review only the granted evidence.",
        ttl_seconds=300,
        settings=settings,
        issued_at=utcnow() + issued_offset,
    )
    return token


def test_external_agent_token_round_trips_its_scoped_claims(settings: Settings) -> None:
    token = _token(settings)

    claims = decode_external_agent_token(token, settings)

    assert claims.provider is ProviderKind.OPENAI
    assert claims.model == "sdk-test-model"
    assert claims.specialty == "Evidence reviewer"
    assert claims.mission == "Review only the granted evidence."
    assert claims.expires_at > claims.issued_at


def test_expired_external_agent_token_is_rejected(settings: Settings) -> None:
    token = _token(settings, issued_offset=timedelta(minutes=-10))

    with pytest.raises(AuthenticationError, match="Invalid external agent credential"):
        decode_external_agent_token(token, settings)


def test_tampered_external_agent_token_is_rejected(settings: Settings) -> None:
    token = _token(settings)
    header, payload, signature = token.split(".")
    altered = ("A" if signature[0] != "A" else "B") + signature[1:]
    tampered = ".".join((header, payload, altered))

    with pytest.raises(AuthenticationError, match="Invalid external agent credential"):
        decode_external_agent_token(tampered, settings)
