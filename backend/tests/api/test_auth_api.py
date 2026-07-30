"""HTTP behaviour of ``/api/auth`` and the authentication dependencies.

These tests drive the whole path a caller actually travels — request, dependency,
service, database, error envelope — because the properties that matter here
(uniform failure messages, real token revocation, administrator-only
provisioning) are only true end to end.

Accounts are provisioned locally rather than taken from the shared ``seeded``
fixture wherever a test signs in over HTTP: ``seeded`` uses ``@test.internal``
addresses and ``EmailStr`` refuses reserved domains, so those accounts cannot
reach the login endpoint at all.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import AuditAction, RoleName
from app.core.services import users as user_service
from app.models.audit import AuditEntry
from app.models.project import Project
from app.models.user import User
from app.security.tokens import create_access_token
from tests.conftest import TEST_PASSWORD, login

#: Distinctive synthetic secrets, so a leak into a response body or an audit row
#: is unambiguous rather than a coincidental substring match.
WRONG_PASSWORD = "definitely-not-the-stored-password"
NEW_USER_PASSWORD = "provisioned-account-password-42"


@pytest.fixture
def accounts(db: Session) -> dict[str, User]:
    """One account per role, addressed at a domain the API's validator accepts."""
    users = {
        "admin": user_service.create_user(
            db,
            email="admin@example.com",
            display_name="Example Administrator",
            password=TEST_PASSWORD,
            roles=[RoleName.ADMINISTRATOR],
            actor_label="test-fixture",
        ),
        "engineer": user_service.create_user(
            db,
            email="engineer@example.com",
            display_name="Example Engineer",
            password=TEST_PASSWORD,
            roles=[RoleName.ENGINEER],
            actor_label="test-fixture",
        ),
        "reviewer": user_service.create_user(
            db,
            email="reviewer@example.com",
            display_name="Example Reviewer",
            password=TEST_PASSWORD,
            roles=[RoleName.REVIEWER, RoleName.ENGINEER],
            actor_label="test-fixture",
        ),
    }
    db.commit()
    return users


def _login(client: TestClient, email: str, password: str = TEST_PASSWORD) -> Response:
    return client.post("/api/auth/login", json={"email": email, "password": password})


def _bearer(user: User, settings: Settings) -> dict[str, str]:
    """A header holding a genuine token, minted exactly as login mints one."""
    token, _ = create_access_token(user_id=user.id, email=user.email, settings=settings)
    return {"Authorization": f"Bearer {token}"}


def _error(response: Response) -> dict[str, Any]:
    """Assert the documented error envelope and return its ``error`` object."""
    body = response.json()
    assert set(body) == {"error"}, body
    assert {"code", "message"} <= set(body["error"]), body
    assert isinstance(body["error"]["message"], str)
    return dict(body["error"])


# --------------------------------------------------------------------------- login


def test_login_returns_a_token_the_user_and_an_httponly_session_cookie(
    client: TestClient, settings: Settings, accounts: dict[str, User]
) -> None:
    response = _login(client, "engineer@example.com")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["user"]["id"] == str(accounts["engineer"].id)
    assert body["user"]["email"] == "engineer@example.com"
    assert body["user"]["display_name"] == "Example Engineer"
    assert body["user"]["is_active"] is True
    assert body["user"]["roles"] == [RoleName.ENGINEER.value]

    expires_at = datetime.fromisoformat(body["expires_at"])
    assert utcnow() < expires_at
    assert expires_at <= utcnow() + timedelta(seconds=settings.access_token_ttl_seconds + 60)

    assert client.cookies.get(settings.session_cookie_name) == body["access_token"]
    assert "httponly" in response.headers["set-cookie"].lower()


def test_login_response_body_never_contains_the_password_or_its_hash(
    client: TestClient, accounts: dict[str, User]
) -> None:
    """A credential echoed back would end up in browser caches and proxy logs."""
    stored_hash = accounts["engineer"].password_hash
    assert stored_hash

    response = _login(client, "engineer@example.com")
    assert response.status_code == 200, response.text

    assert TEST_PASSWORD not in response.text
    assert stored_hash not in response.text
    assert "password" not in json.dumps(response.json()).lower()
    assert set(response.json()["user"]) == {"id", "email", "display_name", "is_active", "roles"}


def test_login_normalises_the_email_case(client: TestClient, accounts: dict[str, User]) -> None:
    """Engineers type their address however their mail client displays it."""
    response = _login(client, "Engineer@Example.COM")
    assert response.status_code == 200, response.text
    assert response.json()["user"]["email"] == "engineer@example.com"


def test_login_with_the_wrong_password_is_rejected(
    client: TestClient, accounts: dict[str, User]
) -> None:
    response = _login(client, "engineer@example.com", WRONG_PASSWORD)
    assert response.status_code == 401
    assert _error(response)["code"] == "authentication_required"
    assert "set-cookie" not in response.headers


def test_login_with_an_unknown_email_is_rejected(
    client: TestClient, accounts: dict[str, User]
) -> None:
    response = _login(client, "nobody@example.com")
    assert response.status_code == 401
    assert _error(response)["code"] == "authentication_required"
    assert "set-cookie" not in response.headers


def test_login_for_a_deactivated_account_is_rejected(
    client: TestClient, db: Session, accounts: dict[str, User]
) -> None:
    """Deactivating an account has to stop it signing in, not just flag it."""
    accounts["reviewer"].is_active = False
    db.commit()

    response = _login(client, "reviewer@example.com")
    assert response.status_code == 401
    assert _error(response)["code"] == "authentication_required"


def test_every_login_failure_reports_an_identical_error(
    client: TestClient, db: Session, accounts: dict[str, User]
) -> None:
    """Distinguishable failures would let an attacker enumerate ESCO accounts."""
    accounts["reviewer"].is_active = False
    db.commit()

    unknown_email = _login(client, "nobody@example.com")
    wrong_password = _login(client, "engineer@example.com", WRONG_PASSWORD)
    disabled_account = _login(client, "reviewer@example.com")

    assert [r.status_code for r in (unknown_email, wrong_password, disabled_account)] == [
        401,
        401,
        401,
    ]
    assert unknown_email.json() == wrong_password.json()
    assert unknown_email.json() == disabled_account.json()


def test_login_rejects_an_empty_password(client: TestClient, accounts: dict[str, User]) -> None:
    response = _login(client, "engineer@example.com", "")
    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"


# ------------------------------------------------------------------------------ me


def test_me_without_credentials_is_rejected(client: TestClient) -> None:
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert _error(response)["code"] == "authentication_required"


@pytest.mark.parametrize(
    "header",
    [
        "Token abc.def.ghi",
        "Bearer",
        "Bearer ",
        "abc.def.ghi",
        "Basic ZW5naW5lZXI6cGFzc3dvcmQ=",
    ],
)
def test_me_rejects_a_malformed_authorization_header(client: TestClient, header: str) -> None:
    response = client.get("/api/auth/me", headers={"Authorization": header})
    assert response.status_code == 401
    assert _error(response)["code"] == "authentication_required"


def test_me_rejects_a_token_signed_with_a_different_secret(
    client: TestClient, settings: Settings
) -> None:
    """Accepting a foreign signature would let anyone mint their own identity."""
    forged_settings = settings.model_copy(
        update={"jwt_secret": SecretStr("a-different-secret-entirely-0123456789")}
    )
    forged_token, _ = create_access_token(
        user_id=uuid.uuid4(),
        email="attacker@example.com",
        settings=forged_settings,
    )

    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged_token}"})
    assert response.status_code == 401
    assert _error(response)["code"] == "authentication_required"


def test_me_rejects_a_token_that_is_not_a_jwt(client: TestClient) -> None:
    response = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert response.status_code == 401
    assert _error(response)["code"] == "authentication_required"


def test_me_rejects_a_valid_token_for_an_account_that_does_not_exist(
    client: TestClient, settings: Settings
) -> None:
    """A correctly signed token is still only as good as the account behind it."""
    ghost = User(id=uuid.uuid4(), email="ghost@example.com", display_name="Ghost")
    response = client.get("/api/auth/me", headers=_bearer(ghost, settings))
    assert response.status_code == 401
    assert _error(response)["code"] == "authentication_required"


def test_me_returns_the_user_and_their_project_roles(
    client: TestClient, settings: Settings, seeded: dict[str, User], project: Project
) -> None:
    """The frontend gates every project action on exactly this mapping."""
    response = client.get("/api/auth/me", headers=_bearer(seeded["reviewer"], settings))
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["user"]["email"] == "reviewer@test.internal"
    assert set(body["user"]["roles"]) == {RoleName.REVIEWER.value, RoleName.ENGINEER.value}
    assert body["project_roles"] == {str(project.id): "reviewer"}


def test_me_reports_no_project_roles_for_a_non_member(
    client: TestClient, settings: Settings, seeded: dict[str, User], project: Project
) -> None:
    response = client.get("/api/auth/me", headers=_bearer(seeded["outsider"], settings))
    assert response.status_code == 200, response.text
    assert response.json()["project_roles"] == {}


def test_me_accepts_the_session_cookie_without_a_bearer_header(
    client: TestClient, settings: Settings, accounts: dict[str, User]
) -> None:
    """The browser frontend only ever holds the httpOnly cookie, never a header."""
    assert _login(client, "engineer@example.com").status_code == 200
    assert client.cookies.get(settings.session_cookie_name)

    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    assert response.json()["user"]["email"] == "engineer@example.com"


def test_deactivating_an_account_invalidates_its_existing_token(
    client: TestClient, db: Session, settings: Settings, accounts: dict[str, User]
) -> None:
    """Withdrawing access must not have to wait for the token to expire."""
    header = _bearer(accounts["engineer"], settings)
    assert client.get("/api/auth/me", headers=header).status_code == 200

    accounts["engineer"].is_active = False
    db.commit()

    response = client.get("/api/auth/me", headers=header)
    assert response.status_code == 401
    assert _error(response)["code"] == "authentication_required"


# -------------------------------------------------------------------------- logout


def test_logout_revokes_the_token_it_was_called_with(
    client: TestClient, settings: Settings, accounts: dict[str, User]
) -> None:
    """Logout must be real revocation, not a client-side gesture: a token that
    leaked before sign-out has to stop working."""
    token = login(client, "engineer@example.com")
    header = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/me", headers=header).status_code == 200

    signed_out = client.post("/api/auth/logout", headers=header)
    assert signed_out.status_code == 200, signed_out.text
    assert signed_out.json()["ok"] is True

    replayed = client.get("/api/auth/me", headers=header)
    assert replayed.status_code == 401
    assert _error(replayed)["code"] == "authentication_required"
    assert not client.cookies.get(settings.session_cookie_name)


def test_logout_leaves_other_sessions_of_the_same_user_alone(
    client: TestClient, accounts: dict[str, User]
) -> None:
    """Signing out of one machine must not sign the engineer out of the others."""
    first = {"Authorization": f"Bearer {login(client, 'engineer@example.com')}"}
    second = {"Authorization": f"Bearer {login(client, 'engineer@example.com')}"}

    assert client.post("/api/auth/logout", headers=first).status_code == 200
    assert client.get("/api/auth/me", headers=first).status_code == 401
    assert client.get("/api/auth/me", headers=second).status_code == 200


def test_logout_without_credentials_is_rejected(client: TestClient) -> None:
    response = client.post("/api/auth/logout")
    assert response.status_code == 401
    assert _error(response)["code"] == "authentication_required"


def test_logout_with_an_already_revoked_token_is_rejected(
    client: TestClient, accounts: dict[str, User]
) -> None:
    header = {"Authorization": f"Bearer {login(client, 'engineer@example.com')}"}
    assert client.post("/api/auth/logout", headers=header).status_code == 200
    assert client.post("/api/auth/logout", headers=header).status_code == 401


# ---------------------------------------------------------------- user management


@pytest.mark.parametrize("actor", ["engineer", "reviewer"])
def test_creating_a_user_requires_the_administrator_role(
    client: TestClient, settings: Settings, accounts: dict[str, User], actor: str
) -> None:
    response = client.post(
        "/api/auth/users",
        headers=_bearer(accounts[actor], settings),
        json={
            "email": "escalated@example.com",
            "display_name": "Escalated Account",
            "password": NEW_USER_PASSWORD,
            "roles": [RoleName.ADMINISTRATOR.value],
        },
    )
    assert response.status_code == 403
    assert _error(response)["code"] == "permission_denied"


def test_a_refused_user_creation_persists_nothing(
    client: TestClient, db: Session, settings: Settings, accounts: dict[str, User]
) -> None:
    client.post(
        "/api/auth/users",
        headers=_bearer(accounts["engineer"], settings),
        json={
            "email": "escalated@example.com",
            "display_name": "Escalated Account",
            "password": NEW_USER_PASSWORD,
        },
    )
    escalated = select(User).where(User.email == "escalated@example.com")
    assert db.execute(escalated).scalar_one_or_none() is None


def test_administrator_creates_a_user_who_can_then_sign_in(
    client: TestClient, db: Session, settings: Settings, accounts: dict[str, User]
) -> None:
    created = client.post(
        "/api/auth/users",
        headers=_bearer(accounts["admin"], settings),
        json={
            "email": "New.Hire@Example.com",
            "display_name": "New Hire",
            "password": NEW_USER_PASSWORD,
            "roles": [RoleName.REVIEWER.value],
        },
    )
    assert created.status_code == 201, created.text

    body = created.json()
    assert body["email"] == "new.hire@example.com"
    assert body["display_name"] == "New Hire"
    assert body["is_active"] is True
    assert body["roles"] == [RoleName.REVIEWER.value]
    assert NEW_USER_PASSWORD not in created.text
    assert "password" not in json.dumps(body).lower()

    stored = db.execute(
        select(User).where(User.email == "new.hire@example.com")
    ).scalar_one_or_none()
    assert stored is not None
    assert stored.password_hash.startswith("$argon2")

    signed_in = _login(client, "new.hire@example.com", NEW_USER_PASSWORD)
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["user"]["id"] == body["id"]


def test_creating_a_user_with_an_existing_email_conflicts(
    client: TestClient, settings: Settings, accounts: dict[str, User]
) -> None:
    """The clash is reported, not silently resolved into a second account."""
    response = client.post(
        "/api/auth/users",
        headers=_bearer(accounts["admin"], settings),
        json={
            "email": "Engineer@Example.com",
            "display_name": "Duplicate Engineer",
            "password": NEW_USER_PASSWORD,
        },
    )
    assert response.status_code == 409
    assert _error(response)["code"] == "conflict"


def test_creating_a_user_below_the_password_policy_is_rejected(
    client: TestClient, db: Session, settings: Settings, accounts: dict[str, User]
) -> None:
    """A weak password must be refused before there is an account to attack."""
    response = client.post(
        "/api/auth/users",
        headers=_bearer(accounts["admin"], settings),
        json={
            "email": "weak@example.com",
            "display_name": "Weak Password",
            "password": "short",
        },
    )
    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"

    row = db.execute(select(User).where(User.email == "weak@example.com")).scalar_one_or_none()
    assert row is None


@pytest.mark.parametrize("actor", ["engineer", "reviewer"])
def test_listing_users_requires_the_administrator_role(
    client: TestClient, settings: Settings, accounts: dict[str, User], actor: str
) -> None:
    response = client.get("/api/auth/users", headers=_bearer(accounts[actor], settings))
    assert response.status_code == 403
    assert _error(response)["code"] == "permission_denied"


def test_administrator_lists_every_user_without_any_password_material(
    client: TestClient, settings: Settings, accounts: dict[str, User]
) -> None:
    response = client.get("/api/auth/users", headers=_bearer(accounts["admin"], settings))
    assert response.status_code == 200, response.text

    rows = response.json()
    assert {row["email"] for row in rows} == {user.email for user in accounts.values()}
    assert TEST_PASSWORD not in response.text
    for user in accounts.values():
        assert user.password_hash not in response.text


# --------------------------------------------------------------------------- audit


def test_a_successful_login_is_audited(
    client: TestClient, db: Session, accounts: dict[str, User]
) -> None:
    """Sign-ins are the primary trail when reconstructing who changed a project."""
    assert _login(client, "engineer@example.com").status_code == 200

    entries = (
        db.execute(select(AuditEntry).where(AuditEntry.action == AuditAction.USER_LOGIN_SUCCEEDED))
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].actor_user_id == accounts["engineer"].id
    assert "engineer@example.com" in entries[0].summary


def test_a_logout_is_audited(client: TestClient, db: Session, accounts: dict[str, User]) -> None:
    login(client, "engineer@example.com")
    assert client.post("/api/auth/logout").status_code == 200

    entries = (
        db.execute(select(AuditEntry).where(AuditEntry.action == AuditAction.USER_LOGOUT))
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].actor_user_id == accounts["engineer"].id


def test_an_administrator_created_user_is_audited_against_the_administrator(
    client: TestClient, db: Session, settings: Settings, accounts: dict[str, User]
) -> None:
    created = client.post(
        "/api/auth/users",
        headers=_bearer(accounts["admin"], settings),
        json={
            "email": "audited@example.com",
            "display_name": "Audited Account",
            "password": NEW_USER_PASSWORD,
        },
    )
    assert created.status_code == 201, created.text

    entry = db.execute(
        select(AuditEntry)
        .where(AuditEntry.action == AuditAction.USER_CREATED)
        .where(AuditEntry.target_id == uuid.UUID(created.json()["id"]))
    ).scalar_one()
    assert entry.actor_user_id == accounts["admin"].id
    assert entry.details["roles"] == [RoleName.ENGINEER.value]


def test_no_audit_entry_records_a_plaintext_password(
    client: TestClient, db: Session, settings: Settings, accounts: dict[str, User]
) -> None:
    """An audit trail that captured credentials would be a second password store."""
    client.post(
        "/api/auth/users",
        headers=_bearer(accounts["admin"], settings),
        json={
            "email": "audited@example.com",
            "display_name": "Audited Account",
            "password": NEW_USER_PASSWORD,
        },
    )
    _login(client, "engineer@example.com", WRONG_PASSWORD)
    _login(client, "nobody@example.com")
    _login(client, "engineer@example.com")
    client.post("/api/auth/logout")

    entries = db.execute(select(AuditEntry)).scalars().all()
    assert entries

    for entry in entries:
        recorded = f"{entry.summary}|{entry.actor_label}|{json.dumps(entry.details)}"
        assert TEST_PASSWORD not in recorded
        assert NEW_USER_PASSWORD not in recorded
        assert WRONG_PASSWORD not in recorded
        assert accounts["engineer"].password_hash not in recorded
