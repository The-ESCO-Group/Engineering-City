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


def test_installed_partner_plugins_are_reported_available(client: TestClient) -> None:
    """Installed delivery and reporting capabilities are reported explicitly.

    Asserts *membership*, not an exact list. The delivery agents and report
    providers are contributed by a partner-owned package that adds to them
    freely; an equality assertion here breaks every time they ship one, which
    trains whoever hits it to edit the test rather than read it. What this
    backend actually promises is that an installed capability is reported as
    available and named — so that is what is checked.
    """
    body = client.get("/api/health").json()
    capabilities = {row["name"]: row for row in body["capabilities"]}

    for name in ("delivery_agents", "report_providers", "export_formats"):
        assert capabilities[name]["available"] is True, name
        assert capabilities[name]["provider"], f"{name} is available but names nothing"

    installed = {
        name: {item.strip() for item in capabilities[name]["provider"].split(",")}
        for name in ("delivery_agents", "report_providers", "export_formats")
    }
    assert "engineering_report_delivery" in installed["delivery_agents"]
    assert {"discrepancy_matrix", "test_plan"} <= installed["report_providers"]
    assert {"html", "markdown"} <= installed["export_formats"]


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
