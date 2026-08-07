"""Short-lived, project-scoped credentials for externally hosted SDK agents.

An external agent token is deliberately not a user session.  It can only be
consumed by the external-agent gateway, names one project and one agent
instance, and expires after a single bounded mission.  The gateway also checks
the deploying user's current account and project authority on every request,
so removing that authority revokes the mission without waiting for expiry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from jwt import InvalidTokenError

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import ProviderKind
from app.core.errors import AuthenticationError

ISSUER = "esco-engineering-city"
AUDIENCE = "esco-engineering-city-external-agent"
MAX_EXTERNAL_AGENT_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class ExternalAgentClaims:
    instance_id: uuid.UUID
    project_id: uuid.UUID
    deployed_by_id: uuid.UUID
    provider: ProviderKind
    model: str
    specialty: str
    mission: str
    jti: str
    issued_at: datetime
    expires_at: datetime


def create_external_agent_token(
    *,
    instance_id: uuid.UUID,
    project_id: uuid.UUID,
    deployed_by_id: uuid.UUID,
    provider: ProviderKind,
    model: str,
    specialty: str,
    mission: str,
    ttl_seconds: int,
    settings: Settings,
    issued_at: datetime | None = None,
) -> tuple[str, ExternalAgentClaims]:
    """Mint one short-lived capability credential for an SDK mission."""

    now = issued_at or utcnow()
    bounded_ttl = max(300, min(int(ttl_seconds), MAX_EXTERNAL_AGENT_TTL_SECONDS))
    expires_at = now + timedelta(seconds=bounded_ttl)
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": str(instance_id),
        "project_id": str(project_id),
        "deployed_by_id": str(deployed_by_id),
        "provider": provider.value,
        "model": model[:120],
        "specialty": specialty[:120],
        "mission": mission[:4000],
        "scope": "external_agent_mission",
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": ISSUER,
        "aud": AUDIENCE,
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, ExternalAgentClaims(
        instance_id=instance_id,
        project_id=project_id,
        deployed_by_id=deployed_by_id,
        provider=provider,
        model=model[:120],
        specialty=specialty[:120],
        mission=mission[:4000],
        jti=jti,
        issued_at=now,
        expires_at=expires_at,
    )


def decode_external_agent_token(token: str, settings: Settings) -> ExternalAgentClaims:
    """Verify a capability token without accepting ordinary user JWTs."""

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={
                "require": [
                    "exp",
                    "iat",
                    "sub",
                    "jti",
                    "project_id",
                    "deployed_by_id",
                    "provider",
                    "specialty",
                    "mission",
                    "scope",
                ]
            },
        )
        if payload.get("scope") != "external_agent_mission":
            raise ValueError("wrong scope")
        provider = ProviderKind(str(payload["provider"]))
        instance_id = uuid.UUID(str(payload["sub"]))
        project_id = uuid.UUID(str(payload["project_id"]))
        deployed_by_id = uuid.UUID(str(payload["deployed_by_id"]))
    except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("Invalid external agent credential.") from exc

    return ExternalAgentClaims(
        instance_id=instance_id,
        project_id=project_id,
        deployed_by_id=deployed_by_id,
        provider=provider,
        model=str(payload.get("model", ""))[:120],
        specialty=str(payload.get("specialty", ""))[:120],
        mission=str(payload.get("mission", ""))[:4000],
        jti=str(payload["jti"]),
        issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
        expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
    )


__all__ = [
    "ExternalAgentClaims",
    "create_external_agent_token",
    "decode_external_agent_token",
]
