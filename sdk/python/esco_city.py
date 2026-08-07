"""Tiny provider-neutral client for an ESCO external-agent mission.

This module intentionally depends only on Python's standard library.  Provider
SDKs remain in the worker examples, while every city operation goes through the
same capability-scoped HTTP surface.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class EscoCityError(RuntimeError):
    pass


@dataclass(slots=True)
class EscoCityClient:
    base_url: str
    token: str

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(  # noqa: S310 - caller supplies the ESCO service URL
            f"{self.base_url.rstrip('/')}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=60) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise EscoCityError(f"ESCO returned HTTP {exc.code}: {detail}") from exc

    def context(self, *, evidence_offset: int = 0, evidence_limit: int = 200) -> dict[str, Any]:
        query = urlencode(
            {"evidence_offset": evidence_offset, "evidence_limit": evidence_limit}
        )
        return self._request("GET", f"/api/external-agents/session/context?{query}")

    def report_progress(
        self,
        *,
        status: str,
        location: str,
        activity: str,
        message: str = "",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/external-agents/session/progress",
            {
                "status": status,
                "location": location,
                "activity": activity,
                "message": message,
            },
        )

    def submit(
        self,
        *,
        rationale: str,
        actions: list[dict[str, Any]],
        warnings: list[str] | None = None,
        submission_id: uuid.UUID | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/external-agents/session/submit",
            {
                "submission_id": str(submission_id or uuid.uuid4()),
                "rationale": rationale,
                "actions": actions,
                "warnings": warnings or [],
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "latency_ms": latency_ms,
            },
        )


@dataclass(slots=True)
class EscoCityOperatorClient(EscoCityClient):
    """User-session operations used to deploy or revoke a mission."""

    def deploy(
        self,
        *,
        project_id: str,
        provider: str,
        display_name: str,
        specialty: str,
        mission: str,
        model: str,
        ttl_seconds: int = 3600,
        initial_location: str = "project_intake",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/projects/{project_id}/external-agents",
            {
                "provider": provider,
                "display_name": display_name,
                "specialty": specialty,
                "mission": mission,
                "model": model,
                "ttl_seconds": ttl_seconds,
                "initial_location": initial_location,
                "acknowledge_external_processing": True,
            },
        )

    def revoke(self, *, project_id: str, instance_id: str) -> dict[str, Any]:
        return self._request(
            "DELETE", f"/api/projects/{project_id}/external-agents/{instance_id}"
        )
