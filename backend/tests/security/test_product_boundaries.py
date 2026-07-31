"""The things this product must never be able to do.

ADR 0001 decision 6 lists them, and this module is what stops that list becoming
a paragraph nobody rereads. Each test fails loudly if a later change quietly
adds the capability, so weakening one of these becomes a visible reversal rather
than a refactor that slipped through.

Two of the checks scan source text. That is a blunt instrument and it is chosen
deliberately: the guarantee is about the *absence* of a capability, and absence
is not something a unit test of existing behaviour can demonstrate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.enums import (
    MAX_OFFLINE_VALIDATION_LEVEL,
    VALIDATION_LEVEL_ORDER,
    ProviderKind,
    RestrictedDomain,
    ValidationLevel,
)

BACKEND_APP = Path(__file__).resolve().parents[2] / "app"

#: Every Python module in the backend application package.
APP_MODULES = sorted(
    path
    for path in BACKEND_APP.rglob("*.py")
    if "migrations" not in path.parts and "__pycache__" not in path.parts
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_there_are_modules_to_scan() -> None:
    """Guards the scans below from passing because they found nothing."""
    assert len(APP_MODULES) > 60


# ---------------------------------------------------------------------------
# No live controller operation
# ---------------------------------------------------------------------------
#: Industrial protocol libraries. Not disabled — absent. Importing one of these
#: would be the first step towards talking to a controller, and there is no
#: legitimate reason for this backend to have taken it.
FORBIDDEN_IMPORTS = frozenset(
    {
        "pycomm3",
        "pylogix",
        "cpppo",
        "pymodbus",
        "opcua",
        "asyncua",
        "snap7",
        "pyads",
    }
)


def _imported_module_roots(path: Path) -> set[str]:
    tree = ast.parse(_source(path), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", APP_MODULES, ids=lambda p: p.name)
def test_no_module_imports_an_industrial_protocol_library(path: Path) -> None:
    """The MVP has no path to a live controller, by construction.

    Not a disabled feature flag — no code that could do it exists. A future
    change that adds one of these imports fails here, which is the point.
    """
    offending = _imported_module_roots(path) & FORBIDDEN_IMPORTS
    assert not offending, f"{path.name} imports {sorted(offending)}"


def test_no_controller_operation_vocabulary_in_the_action_contract() -> None:
    """A model cannot propose an action the closed set does not contain.

    Upload, download, online edit, mode change, force and firmware operations
    are absent from ``ActionType``, so there is nothing for a model to name.
    """
    from app.core.enums import ActionType

    values = {action.value for action in ActionType}
    forbidden = {
        "upload_program",
        "download_program",
        "online_edit",
        "change_mode",
        "force_tag",
        "flash_firmware",
        "connect_controller",
    }
    assert not (values & forbidden)


# ---------------------------------------------------------------------------
# No proprietary format fabrication
# ---------------------------------------------------------------------------
def test_acd_is_not_a_supported_file_kind() -> None:
    """``.ACD`` is proprietary. The product says so rather than guessing at it."""
    from app.core.enums import FileKind

    assert "acd" not in {kind.value for kind in FileKind}


def test_uploading_an_acd_is_refused_with_an_explanation() -> None:
    """A refusal that does not say what to upload instead is a dead end."""
    from app.core.errors import AppError
    from app.security.file_validation import validate_upload

    with pytest.raises(AppError) as raised:
        validate_upload(
            filename="Line4.ACD",
            head=b"\x00\x01\x02\x03proprietary",
            size_bytes=4096,
            max_bytes=10_000_000,
        )
    assert "L5X" in str(raised.value), str(raised.value)


# ---------------------------------------------------------------------------
# Honest verification status
# ---------------------------------------------------------------------------
def test_offline_verification_stops_short_of_a_real_import() -> None:
    """Round-trip parsing our own output is not a Studio 5000 import.

    The ceiling is named in one place so no call site can quietly raise it.
    """
    assert MAX_OFFLINE_VALIDATION_LEVEL is ValidationLevel.ROUND_TRIP_PARSED
    assert (
        VALIDATION_LEVEL_ORDER[MAX_OFFLINE_VALIDATION_LEVEL]
        < VALIDATION_LEVEL_ORDER[ValidationLevel.ROCKWELL_IMPORT_VERIFIED]
    )


def test_the_workstation_adapter_is_off_by_default() -> None:
    settings = Settings(environment="test")
    assert settings.workstation_adapter_enabled is False
    assert settings.workstation_executable_allowlist == []


def test_an_enabled_adapter_with_no_allowlist_is_refused_at_startup() -> None:
    """An adapter that can launch anything is not an adapter, it is a shell."""
    with pytest.raises(ValueError, match="EXECUTABLE_ALLOWLIST is empty"):
        Settings(
            environment="test",
            workstation_adapter_enabled=True,
            workstation_executable_allowlist=[],
        )


def test_an_allowlist_of_relative_paths_is_refused() -> None:
    """A relative path names whatever the working directory happens to hold."""
    with pytest.raises(ValueError, match="absolute paths"):
        Settings(
            environment="test",
            workstation_adapter_enabled=True,
            workstation_executable_allowlist=["studio5000.exe"],
        )


# ---------------------------------------------------------------------------
# Restricted domains
# ---------------------------------------------------------------------------
def test_no_restricted_domain_is_enabled_by_default() -> None:
    """Motion, safety logic, burner management and the rest are review-only.

    Enabling one is an administrator's explicit act, because the competence to
    review generated logic in those domains is a formal qualification rather
    than general controls experience.
    """
    assert Settings(environment="test").enabled_restricted_domains == []


def test_an_unrecognised_restricted_domain_is_refused_rather_than_ignored() -> None:
    """A typo fails safe but fails silently, which is its own hazard.

    An administrator who believes they enabled a qualified motion workflow and
    did not is worse off than one who was told the name was wrong.
    """
    with pytest.raises(ValueError, match=r"are not\s+restricted domains"):
        Settings(environment="test", enabled_restricted_domains=["moshun"])


def test_every_restricted_domain_name_is_accepted() -> None:
    settings = Settings(
        environment="test",
        enabled_restricted_domains=[domain.value for domain in RestrictedDomain],
    )
    assert len(settings.enabled_restricted_domains) == len(list(RestrictedDomain))


# ---------------------------------------------------------------------------
# No external model by default
# ---------------------------------------------------------------------------
def test_only_the_offline_mock_provider_is_enabled_by_default() -> None:
    """Project content does not leave this machine unless somebody says so."""
    assert Settings(environment="test").enabled_providers == [ProviderKind.MOCK]


@pytest.mark.parametrize("provider", [ProviderKind.OPENAI, ProviderKind.ANTHROPIC])
def test_a_cloud_provider_cannot_be_enabled_without_a_credential(
    provider: ProviderKind,
) -> None:
    """Half-configured is the dangerous state: enabled, and failing at run time."""
    with pytest.raises(ValueError, match="API_KEY is unset"):
        Settings(environment="test", enabled_providers=[ProviderKind.MOCK, provider])
