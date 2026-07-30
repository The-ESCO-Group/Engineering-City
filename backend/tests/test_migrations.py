"""Alembic history and the idempotent startup sequence.

The migration is the only thing that builds a real deployment's schema, so it is
exercised through the ``alembic`` CLI exactly as an operator would run it, in a
subprocess against a throwaway SQLite file. Bootstrap runs on every boot of both
the API and the worker, so re-running it must never duplicate or destroy data.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from app.agents.base.plugins import PluginRegistry
from app.core.bootstrap import sync_agent_definitions
from app.core.config import Settings
from app.core.enums import AgentKind, RoleName
from app.core.services import users as user_service
from app.models import Base
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
