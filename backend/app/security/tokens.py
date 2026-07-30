"""Access tokens.

HS256 JWTs signed with a local secret. There is no external identity provider
and no outbound call at login: the product is local-only.

Every token carries a ``jti`` so ``POST /api/auth/logout`` can revoke it. Without
that, logout would be a client-side gesture and a stolen token would stay valid
until expiry.
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
from app.core.errors import AuthenticationError

#: Identifies tokens minted by this application, checked on decode.
ISSUER = "esco-engineering-city"
AUDIENCE = "esco-engineering-city-api"


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Validated claims from an access token."""

    user_id: uuid.UUID
    jti: str
    issued_at: datetime
    expires_at: datetime
    email: str = ""


def create_access_token(
    *,
    user_id: uuid.UUID,
    email: str,
    settings: Settings,
    issued_at: datetime | None = None,
) -> tuple[str, TokenClaims]:
    """Mint a signed access token and return it with its claims."""
    now = issued_at or utcnow()
    expires_at = now + timedelta(seconds=settings.access_token_ttl_seconds)
    jti = uuid.uuid4().hex

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
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
    return token, TokenClaims(
        user_id=user_id,
        jti=jti,
        issued_at=now,
        expires_at=expires_at,
        email=email,
    )


def decode_access_token(token: str, settings: Settings) -> TokenClaims:
    """Verify and decode a token.

    Raises :class:`AuthenticationError` for anything wrong — expired, wrong
    signature, wrong issuer, malformed. The reason is deliberately coarse in the
    message so a caller cannot use it as an oracle.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            # Pinning the algorithm list is what prevents the classic "alg: none"
            # and HS/RS confusion attacks.
            algorithms=[settings.jwt_algorithm],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Session expired. Sign in again.") from exc
    except InvalidTokenError as exc:
        raise AuthenticationError("Invalid authentication token.") from exc

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Invalid authentication token.") from exc

    return TokenClaims(
        user_id=user_id,
        jti=str(payload["jti"]),
        issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
        expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        email=str(payload.get("email", "")),
    )
