"""Shared pytest fixtures.

Every test runs against an isolated, file-backed SQLite database and a
throwaway data directory, so nothing touches a developer's real ``data/``
tree and tests never observe each other's state.

A file (rather than ``:memory:``) is used because several tests exercise
restart recovery, which requires the database to outlive a session and an
engine disposal.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.database import session as db_session
from app.models import Base


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a developer's ``ESCO_*`` environment leaking into tests."""
    for key in list(os.environ):
        if key.startswith("ESCO_"):
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def settings(tmp_path: Path, data_dir: Path) -> Settings:
    """Deterministic test settings: SQLite on disk, mock provider only."""
    return Settings(
        environment="test",
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'test.sqlite3').as_posix()}",
        data_dir=data_dir,
        log_json=False,
        log_level="WARNING",
        worker_enabled=False,
        bootstrap_demo_users=True,
        jwt_secret="test-secret-not-used-outside-tests",
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    """Engine bound to a fresh schema, wired into the app's session module."""
    eng = db_session.build_engine(settings)
    Base.metadata.create_all(eng)
    db_session.configure(eng)
    try:
        yield eng
    finally:
        db_session.reset()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """A session for arranging fixtures and asserting persisted state."""
    session = db_session.get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app(settings: Settings, engine: Engine) -> FastAPI:
    from app.main import create_app

    application = create_app(settings)
    application.dependency_overrides[get_settings] = lambda: settings
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
