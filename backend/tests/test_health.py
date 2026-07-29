"""Health and capability reporting."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import CONTRACT_VERSION, __version__


def test_health_reports_ok_with_a_reachable_database(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"] == __version__
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["environment"] == "test"


def test_health_lists_only_explicitly_enabled_providers(client: TestClient) -> None:
    """No external provider may appear without an administrator enabling it."""
    body = client.get("/api/health").json()
    assert body["enabled_providers"] == ["mock"]


def test_missing_plugins_are_reported_not_fatal(client: TestClient) -> None:
    """GPT-owned plugins are absent here; the app must still start and say so."""
    body = client.get("/api/health").json()
    capabilities = {row["name"]: row for row in body["capabilities"]}

    assert capabilities["delivery_agents"]["available"] is False
    assert "app.agents.delivery" in capabilities["delivery_agents"]["detail"]
    assert capabilities["report_providers"]["available"] is False
    assert "app.reporting" in capabilities["report_providers"]["detail"]


def test_capabilities_endpoint_matches_health(client: TestClient) -> None:
    health = client.get("/api/health").json()["capabilities"]
    standalone = client.get("/api/system/capabilities").json()
    assert standalone == health


def test_security_headers_are_present(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "X-Request-ID" in response.headers


def test_unknown_route_returns_the_standard_error_envelope(client: TestClient) -> None:
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert isinstance(body["error"]["message"], str)
