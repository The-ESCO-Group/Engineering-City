"""Alembic history and the idempotent startup sequence.

The migration is the only thing that builds a real deployment's schema, so it is
exercised through the ``alembic`` CLI exactly as an operator would run it, in a
subprocess against a throwaway SQLite file. Bootstrap runs on every boot of both
the API and the worker, so re-running it must never duplicate or destroy data.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import MetaData, Table, create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from app.agents.base.plugins import PluginRegistry
from app.core.bootstrap import sync_agent_definitions
from app.core.config import Settings
from app.core.enums import AgentKind, ChangeSetStatus, RoleName
from app.core.services import users as user_service
from app.models import Base, ChangeOperation, ChangeSet, MergeResult, Project
from app.models.agent import AgentDefinition
from app.models.user import Role, User

REPO_ROOT = Path(__file__).resolve().parents[2]

CORE_KINDS = (
    AgentKind.PROJECT_MANAGER,
    AgentKind.PLC_REVIEW,
    AgentKind.INTERLOCK_REVIEW,
    AgentKind.SENIOR_REVIEW,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path.as_posix()}"


def _run_alembic(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
    """Invoke the alembic CLI the way an operator does, against ``database_url``."""
    env = {**os.environ, "ESCO_DATABASE_URL": database_url, "ESCO_ENVIRONMENT": "test"}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _diagnose(result: subprocess.CompletedProcess[str], label: str) -> str:
    return (
        f"{label} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def _table_names(path: Path) -> list[str]:
    engine = create_engine(_sqlite_url(path))
    try:
        return sorted(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _stamped_revisions(path: Path) -> list[str]:
    engine = create_engine(_sqlite_url(path))
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version")).all()
    finally:
        engine.dispose()
    return [str(row[0]) for row in rows]


@pytest.fixture
def migration_db(tmp_path: Path) -> Path:
    """Path to a SQLite file that does not exist yet, for the CLI to create."""
    return tmp_path / "migrations.sqlite3"


@pytest.mark.slow
def test_upgrade_head_creates_the_schema_and_stamps_exactly_one_revision(
    migration_db: Path,
) -> None:
    """An unstamped upgrade leaves the next upgrade a silent no-op, so assert the stamp."""
    result = _run_alembic("upgrade", "head", database_url=_sqlite_url(migration_db))
    assert result.returncode == 0, _diagnose(result, "alembic upgrade head")

    tables = _table_names(migration_db)
    assert "alembic_version" in tables
    assert "users" in tables
    assert "projects" in tables

    assert len(_stamped_revisions(migration_db)) == 1, _stamped_revisions(migration_db)


@pytest.mark.slow
def test_alembic_check_reports_no_drift_against_the_orm_models(migration_db: Path) -> None:
    """Autogenerate drift would otherwise ship silently and break the next deployment."""
    url = _sqlite_url(migration_db)
    upgrade = _run_alembic("upgrade", "head", database_url=url)
    assert upgrade.returncode == 0, _diagnose(upgrade, "alembic upgrade head")

    check = _run_alembic("check", database_url=url)
    assert check.returncode == 0, _diagnose(check, "alembic check")
    assert "No new upgrade operations detected." in check.stdout


@pytest.mark.slow
def test_downgrade_base_removes_every_application_table_and_upgrade_works_again(
    migration_db: Path,
) -> None:
    """A downgrade that leaves debris behind cannot be used to recover a bad release."""
    url = _sqlite_url(migration_db)
    upgrade = _run_alembic("upgrade", "head", database_url=url)
    assert upgrade.returncode == 0, _diagnose(upgrade, "alembic upgrade head")
    migrated = _table_names(migration_db)

    downgrade = _run_alembic("downgrade", "base", database_url=url)
    assert downgrade.returncode == 0, _diagnose(downgrade, "alembic downgrade base")
    assert _table_names(migration_db) == ["alembic_version"]
    assert _stamped_revisions(migration_db) == []

    again = _run_alembic("upgrade", "head", database_url=url)
    assert again.returncode == 0, _diagnose(again, "alembic upgrade head (after downgrade)")
    assert _table_names(migration_db) == migrated
    assert len(_stamped_revisions(migration_db)) == 1


@pytest.mark.slow
def test_migration_creates_the_same_tables_as_the_orm_metadata(migration_db: Path) -> None:
    """The tests use create_all; production uses the migration. They must agree."""
    result = _run_alembic("upgrade", "head", database_url=_sqlite_url(migration_db))
    assert result.returncode == 0, _diagnose(result, "alembic upgrade head")

    migrated = [name for name in _table_names(migration_db) if name != "alembic_version"]

    reference = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(reference)
        from_metadata = sorted(inspect(reference).get_table_names())
    finally:
        reference.dispose()

    assert migrated == from_metadata


@pytest.mark.slow
def test_proposal_split_migration_preserves_legacy_rows_and_round_trips(
    migration_db: Path,
) -> None:
    """Populated databases keep known facts without fabricating overwritten proposals."""
    url = _sqlite_url(migration_db)
    previous = _run_alembic("upgrade", "38af6b21dc04", database_url=url)
    assert previous.returncode == 0, _diagnose(previous, "alembic upgrade 38af6b21dc04")

    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    engine = create_engine(url)
    try:
        with Session(engine) as db:
            project = Project(name="Legacy proposal migration")
            db.add(project)
            db.flush()
            open_set = ChangeSet(
                project_id=project.id,
                base_revision_number=0,
                title="Validation failed but proposal remains recoverable",
                status=ChangeSetStatus.VALIDATION_FAILED,
            )
            merged_set = ChangeSet(
                project_id=project.id,
                base_revision_number=0,
                merged_revision_number=1,
                title="Proposal was overwritten by its landed outcome",
                status=ChangeSetStatus.MERGED,
            )
            rehearsed_set = ChangeSet(
                project_id=project.id,
                base_revision_number=1,
                title="Proposal was overwritten by a successful rehearsal",
                status=ChangeSetStatus.SUBMITTED,
            )
            db.add_all((open_set, merged_set, rehearsed_set))
            db.flush()
            project_id = project.id
            change_set_ids = {
                "open": open_set.id,
                "merged": merged_set.id,
                "rehearsed": rehearsed_set.id,
            }
            db.commit()

        metadata = MetaData()
        operations = Table("change_operations", metadata, autoload_with=engine)
        results = Table("merge_results", metadata, autoload_with=engine)
        operation_ids = {name: uuid.uuid4() for name in change_set_ids}
        basis = [{"kind": "template_rule", "template_id": "legacy@1"}]

        def operation_row(
            name: str, *, after: dict[str, object], applied: bool
        ) -> dict[str, object]:
            return {
                "project_id": str(project_id),
                "change_set_id": str(change_set_ids[name]),
                "ordinal": 0,
                "operation_type": "create_object",
                "object_type": "tag",
                "object_id": None,
                "temp_key": f"legacy-{name}",
                "based_on_object_revision": None,
                "field_name": "",
                "before": json.dumps({}),
                "after": json.dumps({**after, "$basis": basis}),
                "related_object_type": None,
                "related_object_id": None,
                "relationship_key": "",
                "description": f"Legacy {name} operation",
                "applied": applied,
                "applied_at": now if applied else None,
                "failure_reason": "",
                "id": str(operation_ids[name]),
                "created_at": now,
                "updated_at": now,
            }

        def result_row(
            name: str,
            *,
            outcome: str,
            dry_run: bool,
            validation_error_count: int = 0,
        ) -> dict[str, object]:
            return {
                "project_id": str(project_id),
                "change_set_id": str(change_set_ids[name]),
                "outcome": outcome,
                "is_dry_run": dry_run,
                "base_revision_number": 0,
                "head_revision_number": 0,
                "resulting_revision_number": None,
                "operations_applied": 0,
                "operations_skipped": 1,
                "conflict_count": 0,
                "validation_error_count": validation_error_count,
                "explanation": "Legacy result retained only prose and a count.",
                "performed_by_id": None,
                "id": str(uuid.uuid4()),
                "created_at": now,
                "updated_at": now,
            }

        with engine.begin() as connection:
            connection.execute(
                operations.insert(),
                (
                    operation_row("open", after={"name": "Proposed"}, applied=False),
                    operation_row("merged", after={"name": "Landed"}, applied=True),
                    operation_row("rehearsed", after={"name": "Would land"}, applied=False),
                ),
            )
            connection.execute(
                results.insert(),
                (
                    result_row(
                        "open",
                        outcome="validation_failed",
                        dry_run=False,
                        validation_error_count=2,
                    ),
                    result_row("rehearsed", outcome="dry_run_ok", dry_run=True),
                ),
            )
    finally:
        engine.dispose()

    upgraded = _run_alembic("upgrade", "head", database_url=url)
    assert upgraded.returncode == 0, _diagnose(upgraded, "alembic upgrade head")

    engine = create_engine(url)
    try:
        with Session(engine) as db:
            migrated = {
                row.id: row
                for row in db.execute(
                    select(ChangeOperation).where(ChangeOperation.project_id == project_id)
                ).scalars()
            }
            assert migrated[operation_ids["open"]].values == {"name": "Proposed"}
            assert migrated[operation_ids["open"]].basis == basis
            assert migrated[operation_ids["open"]].after == {}

            # Outcome rows remain truthful. Their exact proposal was already
            # overwritten in the old schema, so values stays explicitly unknown.
            assert migrated[operation_ids["merged"]].values == {}
            assert migrated[operation_ids["merged"]].basis == basis
            assert migrated[operation_ids["merged"]].after == {"name": "Landed"}
            assert migrated[operation_ids["rehearsed"]].values == {}
            assert migrated[operation_ids["rehearsed"]].basis == basis
            assert migrated[operation_ids["rehearsed"]].after == {"name": "Would land"}

            legacy_results = list(
                db.execute(
                    select(MergeResult).where(MergeResult.project_id == project_id)
                ).scalars()
            )
            assert legacy_results
            assert all(row.validation_errors == [] for row in legacy_results)
            assert any(row.validation_error_count == 2 for row in legacy_results)

        schema = inspect(engine)
        operation_columns = {row["name"]: row for row in schema.get_columns("change_operations")}
        assert operation_columns["values"]["nullable"] is False
        assert operation_columns["basis"]["nullable"] is False
        result_columns = {row["name"]: row for row in schema.get_columns("merge_results")}
        assert result_columns["validation_errors"]["nullable"] is False
        instruction_columns = {row["name"]: row for row in schema.get_columns("logic_instructions")}
        assert instruction_columns["branch_id"]["nullable"] is True
        assert "ix_logic_instructions_branch_id" in {
            row["name"] for row in schema.get_indexes("logic_instructions")
        }
        branch_fks = [
            row
            for row in schema.get_foreign_keys("logic_instructions")
            if row["constrained_columns"] == ["branch_id"]
        ]
        assert len(branch_fks) == 1
        assert branch_fks[0]["referred_table"] == "logic_branches"
        assert branch_fks[0]["options"].get("ondelete") == "CASCADE"
    finally:
        engine.dispose()

    downgraded = _run_alembic("downgrade", "38af6b21dc04", database_url=url)
    assert downgraded.returncode == 0, _diagnose(downgraded, "alembic downgrade 38af6b21dc04")

    engine = create_engine(url)
    try:
        metadata = MetaData()
        operations = Table("change_operations", metadata, autoload_with=engine)
        with engine.connect() as connection:
            restored = {
                uuid.UUID(str(row.id)): json.loads(row.after)
                for row in connection.execute(
                    select(operations.c.id, operations.c.after).where(
                        operations.c.project_id == str(project_id)
                    )
                )
            }
        assert restored[operation_ids["open"]] == {
            "name": "Proposed",
            "$basis": basis,
        }
        assert restored[operation_ids["merged"]] == {
            "name": "Landed",
            "$basis": basis,
        }
        assert restored[operation_ids["rehearsed"]] == {
            "name": "Would land",
            "$basis": basis,
        }
        schema = inspect(engine)
        assert "values" not in {row["name"] for row in schema.get_columns("change_operations")}
        assert "basis" not in {row["name"] for row in schema.get_columns("change_operations")}
        assert "validation_errors" not in {
            row["name"] for row in schema.get_columns("merge_results")
        }
        assert "branch_id" not in {row["name"] for row in schema.get_columns("logic_instructions")}
    finally:
        engine.dispose()


def test_sync_agent_definitions_is_idempotent(db: Session) -> None:
    """It runs on every boot of the API and the worker, so it must not duplicate rows."""
    sync_agent_definitions(db)
    db.commit()
    first = db.execute(select(func.count()).select_from(AgentDefinition)).scalar_one()

    sync_agent_definitions(db)
    db.commit()
    second = db.execute(select(func.count()).select_from(AgentDefinition)).scalar_one()

    assert first == second

    rows = {row.kind: row for row in db.execute(select(AgentDefinition)).scalars()}
    for kind in CORE_KINDS:
        assert kind.value in rows, sorted(rows)
        assert rows[kind.value].is_available is True
        assert rows[kind.value].is_plugin is False
        assert rows[kind.value].unavailable_reason == ""
        assert rows[kind.value].display_name != ""


def test_sync_agent_definitions_retires_a_removed_plugin_agent_instead_of_deleting_it(
    db: Session,
) -> None:
    """Tasks and runs in the history reference the definition; deleting it would orphan them."""
    sync_agent_definitions(db, PluginRegistry())
    db.add(
        AgentDefinition(
            kind="synthetic_test_plan_writer",
            display_name="Synthetic Test Plan Writer",
            is_plugin=True,
            is_available=True,
        )
    )
    db.flush()
    before = db.execute(select(func.count()).select_from(AgentDefinition)).scalar_one()

    sync_agent_definitions(db, PluginRegistry())
    db.commit()

    after = db.execute(select(func.count()).select_from(AgentDefinition)).scalar_one()
    assert after == before

    retired = db.execute(
        select(AgentDefinition).where(AgentDefinition.kind == "synthetic_test_plan_writer")
    ).scalar_one()
    assert retired.is_available is False
    assert retired.unavailable_reason != ""

    for kind in CORE_KINDS:
        core = db.execute(
            select(AgentDefinition).where(AgentDefinition.kind == kind.value)
        ).scalar_one()
        assert core.is_available is True


def test_user_bootstrap_is_idempotent_and_seeds_one_administrator(
    db: Session, settings: Settings
) -> None:
    """Without a single stable admin account a fresh install has no way in, and a
    second boot must not mint a duplicate."""
    assert settings.bootstrap_demo_users is False

    first = user_service.bootstrap(db, settings)
    db.commit()
    second = user_service.bootstrap(db, settings)
    db.commit()

    assert first.id == second.id
    assert first.email == settings.bootstrap_admin_email

    users = list(db.execute(select(User)).scalars())
    assert [user.email for user in users] == [settings.bootstrap_admin_email]

    administrators = [user for user in users if RoleName.ADMINISTRATOR in user.role_names]
    assert len(administrators) == 1
    assert administrators[0].id == first.id


def test_user_bootstrap_seeds_every_role_row(db: Session, settings: Settings) -> None:
    """Roles are referenced by name when granting permissions; a missing row is a 500."""
    user_service.bootstrap(db, settings)
    db.commit()

    names = sorted(role.name for role in db.execute(select(Role)).scalars())
    assert names == sorted(role.value for role in RoleName)
