"""Password hashing.

Argon2id via ``argon2-cffi``. Chosen over bcrypt for its memory-hardness and
because it has no silent 72-byte input truncation.

Plaintext passwords are never logged, never stored, and never included in an
audit entry or an error message.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

#: Interactive-login parameters: ~64 MiB and ~2 passes. Comfortably above the
#: RFC 9106 second recommended option while staying responsive on a workstation.
_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=64 * 1024,
    parallelism=2,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

#: Minimum length accepted when setting a password. Length is the only
#: composition rule enforced: complexity rules push users toward predictable
#: substitutions without measurably improving resistance to guessing.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024


class PasswordPolicyError(ValueError):
    """Raised when a proposed password fails the policy."""


def validate_password(password: str) -> None:
    """Raise :class:`PasswordPolicyError` if the password is unacceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        # Bounded to keep a very long input from becoming a cheap way to burn
        # server memory in the KDF.
        raise PasswordPolicyError(f"password must be at most {MAX_PASSWORD_LENGTH} characters")


def hash_password(password: str) -> str:
    """Hash a password after checking it against the policy."""
    validate_password(password)
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time-ish verification. Returns ``False`` rather than raising.

    A malformed stored hash is treated as a failed verification, not an
    exception: a corrupted row must not turn into a 500 that distinguishes it
    from a wrong password.
    """
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash used weaker parameters than the current policy.

    Callers re-hash on the next successful login so parameters can be raised
    over time without a forced password reset.
    """
    if not password_hash:
        return True
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


#: A valid Argon2 hash of a value nobody can supply. Verified against when an
#: account does not exist so that a login attempt for an unknown user costs the
#: same as one for a known user, closing the timing side-channel that would
#: otherwise enumerate accounts.
DUMMY_HASH = _hasher.hash("dummy-password-for-timing-equalisation")


def waste_time_like_a_real_verification() -> None:
    """Spend the same work as a real verification, for unknown accounts."""
    verify_password("wrong", DUMMY_HASH)
