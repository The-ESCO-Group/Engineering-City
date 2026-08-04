"""Properties that only fail on PostgreSQL.

Every test here guards a difference between SQLite and PostgreSQL that the rest
of the suite cannot see, because the rest of the suite runs on SQLite. That is
the whole hazard: the demo backend is the permissive one, so an incompatibility
is green locally and fails on the shared deployment, where it is expensive.

None of these needs a PostgreSQL server. Each asserts on metadata, on a code
path that fails before any connection is made, or on a throwaway SQLite
database built from the same metadata: where a rule is expressible in portable
DDL, SQLite enforcing it is evidence PostgreSQL will, and it is evidence
available on every developer's machine.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Index, Table, create_engine
from sqlalchemy.exc import IntegrityError

from app.core.enums import EvidenceKind, ImportDependencyKind, ObjectType
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
        # Global defaults have a NULL project, so this catches only the
        # project-scoped half. The backstop is not application-level: the
        # partial unique index uq_model_configurations_global_scope covers the
        # rows this constraint cannot see, and is asserted below.
        "uq_model_configurations_scope",
        # Wholly inert since anchor_id became nullable for the SET NULL
        # cascade, because it is the only nullable column in it. The backstop
        # is uq_finding_evidence_anchored_link, which constrains exactly the
        # rows that still name an anchor; the rows that do not must stay
        # unconstrained, which is why the constraint is not simply widened.
        "uq_finding_evidence_link",
    }
    unexpected = sorted(inert - known)
    assert not unexpected, (
        "unique constraints containing a nullable column, which SQL will not "
        f"enforce: {unexpected}. Either make the column non-nullable with a "
        "sentinel, use a partial index, or add it to the known list with the "
        "application-level backstop named."
    )


# -- the partial unique indexes that carry the rules a UNIQUE cannot -------
#
# Each of these replaces, or completes, a unique constraint that was inert
# because one of its columns is nullable. A partial index is the portable way
# to say it: both PostgreSQL and SQLite index only the rows matching the
# predicate, so the rows that carry the identifying value are constrained and
# the rows that do not are left alone.
#
# They are asserted twice over: once against the metadata, so the declaration
# cannot quietly disappear, and once against a real SQLite database, so what is
# asserted is enforcement rather than intent.
PARTIAL_UNIQUE_INDEXES = {
    "uq_model_configurations_global_scope",
    "uq_finding_evidence_anchored_link",
    "uq_object_evidence_links_anchor",
    "uq_object_evidence_links_requirement",
    "uq_object_evidence_links_decision",
    "uq_object_evidence_links_template",
    "uq_import_dependencies_internal_edge",
    "uq_import_dependencies_external_edge",
}


def _index(name: str) -> Index:
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            if index.name == name:
                return index
    raise AssertionError(f"no index named {name} in the metadata")


@pytest.mark.parametrize("name", sorted(PARTIAL_UNIQUE_INDEXES))
def test_the_declared_backstop_is_a_partial_unique_index_on_both_backends(name: str) -> None:
    """A predicate on one dialect only would leave the other unprotected.

    SQLAlchemy has no dialect-independent spelling for a partial index, so each
    one has to be written twice. Writing only ``postgresql_where`` is the easy
    mistake and it fails in the direction nothing catches: the demo backend
    would build a *full* unique index and start rejecting rows the shared
    deployment accepts.
    """
    index = _index(name)
    assert index.unique, f"{name} is not unique, so it constrains nothing"
    kwargs = index.dialect_kwargs
    assert "postgresql_where" in kwargs, f"{name} has no PostgreSQL predicate"
    assert "sqlite_where" in kwargs, f"{name} has no SQLite predicate"
    assert str(kwargs["postgresql_where"]) == str(kwargs["sqlite_where"]), (
        f"{name} filters different rows on the two backends"
    )


def test_the_migrations_build_the_partial_indexes_with_their_predicates(
    tmp_path: Path,
) -> None:
    """``create_all`` is not the path a deployment takes; the migrations are.

    They are also where the predicate is most easily lost. Alembic's batch mode
    rewrites a table by copying it, and any later revision that batch-alters one
    of these tables recreates its indexes from what it could reflect. A
    predicate dropped there would silently widen a partial unique index into a
    full one -- which, on ``uq_object_evidence_links_template``, would start
    rejecting every second citation of an object that has no template basis, and
    on ``uq_finding_evidence_anchored_link`` every second citation whose anchor
    has been re-parsed away.
    """
    database = tmp_path / "migrated.sqlite3"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={
            **_clean_env(),
            "ESCO_DATABASE_URL": f"sqlite+pysqlite:///{database.as_posix()}",
            "ESCO_ENVIRONMENT": "test",
        },
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    finally:
        connection.close()
    statements = {name: sql or "" for name, sql in rows}

    for name in sorted(PARTIAL_UNIQUE_INDEXES):
        assert name in statements, f"the migrations did not create {name}"
        sql = statements[name]
        assert "UNIQUE" in sql.upper(), f"{name} was created without UNIQUE: {sql}"
        predicate = str(_index(name).dialect_kwargs["sqlite_where"])
        assert f"WHERE {predicate}" in sql, (
            f"{name} lost its predicate somewhere in the migration chain; "
            f"it is now {sql!r}, which constrains rows it must not"
        )


@pytest.fixture
def sqlite_schema() -> Iterator[Connection]:
    """A throwaway database built from the ORM metadata.

    Foreign keys are left off (SQLite's default), so a test can insert a single
    row naming ids that do not exist and assert on the one rule it is about.
    """
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def test_a_second_global_model_configuration_is_rejected(sqlite_schema: Connection) -> None:
    """Two global defaults for one agent kind break every later read.

    ``uq_model_configurations_scope`` does not stop it: ``project_id`` is NULL
    for exactly the global rows. The upsert route is a select-then-insert with
    no lock and reads the scope with ``scalar_one_or_none()``, so once a pair
    exists every PUT and GET of that scope raises ``MultipleResultsFound`` and
    only direct SQL can repair it.
    """
    rows = _table("model_configurations")
    values = {"project_id": None, "agent_kind": "plc_review", "provider": "mock", "model": "m"}
    sqlite_schema.execute(rows.insert(), values)

    with pytest.raises(IntegrityError):
        sqlite_schema.execute(rows.insert(), {**values, "model": "other"})


def test_two_global_configurations_for_different_agent_kinds_are_still_allowed(
    sqlite_schema: Connection,
) -> None:
    """The index must not collapse the global scope into a single row."""
    rows = _table("model_configurations")
    base = {"project_id": None, "provider": "mock", "model": "m"}
    sqlite_schema.execute(rows.insert(), {**base, "agent_kind": "plc_review"})
    sqlite_schema.execute(rows.insert(), {**base, "agent_kind": "senior_review"})

    count = sqlite_schema.execute(rows.select()).fetchall()
    assert len(count) == 2


def test_one_object_cannot_cite_the_same_anchor_twice(sqlite_schema: Connection) -> None:
    """The citation table's only uniqueness, and it used to be unenforceable.

    Two agents' change sets merged concurrently both run the check-then-insert
    in ``engineering/merge.py`` with no lock, both find nothing and both
    insert. The old six-column constraint rejected neither, because
    ``requirement_id`` and ``decision_id`` are NULL in both rows. The result is
    a doubled citation, which makes a traceability report overstate how much
    independent basis an object has.
    """
    links = _table("object_evidence_links")
    object_id = uuid.uuid4()
    anchor_id = uuid.uuid4()
    values = {
        "project_id": uuid.uuid4(),
        "object_type": ObjectType.TAG,
        "object_id": object_id,
        "anchor_id": anchor_id,
        "basis_kind": EvidenceKind.DIRECT,
    }
    sqlite_schema.execute(links.insert(), values)

    with pytest.raises(IntegrityError):
        sqlite_schema.execute(links.insert(), {**values, "note": "the same anchor again"})


def test_a_second_citation_of_one_anchor_is_rejected_however_it_is_classified(
    sqlite_schema: Connection,
) -> None:
    """``basis_kind`` is deliberately outside the index.

    Citing anchor A for object O twice is a duplicate whether the second copy
    calls itself DIRECT or INFERENCE, and the merge engine's own dedupe check
    ignores the kind for the same reason.
    """
    links = _table("object_evidence_links")
    values = {
        "project_id": uuid.uuid4(),
        "object_type": ObjectType.TAG,
        "object_id": uuid.uuid4(),
        "anchor_id": uuid.uuid4(),
        "basis_kind": EvidenceKind.DIRECT,
    }
    sqlite_schema.execute(links.insert(), values)

    with pytest.raises(IntegrityError):
        sqlite_schema.execute(links.insert(), {**values, "basis_kind": EvidenceKind.INFERENCE})


def test_two_objects_may_cite_the_same_anchor(sqlite_schema: Connection) -> None:
    """One anchor is the basis for as many objects as it supports."""
    links = _table("object_evidence_links")
    anchor_id = uuid.uuid4()
    base = {
        "project_id": uuid.uuid4(),
        "object_type": ObjectType.TAG,
        "anchor_id": anchor_id,
        "basis_kind": EvidenceKind.DIRECT,
    }
    sqlite_schema.execute(links.insert(), {**base, "object_id": uuid.uuid4()})
    sqlite_schema.execute(links.insert(), {**base, "object_id": uuid.uuid4()})

    assert len(sqlite_schema.execute(links.select()).fetchall()) == 2


def test_one_object_cannot_cite_the_same_template_twice(sqlite_schema: Connection) -> None:
    """``template_id`` is '' rather than NULL, so its index is predicated on that."""
    links = _table("object_evidence_links")
    values = {
        "project_id": uuid.uuid4(),
        "object_type": ObjectType.LOGIC_RUNG,
        "object_id": uuid.uuid4(),
        "template_id": "esco.motor.standard@1.0.0",
        "basis_kind": EvidenceKind.TEMPLATE_RULE,
    }
    sqlite_schema.execute(links.insert(), values)

    with pytest.raises(IntegrityError):
        sqlite_schema.execute(links.insert(), values)


def test_a_repeated_external_import_prerequisite_is_rejected(sqlite_schema: Connection) -> None:
    """An external prerequisite is identified by its label, not by a null id.

    A routine that references a customer-owned UDT N times used to produce N
    identical dependency rows: ``depends_on_artifact_id`` is NULL on every one
    of them, so the constraint named for that column rejected none. The
    customer-facing import instructions then listed the same prerequisite N
    times.
    """
    edges = _table("import_dependencies")
    values = {
        "project_id": uuid.uuid4(),
        "release_candidate_id": uuid.uuid4(),
        "artifact_id": uuid.uuid4(),
        "depends_on_artifact_id": None,
        "depends_on_label": "User-defined type UDT_ConveyorState",
        "kind": ImportDependencyKind.REQUIRES_DATA_TYPE,
    }
    sqlite_schema.execute(edges.insert(), values)

    with pytest.raises(IntegrityError):
        sqlite_schema.execute(edges.insert(), {**values, "reason": "the same prerequisite"})


def test_a_repeated_internal_import_prerequisite_is_still_rejected(
    sqlite_schema: Connection,
) -> None:
    """The half the old constraint did cover, kept when it was replaced."""
    edges = _table("import_dependencies")
    values = {
        "project_id": uuid.uuid4(),
        "release_candidate_id": uuid.uuid4(),
        "artifact_id": uuid.uuid4(),
        "depends_on_artifact_id": uuid.uuid4(),
        "depends_on_label": "User-defined type UDT_ConveyorState",
        "kind": ImportDependencyKind.REQUIRES_DATA_TYPE,
    }
    sqlite_schema.execute(edges.insert(), values)

    with pytest.raises(IntegrityError):
        sqlite_schema.execute(edges.insert(), values)


def test_two_different_external_prerequisites_of_one_artifact_are_allowed(
    sqlite_schema: Connection,
) -> None:
    """The dedupe is per prerequisite, not per artifact."""
    edges = _table("import_dependencies")
    base = {
        "project_id": uuid.uuid4(),
        "release_candidate_id": uuid.uuid4(),
        "artifact_id": uuid.uuid4(),
        "depends_on_artifact_id": None,
        "kind": ImportDependencyKind.REQUIRES_DATA_TYPE,
    }
    sqlite_schema.execute(edges.insert(), {**base, "depends_on_label": "UDT_ConveyorState"})
    sqlite_schema.execute(edges.insert(), {**base, "depends_on_label": "UDT_MotorState"})

    assert len(sqlite_schema.execute(edges.select()).fetchall()) == 2
