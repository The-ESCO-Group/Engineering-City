"""Properties that only fail on PostgreSQL.

Every test here guards a difference between SQLite and PostgreSQL that the rest
of the suite cannot see, because the rest of the suite runs on SQLite. That is
the whole hazard: the demo backend is the permissive one, so an incompatibility
is green locally and fails on the shared deployment, where it is expensive.

None of these needs a PostgreSQL server. Each asserts on metadata or on a code
path that fails before any connection is made.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.models import Base

REPO_ROOT = Path(__file__).resolve().parents[2]

#: PostgreSQL truncates an identifier at 63 *bytes*, silently. Two names that
#: collide after truncation become one constraint; a name that survives
#: truncation differs from the one SQLite stored, so a later migration that
#: drops it by name works in demo mode and fails on the shared deployment.
#:
#: The bound is 59 rather than 63 to leave room for the ``_1`` suffixes Alembic
#: appends during a batch rebuild.
MAX_IDENTIFIER_CHARS = 59


def _identifiers() -> list[tuple[str, str]]:
    """Every constraint and index name in the schema, with its table."""
    found: list[tuple[str, str]] = []
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if constraint.name and not isinstance(constraint.name, str):
                continue
            if constraint.name:
                found.append((table.name, str(constraint.name)))
        for index in table.indexes:
            if index.name:
                found.append((table.name, str(index.name)))
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.constraint is not None and fk.constraint.name:
                    found.append((table.name, str(fk.constraint.name)))
    return found


def test_the_schema_is_large_enough_for_this_to_be_a_real_check() -> None:
    """Guards against the sweep silently finding nothing."""
    assert len(_identifiers()) > 400


def test_no_identifier_would_be_truncated_by_postgresql() -> None:
    """A truncated name is a name the two backends disagree about.

    SQLAlchemy does not raise for a name produced by a naming convention — it
    quietly rewrites it to 59 characters plus an MD5 fragment on PostgreSQL,
    while SQLite keeps the full name. Nothing in a SQLite test run can notice.
    """
    over = sorted(
        {
            (name, len(name), table)
            for table, name in _identifiers()
            if len(name) > MAX_IDENTIFIER_CHARS
        }
    )
    assert not over, "identifiers PostgreSQL would truncate: " + ", ".join(
        f"{name} ({length} chars, on {table})" for name, length, table in over
    )


def test_no_two_identifiers_collide_after_truncation() -> None:
    """The worse half of the same problem: two constraints becoming one."""
    seen: dict[str, str] = {}
    collisions: list[str] = []
    for _table, name in _identifiers():
        key = name[:63]
        if key in seen and seen[key] != name:
            collisions.append(f"{seen[key]} / {name}")
        seen.setdefault(key, name)
    assert not collisions, collisions


def test_every_string_column_declares_a_length() -> None:
    """PostgreSQL requires one for VARCHAR; SQLite does not care.

    A bare ``String`` maps to unbounded ``TEXT`` on PostgreSQL, which works but
    silently discards the bound the model was documenting — and on some
    backends fails outright.
    """
    from sqlalchemy import String

    unbounded: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            column_type = column.type
            if isinstance(column_type, String) and type(column_type) is String:
                if column_type.length is None:
                    unbounded.append(f"{table.name}.{column.name}")
    assert not unbounded, unbounded


@pytest.mark.parametrize(
    "password",
    [
        "p%40ssw0rd",  # @
        "p%2Fssw0rd",  # /
        "p%21ssw0rd",  # !
        "p%25ssw0rd",  # a literal percent
    ],
)
def test_alembic_survives_a_percent_encoded_database_password(password: str) -> None:
    """The migration runner must not choke on a URL the app connects with.

    Alembic keeps ``sqlalchemy.url`` in a ``configparser``, which applies
    pyformat interpolation. A PostgreSQL password containing any character RFC
    3986 requires to be percent-encoded arrives as ``%40``/``%2F``/``%21`` and
    configparser rejects the whole string before a single migration runs.

    The asymmetry is what makes it dangerous: SQLAlchemy parses the same URL
    correctly, so the API and the worker start and connect fine. Only the tool
    needed to create the schema dies. SQLite URLs contain no ``%``, so demo mode
    never sees it.

    ``alembic heads`` reads the configuration and the migration scripts without
    opening a connection, so this asserts the parse rather than the database.
    """
    url = f"postgresql+psycopg://esco:{password}@db.internal:5432/esco"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=REPO_ROOT,
        env={
            **_clean_env(),
            "ESCO_DATABASE_URL": url,
            "ESCO_ENVIRONMENT": "test",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert "interpolation" not in combined.lower(), combined
    assert result.returncode == 0, combined


def _clean_env() -> dict[str, str]:
    import os

    return {k: v for k, v in os.environ.items() if not k.startswith("ESCO_")}


def test_a_nullable_column_inside_a_unique_constraint_is_declared_not_relied_on() -> None:
    """SQL treats NULL as distinct, so such a constraint does not constrain.

    This is not a defect in itself — several are deliberate — but every one is a
    place where the database provides no guarantee and something in the
    application must. Listing them here means adding one is a conscious act
    rather than an accident, and the list doubles as the set a reviewer should
    check has an application-level backstop.
    """
    from sqlalchemy import UniqueConstraint

    inert: set[str] = set()
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            if any(column.nullable for column in constraint.columns):
                inert.add(str(constraint.name))

    known = {
        # Controller-scoped tags have a NULL program, so this catches only the
        # program-scoped half. TagValidationCode.DUPLICATE_NAME is the backstop.
        "uq_tags_project_id_scope_name",
    }
    unexpected = sorted(inert - known)
    assert not unexpected, (
        "unique constraints containing a nullable column, which SQL will not "
        f"enforce: {unexpected}. Either make the column non-nullable with a "
        "sentinel, use a partial index, or add it to the known list with the "
        "application-level backstop named."
    )
