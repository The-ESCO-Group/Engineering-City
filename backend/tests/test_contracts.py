"""The committed contracts must match what the application actually exposes.

An API change that is not reflected in ``contracts/`` fails here, which is what
stops the frontend from generating a client against a document that lies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app import CONTRACT_VERSION
from app.api.schemas.events import EVENT_PAYLOAD_MODELS
from app.core.enums import EventType

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"


def _load_generator():  # type: ignore[no-untyped-def]
    """Import the generator script by path (it lives outside the package)."""
    import importlib.util

    script = REPO_ROOT / "scripts" / "backend" / "generate_contracts.py"
    spec = importlib.util.spec_from_file_location("_generate_contracts", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_contracts_match_application() -> None:
    generator = _load_generator()
    for path, expected in generator.generate().items():
        relative = path.relative_to(REPO_ROOT)
        assert path.exists(), f"missing contract file: {relative}"
        actual = path.read_text(encoding="utf-8")
        assert actual == expected, (
            f"{relative} is out of date.\nRun: python scripts/backend/generate_contracts.py"
        )


def test_contract_version_is_consistent_across_artifacts() -> None:
    openapi = yaml.safe_load((CONTRACTS_DIR / "openapi.yaml").read_text(encoding="utf-8"))
    domain = json.loads((CONTRACTS_DIR / "domain.schema.json").read_text(encoding="utf-8"))
    events = json.loads((CONTRACTS_DIR / "events.schema.json").read_text(encoding="utf-8"))

    assert openapi["info"]["x-contract-version"] == CONTRACT_VERSION
    assert domain["x-contract-version"] == CONTRACT_VERSION
    assert events["x-contract-version"] == CONTRACT_VERSION


def test_every_event_type_has_a_documented_payload() -> None:
    """A broadcast event the frontend cannot type is a contract hole."""
    undocumented = sorted(
        event_type.value for event_type in EventType if event_type not in EVENT_PAYLOAD_MODELS
    )
    assert undocumented == [], f"event types with no payload model: {undocumented}"


def test_events_contract_maps_every_event_type() -> None:
    events = json.loads((CONTRACTS_DIR / "events.schema.json").read_text(encoding="utf-8"))
    mapped = set(events["x-payload-by-event-type"])
    assert mapped == {event_type.value for event_type in EventType}

    defs = events["$defs"]
    for event_type, ref in events["x-payload-by-event-type"].items():
        name = ref.rsplit("/", 1)[-1]
        assert name in defs, f"{event_type} references undefined payload {name}"
