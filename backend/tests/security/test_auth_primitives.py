"""Authentication primitives: password hashing and access tokens.

These two modules are the whole of the login trust boundary, so their failure
modes are asserted directly rather than only through the ``/api/auth`` routes.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, timedelta
from typing import Any

import jwt
import pytest
from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import VerifyMismatchError
from argon2.low_level import Type
from pydantic import SecretStr

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.security.passwords import (
    DUMMY_HASH,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
    waste_time_like_a_real_verification,
)
from app.security.tokens import (
    AUDIENCE,
    ISSUER,
    create_access_token,
    decode_access_token,
)

GOOD_PASSWORD = "synthetic-password-123456"

#: Stored-hash shapes a corrupted or hand-edited row could plausibly contain.
GARBAGE_HASHES = [
    "not-a-hash",
    "$argon2id$",
    "$argon2id$v=19$m=65536,t=2,p=2$AAAA$AAAA",
    "$2b$12$KIXQ0pFhF2Wq4hM0y7Zj1eYbG0Xq0pFhF2Wq4hM0y7Zj1eYbG0Xq0",
    "   ",
]


# ---------------------------------------------------------------------------
# passwords.py
# ---------------------------------------------------------------------------


def test_hash_then_verify_accepts_the_original_password() -> None:
    stored = hash_password(GOOD_PASSWORD)
    assert stored != GOOD_PASSWORD
    assert GOOD_PASSWORD not in stored
    assert stored.startswith("$argon2id$")
    assert verify_password(GOOD_PASSWORD, stored) is True


def test_verify_rejects_a_wrong_password() -> None:
    stored = hash_password(GOOD_PASSWORD)
    assert verify_password(GOOD_PASSWORD + "x", stored) is False
    assert verify_password(GOOD_PASSWORD.upper(), stored) is False
    assert verify_password("", stored) is False


def test_verify_returns_false_for_an_empty_stored_hash() -> None:
    """A user row with no hash (SSO placeholder, failed write) must not log in."""
    assert verify_password(GOOD_PASSWORD, "") is False


@pytest.mark.parametrize("stored", GARBAGE_HASHES)
def test_verify_returns_false_for_a_malformed_stored_hash(stored: str) -> None:
    """A corrupted row is a failed login, not a 500 that identifies the row."""
    assert verify_password(GOOD_PASSWORD, stored) is False


def test_hashes_of_the_same_password_differ_because_they_are_salted() -> None:
    """Equal hashes would let an attacker spot shared passwords from a dump."""
    first = hash_password(GOOD_PASSWORD)
    second = hash_password(GOOD_PASSWORD)
    assert first != second
    assert verify_password(GOOD_PASSWORD, first) is True
    assert verify_password(GOOD_PASSWORD, second) is True


def test_password_shorter_than_the_minimum_is_refused() -> None:
    too_short = "a" * (MIN_PASSWORD_LENGTH - 1)
    with pytest.raises(PasswordPolicyError, match=str(MIN_PASSWORD_LENGTH)):
        validate_password(too_short)
    with pytest.raises(PasswordPolicyError):
        hash_password(too_short)


def test_password_exactly_at_the_minimum_is_accepted() -> None:
    """The boundary is inclusive; an off-by-one here locks out valid users."""
    at_minimum = "a" * MIN_PASSWORD_LENGTH
    validate_password(at_minimum)
    assert verify_password(at_minimum, hash_password(at_minimum)) is True


def test_password_longer_than_the_maximum_is_refused() -> None:
    """An unbounded input is a cheap way to burn server memory in the KDF."""
    too_long = "a" * (MAX_PASSWORD_LENGTH + 1)
    with pytest.raises(PasswordPolicyError, match=str(MAX_PASSWORD_LENGTH)):
        validate_password(too_long)
    with pytest.raises(PasswordPolicyError):
        hash_password(too_long)


def test_password_exactly_at_the_maximum_is_accepted() -> None:
    at_maximum = "a" * MAX_PASSWORD_LENGTH
    validate_password(at_maximum)
    assert verify_password(at_maximum, hash_password(at_maximum)) is True


def test_long_and_non_ascii_passwords_are_not_truncated() -> None:
    """Argon2 was chosen over bcrypt for this: no silent 72-byte cut-off."""
    base = "pässwörd-with-üñïcode-" + "z" * 80
    stored = hash_password(base)
    assert verify_password(base, stored) is True
    assert verify_password(base[:72], stored) is False


def test_needs_rehash_is_false_for_a_freshly_minted_hash() -> None:
    assert needs_rehash(hash_password(GOOD_PASSWORD)) is False


@pytest.mark.parametrize("stored", ["", *GARBAGE_HASHES])
def test_needs_rehash_is_true_for_a_missing_or_malformed_hash(stored: str) -> None:
    """Unreadable parameters must be treated as stale, never as up to date."""
    assert needs_rehash(stored) is True


def test_needs_rehash_is_true_for_a_hash_made_with_weaker_parameters() -> None:
    """This is what lets parameters be raised without a forced password reset."""
    legacy = PasswordHasher(
        time_cost=1,
        memory_cost=8,
        parallelism=1,
        hash_len=16,
        salt_len=8,
        type=Type.ID,
    ).hash(GOOD_PASSWORD)
    assert verify_password(GOOD_PASSWORD, legacy) is True
    assert needs_rehash(legacy) is True


def test_dummy_hash_carries_the_same_parameters_as_a_real_stored_hash() -> None:
    """Cheaper dummy parameters would make unknown accounts measurably faster."""
    assert DUMMY_HASH.startswith("$argon2id$")
    assert extract_parameters(DUMMY_HASH) == extract_parameters(hash_password(GOOD_PASSWORD))
    assert needs_rehash(DUMMY_HASH) is False


def test_verifying_against_the_dummy_hash_runs_the_kdf_to_a_mismatch() -> None:
    """A mismatch (not an invalid-hash reject) proves real work was done, so the
    unknown-account path costs the same as a known one."""
    with pytest.raises(VerifyMismatchError):
        PasswordHasher().verify(DUMMY_HASH, "wrong")
    assert verify_password("wrong", DUMMY_HASH) is False
    assert waste_time_like_a_real_verification() is None


# ---------------------------------------------------------------------------
# tokens.py
# ---------------------------------------------------------------------------


#: A second signing key. Long enough that PyJWT does not warn about HMAC length.
OTHER_SECRET = "a-completely-different-secret-of-ample-length"


def _foreign_settings(settings: Settings) -> Settings:
    """The same configuration under a different signing key."""
    return settings.model_copy(update={"jwt_secret": SecretStr(OTHER_SECRET)})


def _sign(payload: dict[str, Any], settings: Settings) -> str:
    return jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def _reference_payload(settings: Settings) -> dict[str, Any]:
    """The exact claim set the application mints, for negative tests to mutate."""
    now = utcnow()
    return {
        "sub": str(uuid.uuid4()),
        "email": "operator@test.internal",
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.access_token_ttl_seconds)).timestamp()),
        "iss": ISSUER,
        "aud": AUDIENCE,
    }


def _segment(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_payload_segment(token: str) -> dict[str, Any]:
    segment = token.split(".")[1]
    padded = segment + "=" * (-len(segment) % 4)
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
    return decoded


def test_access_token_round_trips_user_id_email_and_jti(settings: Settings) -> None:
    user_id = uuid.uuid4()
    token, minted = create_access_token(
        user_id=user_id,
        email="engineer@test.internal",
        settings=settings,
    )

    decoded = decode_access_token(token, settings)
    assert decoded.user_id == user_id
    assert decoded.email == "engineer@test.internal"
    assert decoded.jti == minted.jti
    assert decoded.issued_at.utcoffset() == timedelta(0)
    assert decoded.expires_at.utcoffset() == timedelta(0)


def test_token_lifetime_matches_the_configured_ttl(settings: Settings) -> None:
    issued_at = utcnow()
    token, minted = create_access_token(
        user_id=uuid.uuid4(),
        email="engineer@test.internal",
        settings=settings,
        issued_at=issued_at,
    )

    ttl = timedelta(seconds=settings.access_token_ttl_seconds)
    assert minted.issued_at == issued_at
    assert minted.expires_at == issued_at + ttl
    decoded = decode_access_token(token, settings)
    assert decoded.expires_at - decoded.issued_at == ttl


def test_each_token_gets_its_own_jti(settings: Settings) -> None:
    """Logout revokes by jti, so a reused jti would sign every session out."""
    _, first = create_access_token(user_id=uuid.uuid4(), email="a@test.internal", settings=settings)
    _, second = create_access_token(
        user_id=uuid.uuid4(), email="a@test.internal", settings=settings
    )
    assert first.jti != second.jti


def test_reference_payload_used_by_the_negative_tests_is_accepted(settings: Settings) -> None:
    """Guards the mutations below: only the mutation may cause the rejection."""
    payload = _reference_payload(settings)
    decoded = decode_access_token(_sign(payload, settings), settings)
    assert str(decoded.user_id) == payload["sub"]
    assert decoded.jti == payload["jti"]


def test_token_signed_with_a_different_secret_is_rejected(settings: Settings) -> None:
    other = _foreign_settings(settings)
    token, _ = create_access_token(
        user_id=uuid.uuid4(),
        email="engineer@test.internal",
        settings=other,
    )

    with pytest.raises(AuthenticationError):
        decode_access_token(token, settings)


def test_token_with_a_tampered_payload_is_rejected(settings: Settings) -> None:
    """Swapping the subject for another user's id must not survive the signature."""
    token, _ = create_access_token(
        user_id=uuid.uuid4(),
        email="engineer@test.internal",
        settings=settings,
    )
    header, _payload, signature = token.split(".")
    forged = _decode_payload_segment(token)
    forged["sub"] = str(uuid.uuid4())
    forged["email"] = "admin@test.internal"

    with pytest.raises(AuthenticationError):
        decode_access_token(f"{header}.{_segment(forged)}.{signature}", settings)


def test_expired_token_is_rejected_and_says_so(settings: Settings) -> None:
    """The one failure the message may distinguish: the UI must offer a re-login."""
    issued_at = utcnow() - timedelta(seconds=settings.access_token_ttl_seconds + 60)
    token, _ = create_access_token(
        user_id=uuid.uuid4(),
        email="engineer@test.internal",
        settings=settings,
        issued_at=issued_at,
    )

    with pytest.raises(AuthenticationError) as caught:
        decode_access_token(token, settings)
    assert "expired" in caught.value.message.lower()


def test_token_from_another_issuer_is_rejected(settings: Settings) -> None:
    payload = {**_reference_payload(settings), "iss": "some-other-product"}
    with pytest.raises(AuthenticationError):
        decode_access_token(_sign(payload, settings), settings)


def test_token_for_another_audience_is_rejected(settings: Settings) -> None:
    """A token minted for a different service must not be replayable here."""
    payload = {**_reference_payload(settings), "aud": "some-other-api"}
    with pytest.raises(AuthenticationError):
        decode_access_token(_sign(payload, settings), settings)


def test_unsigned_alg_none_token_is_rejected(settings: Settings) -> None:
    """The classic JWT bypass: a caller declaring the token needs no signature."""
    payload = _reference_payload(settings)
    unsigned = f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{_segment(payload)}."

    with pytest.raises(AuthenticationError):
        decode_access_token(unsigned, settings)


@pytest.mark.parametrize("claim", ["exp", "iat", "sub", "jti"])
def test_token_missing_a_required_claim_is_rejected(settings: Settings, claim: str) -> None:
    payload = _reference_payload(settings)
    del payload[claim]

    with pytest.raises(AuthenticationError):
        decode_access_token(_sign(payload, settings), settings)


def test_token_with_a_non_uuid_subject_is_rejected(settings: Settings) -> None:
    """``sub`` is looked up as a user id; a non-uuid must fail auth, not the query."""
    payload = {**_reference_payload(settings), "sub": "administrator"}
    with pytest.raises(AuthenticationError):
        decode_access_token(_sign(payload, settings), settings)


def test_token_without_an_email_claim_still_decodes(settings: Settings) -> None:
    """``email`` is display-only; identity comes from ``sub``."""
    payload = _reference_payload(settings)
    del payload["email"]

    decoded = decode_access_token(_sign(payload, settings), settings)
    assert decoded.email == ""
    assert str(decoded.user_id) == payload["sub"]


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "a.b.c",
        "Bearer eyJhbGciOiJIUzI1NiJ9.e30.",
        "eyJhbGciOiJIUzI1NiJ9",
    ],
)
def test_malformed_token_strings_are_rejected(settings: Settings, token: str) -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token(token, settings)


def test_every_failure_surfaces_as_an_http_401_app_error(settings: Settings) -> None:
    """A leaked ``jwt`` exception would become an opaque 500 instead of a 401."""
    for bad_token in _rejected_tokens(settings):
        with pytest.raises(AuthenticationError) as caught:
            decode_access_token(bad_token, settings)
        assert not isinstance(caught.value, jwt.PyJWTError)
        assert caught.value.status_code == 401
        assert caught.value.code == "authentication_required"


def test_failure_messages_do_not_reveal_which_check_failed(settings: Settings) -> None:
    """Distinguishable messages would turn decode into an oracle for forgery."""
    messages = set()
    for bad_token in _rejected_tokens(settings):
        with pytest.raises(AuthenticationError) as caught:
            decode_access_token(bad_token, settings)
        messages.add(caught.value.message)
    assert messages == {"Invalid authentication token."}


def _rejected_tokens(settings: Settings) -> list[str]:
    """One token per non-expiry failure mode, all otherwise well formed."""
    other = _foreign_settings(settings)
    no_jti = _reference_payload(settings)
    del no_jti["jti"]
    return [
        "not-a-token",
        _sign(_reference_payload(settings), other),
        _sign({**_reference_payload(settings), "iss": "some-other-product"}, settings),
        _sign({**_reference_payload(settings), "aud": "some-other-api"}, settings),
        _sign({**_reference_payload(settings), "sub": "administrator"}, settings),
        _sign(no_jti, settings),
        f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{_segment(_reference_payload(settings))}.",
    ]


def test_expiry_and_generic_failures_do_not_share_a_message(settings: Settings) -> None:
    """Expiry is deliberately the only distinguishable reason."""
    expired, _ = create_access_token(
        user_id=uuid.uuid4(),
        email="engineer@test.internal",
        settings=settings,
        issued_at=utcnow() - timedelta(seconds=settings.access_token_ttl_seconds + 60),
    )

    with pytest.raises(AuthenticationError) as expiry:
        decode_access_token(expired, settings)
    with pytest.raises(AuthenticationError) as generic:
        decode_access_token("not-a-token", settings)
    assert expiry.value.message != generic.value.message


def test_claims_are_immutable_once_decoded(settings: Settings) -> None:
    """Callers pass claims around during a request; nothing may rewrite them."""
    token, _ = create_access_token(
        user_id=uuid.uuid4(),
        email="engineer@test.internal",
        settings=settings,
    )
    claims = decode_access_token(token, settings)

    with pytest.raises((AttributeError, TypeError)):
        claims.user_id = uuid.uuid4()  # type: ignore[misc]


def test_decoded_expiry_is_utc_and_in_the_future_for_a_fresh_token(settings: Settings) -> None:
    token, _ = create_access_token(
        user_id=uuid.uuid4(),
        email="engineer@test.internal",
        settings=settings,
    )
    claims = decode_access_token(token, settings)

    assert claims.expires_at.tzinfo is UTC
    assert claims.expires_at > utcnow()
