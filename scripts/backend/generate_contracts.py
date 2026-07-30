"""Generate the published contracts under ``contracts/``.

The contracts are *generated from the running application* rather than
hand-maintained, so they cannot drift into describing endpoints that do not
exist. ``backend/tests/test_contracts.py`` regenerates them and fails if the
committed files differ, which makes an undeclared API change a test failure.

Usage::

    python scripts/backend/generate_contracts.py            # write files
    python scripts/backend/generate_contracts.py --check    # verify only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
CONTRACTS_DIR = REPO_ROOT / "contracts"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import yaml  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app import CONTRACT_VERSION  # noqa: E402
from app.agents.base import actions as action_schemas  # noqa: E402
from app.api.schemas import agent as agent_schemas  # noqa: E402
from app.api.schemas import approval as approval_schemas  # noqa: E402
from app.api.schemas import auth as auth_schemas  # noqa: E402
from app.api.schemas import events as event_schemas  # noqa: E402
from app.api.schemas import file as file_schemas  # noqa: E402
from app.api.schemas import finding as finding_schemas  # noqa: E402
from app.api.schemas import project as project_schemas  # noqa: E402
from app.api.schemas import report as report_schemas  # noqa: E402
from app.api.schemas import task as task_schemas  # noqa: E402
from app.core import enums as domain_enums  # noqa: E402

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

#: Domain models published to the frontend, grouped for readability.
DOMAIN_MODELS: dict[str, list[type[BaseModel]]] = {
    "auth": [
        auth_schemas.LoginRequest,
        auth_schemas.LoginResponse,
        auth_schemas.CurrentUserResponse,
        auth_schemas.CreateUserRequest,
        auth_schemas.UserSummary,
    ],
    "project": [
        project_schemas.CreateProjectRequest,
        project_schemas.UpdateProjectRequest,
        project_schemas.ProjectRead,
        project_schemas.ProjectDetail,
        project_schemas.ProjectMemberRead,
        project_schemas.ProjectCounts,
        project_schemas.AddProjectMemberRequest,
    ],
    "file": [
        file_schemas.UploadedFileRead,
        file_schemas.FileDetail,
        file_schemas.UploadResponse,
        file_schemas.UploadResultItem,
        file_schemas.ParsedArtifactRead,
        file_schemas.ParsedArtifactDetail,
        file_schemas.EvidenceAnchorRead,
        file_schemas.ParserWarningRead,
    ],
    "workflow": [
        task_schemas.TaskRead,
        task_schemas.TaskDetail,
        task_schemas.StartRunRequest,
        task_schemas.RunRead,
        task_schemas.WorkflowControlRequest,
        task_schemas.WorkflowStateResponse,
    ],
    "agent": [
        agent_schemas.AgentDefinitionRead,
        agent_schemas.AgentInstanceRead,
        agent_schemas.AgentRunRead,
        agent_schemas.AgentRunDetail,
        agent_schemas.AgentActionRead,
        agent_schemas.AgentMessageRead,
        agent_schemas.ModelConfigurationRead,
        agent_schemas.UpdateModelConfigurationRequest,
    ],
    "finding": [
        finding_schemas.FindingRead,
        finding_schemas.FindingDetail,
        finding_schemas.FindingEvidenceRead,
        finding_schemas.UpdateFindingRequest,
        finding_schemas.CreateFindingCommentRequest,
        finding_schemas.FindingCommentRead,
        finding_schemas.RecommendationRead,
        finding_schemas.DiscrepancyMatrixRow,
    ],
    "approval": [
        approval_schemas.ApprovalRead,
        approval_schemas.ApprovalDecisionRequest,
    ],
    "report": [report_schemas.ReportRead],
    "agent_action": [
        action_schemas.AgentAction,
        action_schemas.EvidenceRef,
        action_schemas.CreateFindingPayload,
        action_schemas.CreateRecommendationPayload,
        action_schemas.CreateTaskPayload,
        action_schemas.PostMessagePayload,
        action_schemas.RequestApprovalPayload,
        action_schemas.UpdateFindingReviewPayload,
        action_schemas.ReportBlockerPayload,
        action_schemas.ReportNoFindingsPayload,
    ],
}

#: Enums published as part of the domain vocabulary.
PUBLISHED_ENUMS: tuple[str, ...] = (
    "ActionType",
    "AgentKind",
    "AgentRunStatus",
    "AgentStatus",
    "AnchorType",
    "ApprovalKind",
    "ApprovalStatus",
    "AuditAction",
    "DocumentRole",
    "EventType",
    "EvidenceKind",
    "FileKind",
    "FileStatus",
    "FindingCategory",
    "FindingSeverity",
    "FindingStatus",
    "MessageRole",
    "ProjectRole",
    "ProjectStatus",
    "ProviderKind",
    "RecommendationStatus",
    "ReportStatus",
    "RoleName",
    "TaskKind",
    "TaskStatus",
)


def _collect_defs(models: list[type[BaseModel]]) -> dict[str, Any]:
    """Flatten a group of models into one ``$defs`` map.

    Pydantic emits nested ``$defs`` with ``#/$defs/X`` refs; because every model
    lands in the same map here, those refs stay valid after the merge.
    """
    defs: dict[str, Any] = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        nested = schema.pop("$defs", {})
        for name, sub in nested.items():
            defs.setdefault(name, sub)
        defs[schema.get("title", model.__name__)] = schema
    return defs


def build_domain_schema() -> dict[str, Any]:
    """JSON Schema for the domain vocabulary: enums plus entity shapes."""
    enum_defs: dict[str, Any] = {}
    for name in PUBLISHED_ENUMS:
        enum_cls = getattr(domain_enums, name)
        enum_defs[name] = {
            "title": name,
            "type": "string",
            "enum": [member.value for member in enum_cls],
            "description": (enum_cls.__doc__ or "").strip().split("\n")[0],
        }

    defs: dict[str, Any] = dict(enum_defs)
    groups: dict[str, list[str]] = {}
    for group, models in DOMAIN_MODELS.items():
        group_defs = _collect_defs(models)
        groups[group] = sorted(group_defs)
        for name, schema in group_defs.items():
            defs.setdefault(name, schema)

    return {
        "$schema": SCHEMA_DIALECT,
        "$id": "https://esco.local/contracts/domain.schema.json",
        "title": "ESCO Engineering City domain schema",
        "description": (
            "Domain vocabulary and entity shapes for the ESCO Engineering City API. "
            "Generated from the backend Pydantic models; do not edit by hand."
        ),
        "x-contract-version": CONTRACT_VERSION,
        "x-enums": sorted(enum_defs),
        "x-groups": dict(sorted(groups.items())),
        "$defs": dict(sorted(defs.items())),
    }


def build_events_schema() -> dict[str, Any]:
    """JSON Schema for the WebSocket event envelope and every payload shape."""
    envelope_models = [
        event_schemas.EventEnvelope,
        event_schemas.WebSocketHello,
        event_schemas.WebSocketError,
    ]
    payload_models = sorted(
        set(event_schemas.EVENT_PAYLOAD_MODELS.values()), key=lambda m: m.__name__
    )
    defs = _collect_defs([*envelope_models, *payload_models])

    payload_by_type = {
        event_type.value: f"#/$defs/{model.__name__}"
        for event_type, model in sorted(
            event_schemas.EVENT_PAYLOAD_MODELS.items(), key=lambda item: item[0].value
        )
    }

    return {
        "$schema": SCHEMA_DIALECT,
        "$id": "https://esco.local/contracts/events.schema.json",
        "title": "ESCO Engineering City event contract",
        "description": (
            "Envelope and payload shapes for events broadcast on "
            "WebSocket /ws/projects/{project_id}. Every event is persisted "
            "before it is broadcast, and `seq` is gap-free per project so a "
            "reconnecting client can replay with ?since_seq=. "
            "Generated from the backend models; do not edit by hand."
        ),
        "x-contract-version": CONTRACT_VERSION,
        "x-schema-version": 1,
        "x-payload-by-event-type": payload_by_type,
        "$ref": "#/$defs/EventEnvelope",
        "$defs": dict(sorted(defs.items())),
    }


def build_openapi() -> dict[str, Any]:
    """OpenAPI document generated from the live FastAPI application."""
    from app.core.config import Settings
    from app.main import create_app

    # A fixed settings object keeps generation independent of the developer's
    # local environment (data paths, enabled providers).
    settings = Settings(
        environment="test",
        database_url="sqlite+pysqlite:///:memory:",
        log_json=False,
    )
    app = create_app(settings, run_bootstrap=False)
    document = app.openapi()
    document["info"]["x-contract-version"] = CONTRACT_VERSION
    document["info"]["license"] = {"name": "ESCO Group internal"}
    return document


def _dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def _dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def generate() -> dict[Path, str]:
    """Build every contract artifact as ``path -> content``."""
    return {
        CONTRACTS_DIR / "domain.schema.json": _dump_json(build_domain_schema()),
        CONTRACTS_DIR / "events.schema.json": _dump_json(build_events_schema()),
        CONTRACTS_DIR / "openapi.yaml": _dump_yaml(build_openapi()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the committed contracts differ from generated output.",
    )
    args = parser.parse_args()

    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    artifacts = generate()

    if args.check:
        stale = [
            path
            for path, content in artifacts.items()
            if not path.exists() or path.read_text(encoding="utf-8") != content
        ]
        if stale:
            for path in stale:
                print(f"stale contract: {path.relative_to(REPO_ROOT)}")
            print("\nRun: python scripts/backend/generate_contracts.py")
            return 1
        print("contracts up to date")
        return 0

    for path, content in artifacts.items():
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
