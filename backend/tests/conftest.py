"""Shared pytest fixtures.

Every test runs against an isolated, file-backed SQLite database and a
throwaway data directory, so nothing touches a developer's real ``data/``
tree and tests never observe each other's state.

A file (rather than ``:memory:``) is used because several tests exercise restart
recovery, which requires the database to outlive a session and an engine
disposal.
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

from app.core.bootstrap import sync_agent_definitions
from app.core.config import Settings, get_settings
from app.core.enums import RoleName
from app.core.services import projects as project_service
from app.core.services import users as user_service
from app.database import session as db_session
from app.models import Base
from app.models.project import Project
from app.models.user import User

#: Long enough for the password policy; obviously not a real credential.
TEST_PASSWORD = "test-password-123456"


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
        bootstrap_demo_users=False,
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
def seeded(db: Session) -> dict[str, User]:
    """One user per role, plus agent definitions.

    Deliberately not the production bootstrap: tests should not depend on the
    demo seed data, and the passwords here are fixture-local.
    """
    user_service.ensure_roles(db)
    users = {
        "admin": user_service.create_user(
            db,
            email="admin@test.internal",
            display_name="Test Administrator",
            password=TEST_PASSWORD,
            roles=[RoleName.ADMINISTRATOR],
            actor_label="test-fixture",
        ),
        "engineer": user_service.create_user(
            db,
            email="engineer@test.internal",
            display_name="Test Engineer",
            password=TEST_PASSWORD,
            roles=[RoleName.ENGINEER],
            actor_label="test-fixture",
        ),
        "reviewer": user_service.create_user(
            db,
            email="reviewer@test.internal",
            display_name="Test Reviewer",
            password=TEST_PASSWORD,
            roles=[RoleName.REVIEWER, RoleName.ENGINEER],
            actor_label="test-fixture",
        ),
        "outsider": user_service.create_user(
            db,
            email="outsider@test.internal",
            display_name="Unrelated Engineer",
            password=TEST_PASSWORD,
            roles=[RoleName.ENGINEER],
            actor_label="test-fixture",
        ),
    }
    sync_agent_definitions(db)
    db.commit()
    return users


@pytest.fixture
def app(settings: Settings, engine: Engine) -> FastAPI:
    from app.main import create_app

    application = create_app(settings, run_bootstrap=False)
    application.dependency_overrides[get_settings] = lambda: settings
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, email: str, password: str = TEST_PASSWORD) -> str:
    """Authenticate and return the bearer token, failing loudly if refused."""
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


class AuthedClient:
    """Thin wrapper adding an Authorization header to every request.

    Cookies are also set by login, but an explicit header keeps each test's
    identity obvious and prevents one fixture's session leaking into another.
    """

    def __init__(self, client: TestClient, token: str, user: User) -> None:
        self._client = client
        self.token = token
        self.user = user
        self.headers = {"Authorization": f"Bearer {token}"}

    def request(self, method: str, url: str, **kwargs: object) -> object:
        headers = {**self.headers, **dict(kwargs.pop("headers", {}) or {})}  # type: ignore[arg-type]
        return self._client.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]

    def get(self, url: str, **kwargs: object) -> object:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: object) -> object:
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: object) -> object:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: object) -> object:
        return self.request("DELETE", url, **kwargs)


def _authed(client: TestClient, user: User) -> AuthedClient:
    return AuthedClient(client, login(client, user.email), user)


@pytest.fixture
def admin_client(client: TestClient, seeded: dict[str, User]) -> AuthedClient:
    return _authed(client, seeded["admin"])


@pytest.fixture
def engineer_client(client: TestClient, seeded: dict[str, User]) -> AuthedClient:
    return _authed(client, seeded["engineer"])


@pytest.fixture
def reviewer_client(client: TestClient, seeded: dict[str, User]) -> AuthedClient:
    return _authed(client, seeded["reviewer"])


@pytest.fixture
def outsider_client(client: TestClient, seeded: dict[str, User]) -> AuthedClient:
    """A valid account with no membership of the test project."""
    return _authed(client, seeded["outsider"])


@pytest.fixture
def project(db: Session, seeded: dict[str, User], settings: Settings) -> Project:
    """A draft project owned by the engineer, with the reviewer as a reviewer."""
    from app.core.enums import ProjectRole

    created = project_service.create_project(
        db,
        name="Synthetic Conveyor Line 3",
        description="Fixture project built from synthetic data only.",
        client_reference="TEST-001",
        attributes={"site": "Synthetic Plant"},
        creator=seeded["engineer"],
        settings=settings,
    )
    project_service.add_member(
        db,
        created,
        user_id=seeded["reviewer"].id,
        role=ProjectRole.REVIEWER,
        actor=seeded["engineer"],
    )
    db.commit()
    db.refresh(created)
    return created
