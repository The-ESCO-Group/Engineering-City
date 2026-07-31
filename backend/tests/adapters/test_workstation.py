"""The optional Rockwell workstation adapter.

The test that matters most is :func:`test_a_disabled_adapter_cannot_reach_the_
verified_level`, and the group around it. Everything else in this file guards a
refusal; that one guards the claim the product is sold on. A package that says
``ROCKWELL_IMPORT_VERIFIED`` when no licensed workstation ever touched it is not
a bug that shows up as a stack trace — it is a sentence in a document an
engineer believes, and nothing downstream would contradict it.

So the invariant is asserted structurally: not "the disabled adapter happens to
return UNAVAILABLE today", but "there is no combination of arguments that
produces a verified level without an adapter reporting READY".

The second theme is that refusals are *refusals*. Each one is tested with the
attack it exists to stop, not with a value that is merely wrong: a ``..``
segment that names a different binary, a symlink swapped in after the allowlist
was built, a working directory pointing at ``data/projects/``.
"""

from __future__ import annotations

import ast
import sys
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import inspect as sa_inspect

from app.adapters import (
    CAPABILITY_NAME,
    AdapterHealth,
    DisabledWorkstationAdapter,
    ExecutableAllowlist,
    ExecutableNotAllowedError,
    LocalWorkstationAdapter,
    RockwellWorkstationAdapter,
    TargetProfileSupport,
    VerificationCommand,
    VerificationOutcome,
    VerificationRefusedError,
    WorkingDirectoryRefusedError,
    WorkstationAdapterDisabledError,
    capability_row,
    capability_rows,
    get_adapter,
    resolve_isolated_directory,
    run_allowlisted,
    scrubbed_environment,
)
from app.agents.base.plugins import PluginRegistry
from app.api.schemas.common import CapabilityStatus
from app.core.config import Settings
from app.core.enums import (
    MAX_OFFLINE_VALIDATION_LEVEL,
    VALIDATION_LEVEL_ORDER,
    ControllerFamily,
    ImportVerificationStatus,
    ReleaseCandidateStatus,
    ValidationLevel,
    WorkstationAdapterStatus,
)
from app.models.release import WorkstationVerificationJob
from tests.adapters import (
    STAND_IN_EXECUTABLE,
    make_candidate,
    make_job,
    make_package,
    make_settings,
)

ADAPTER_PACKAGE = Path(__file__).resolve().parents[3] / "backend" / "app" / "adapters"

#: Modules that could reach a controller, a network or a shell. None of them may
#: be imported anywhere in the adapter package.
FORBIDDEN_IMPORTS = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "httpx",
        "requests",
        "urllib",
        "ftplib",
        "telnetlib",
        "asyncio",
        "serial",
        "pymodbus",
        "pycomm3",
        "pylogix",
        "cpppo",
    }
)

#: Substrings that must not appear in any identifier the package defines or uses.
#: Names, not prose: the module docstrings say "there is no download path", and a
#: raw text search would flag the sentence that promises the absence.
FORBIDDEN_IDENTIFIER_FRAGMENTS = (
    "download",
    "upload",
    "firmware",
    "online_edit",
    "mode_change",
    "keyswitch",
    "go_online",
    "force_output",
)


def adapter_source_trees() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"))) for path in ADAPTER_PACKAGE.glob("*.py")
    ]


@pytest.fixture
def enabled_settings(tmp_path: Path) -> Settings:
    settings = make_settings(tmp_path)
    assert settings.workstation_working_dir is not None
    settings.workstation_working_dir.mkdir(parents=True, exist_ok=True)
    return settings


def echo_command(script: str, *arguments: str) -> VerificationCommand:
    """A stand-in verification command: the interpreter, running ``-c``."""
    return VerificationCommand(
        executable=str(STAND_IN_EXECUTABLE), arguments=("-c", script, *arguments)
    )


# ---------------------------------------------------------------------------
# The absence that must stay absent
# ---------------------------------------------------------------------------
def test_the_package_imports_nothing_that_could_reach_a_controller() -> None:
    """No controller connection, upload, download or firmware path exists here.

    Asserted against the import graph rather than a promise in a docstring: the
    adapter's entire vocabulary is "launch an allowlisted local program", and a
    future edit that adds a socket has to delete this test to land.
    """
    for path, tree in adapter_source_trees():
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                top_level = name.split(".", 1)[0]
                assert top_level not in FORBIDDEN_IMPORTS, f"{path.name} imports {name}"


def test_no_identifier_names_a_controller_operation() -> None:
    identifiers: set[str] = set()
    for _path, tree in adapter_source_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                identifiers.add(node.name)
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg)
            elif isinstance(node, ast.keyword) and node.arg:
                identifiers.add(node.arg)

    offenders = [
        name
        for name in identifiers
        if any(fragment in name.lower() for fragment in FORBIDDEN_IDENTIFIER_FRAGMENTS)
    ]
    assert not offenders, f"controller-operation identifiers found: {sorted(offenders)}"


def test_no_call_in_the_package_uses_a_shell() -> None:
    """``shell=True`` is never passed, and no shell-invoking builtin is called.

    Checked on the syntax tree so the module docstring can name ``shell=True``
    in the sentence that forbids it without the check tripping over its own
    documentation.
    """
    banned_calls = {"system", "popen", "getoutput", "getstatusoutput", "eval", "exec"}
    for path, tree in adapter_source_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "shell":
                    assert isinstance(keyword.value, ast.Constant), f"{path.name}: dynamic shell="
                    assert keyword.value.value is False, f"{path.name} passes shell=True"
            target = node.func
            called = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            assert called not in banned_calls, f"{path.name} calls {called}()"


# ---------------------------------------------------------------------------
# Disabled is the default
# ---------------------------------------------------------------------------
def test_the_default_settings_produce_a_disabled_adapter(tmp_path: Path) -> None:
    adapter = get_adapter(make_settings(tmp_path, enabled=False))
    assert isinstance(adapter, DisabledWorkstationAdapter)
    assert adapter.health().status is WorkstationAdapterStatus.DISABLED
    assert adapter.health().available is False


def test_both_adapters_satisfy_the_protocol(enabled_settings: Settings) -> None:
    assert isinstance(DisabledWorkstationAdapter(), RockwellWorkstationAdapter)
    assert isinstance(LocalWorkstationAdapter(enabled_settings), RockwellWorkstationAdapter)


def test_every_disabled_method_reports_absence_instead_of_raising(tmp_path: Path) -> None:
    """A missing optional capability degrades the product; it does not break it.

    The job here is deliberately the worst-shaped one in the file — no approver,
    a draft candidate, no package. The disabled adapter still answers, because
    a caller that has no workstation should not need a branch for every reason a
    job might also have been refused.
    """
    adapter = DisabledWorkstationAdapter()
    job = make_job(make_candidate(status=ReleaseCandidateStatus.DRAFT), approved=False)

    health = adapter.health()
    inventory = adapter.installed_products()
    profiles = adapter.supported_target_profiles()
    outcome = adapter.queue_verification(job)
    diagnostics = adapter.collect_diagnostics(job)

    assert health.status is WorkstationAdapterStatus.DISABLED
    assert inventory.available is False and inventory.products == ()
    assert profiles.available is False and profiles.profiles == ()
    assert outcome.status is ImportVerificationStatus.UNAVAILABLE
    assert diagnostics.available is False

    for detail in (health.detail, inventory.detail, profiles.detail, outcome.detail):
        assert "workstation" in detail.lower()
        assert detail.strip(), "an absence must be explained, not merely reported"


def test_unavailable_is_not_failed(tmp_path: Path) -> None:
    """A package nobody examined must not read like a package that was rejected."""
    outcome = DisabledWorkstationAdapter().queue_verification(make_job(make_candidate()))
    assert outcome.status is ImportVerificationStatus.UNAVAILABLE
    assert outcome.status is not ImportVerificationStatus.FAILED


def test_a_declared_free_profile_report_supports_nothing() -> None:
    report = DisabledWorkstationAdapter().supported_target_profiles()
    for family in ControllerFamily:
        assert report.supports(family) is False


# ---------------------------------------------------------------------------
# The invariant: no verified level without a workstation
# ---------------------------------------------------------------------------
def test_a_disabled_adapter_caps_at_the_offline_level() -> None:
    outcome = DisabledWorkstationAdapter().queue_verification(make_job(make_candidate()))
    assert outcome.attained_validation_level is MAX_OFFLINE_VALIDATION_LEVEL
    assert (
        VALIDATION_LEVEL_ORDER[outcome.attained_validation_level]
        < VALIDATION_LEVEL_ORDER[ValidationLevel.ROCKWELL_IMPORT_VERIFIED]
    )
    assert outcome.verified is False


def test_a_disabled_outcome_lowers_any_level_that_claims_verification() -> None:
    """``cap`` is what callers use instead of assigning a level directly."""
    outcome = DisabledWorkstationAdapter().queue_verification(make_job(make_candidate()))
    assert outcome.cap(ValidationLevel.ROCKWELL_IMPORT_VERIFIED) is MAX_OFFLINE_VALIDATION_LEVEL
    assert outcome.cap(ValidationLevel.RELEASED) is MAX_OFFLINE_VALIDATION_LEVEL
    assert outcome.cap(ValidationLevel.GENERATED) is ValidationLevel.GENERATED


@pytest.mark.parametrize(
    "adapter_status",
    [status for status in WorkstationAdapterStatus if status is not WorkstationAdapterStatus.READY],
)
def test_a_pass_cannot_be_constructed_without_a_ready_adapter(
    adapter_status: WorkstationAdapterStatus,
) -> None:
    """There is no code path to ROCKWELL_IMPORT_VERIFIED without a workstation.

    Not "the disabled adapter does not currently return PASSED" — the outcome
    object refuses to exist, so a future caller that assembles one by hand fails
    at construction rather than quietly overclaiming.
    """
    with pytest.raises(ValueError, match="READY"):
        VerificationOutcome(
            status=ImportVerificationStatus.PASSED,
            adapter_status=adapter_status,
        )


@pytest.mark.parametrize(
    "status",
    [
        status
        for status in ImportVerificationStatus
        if status is not ImportVerificationStatus.PASSED
    ],
)
def test_only_a_pass_reaches_the_verified_level(status: ImportVerificationStatus) -> None:
    outcome = VerificationOutcome(status=status, adapter_status=WorkstationAdapterStatus.READY)
    assert outcome.attained_validation_level is MAX_OFFLINE_VALIDATION_LEVEL


def test_a_pass_on_a_ready_workstation_is_the_only_route_to_verified() -> None:
    outcome = VerificationOutcome(
        status=ImportVerificationStatus.PASSED,
        adapter_status=WorkstationAdapterStatus.READY,
    )
    assert outcome.attained_validation_level is ValidationLevel.ROCKWELL_IMPORT_VERIFIED


# ---------------------------------------------------------------------------
# Constructing the real adapter
# ---------------------------------------------------------------------------
def test_the_local_adapter_cannot_be_constructed_while_disabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, enabled=False)
    with pytest.raises(WorkstationAdapterDisabledError) as excinfo:
        LocalWorkstationAdapter(settings)
    assert "ESCO_WORKSTATION_ADAPTER_ENABLED" in str(excinfo.value)


def test_settings_refuse_an_enabled_adapter_with_no_allowlist(tmp_path: Path) -> None:
    """The adapter's precondition is guaranteed one layer up, and stays there."""
    with pytest.raises(PydanticValidationError, match="ALLOWLIST"):
        make_settings(tmp_path, allowlist=[])


def test_the_local_adapter_refuses_an_empty_allowlist_it_is_handed_anyway(
    enabled_settings: Settings,
) -> None:
    """``model_copy`` bypasses validation the way a future refactor might."""
    settings = enabled_settings.model_copy(update={"workstation_executable_allowlist": []})
    with pytest.raises(WorkstationAdapterDisabledError, match="allowlist"):
        LocalWorkstationAdapter(settings)


def test_the_local_adapter_refuses_to_run_without_an_isolated_directory(
    enabled_settings: Settings,
) -> None:
    settings = enabled_settings.model_copy(update={"workstation_working_dir": None})
    with pytest.raises(WorkstationAdapterDisabledError, match="WORKING_DIR"):
        LocalWorkstationAdapter(settings)


def test_a_broken_configuration_degrades_to_a_reported_absence(
    enabled_settings: Settings,
) -> None:
    """An administrator who mistyped a path gets an explanation, not a crash."""
    settings = enabled_settings.model_copy(update={"workstation_working_dir": None})
    adapter = get_adapter(settings)
    assert isinstance(adapter, DisabledWorkstationAdapter)
    assert adapter.health().status is WorkstationAdapterStatus.NOT_CONFIGURED
    assert "WORKING_DIR" in adapter.health().detail


def test_get_adapter_returns_the_real_one_only_when_configured(
    enabled_settings: Settings,
) -> None:
    adapter = get_adapter(enabled_settings)
    assert isinstance(adapter, LocalWorkstationAdapter)
    assert adapter.health().status is WorkstationAdapterStatus.READY


# ---------------------------------------------------------------------------
# Refusal: the executable allowlist
# ---------------------------------------------------------------------------
def test_an_executable_that_is_not_allowlisted_is_refused(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed" / "tool.exe"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("stand-in", encoding="utf-8")
    other = tmp_path / "other" / "tool.exe"
    other.parent.mkdir(parents=True)
    other.write_text("stand-in", encoding="utf-8")

    allowlist = ExecutableAllowlist.from_paths([allowed])
    assert allowlist.resolve(allowed) == allowed.resolve()
    with pytest.raises(ExecutableNotAllowedError, match="allowlist"):
        allowlist.resolve(other)


def test_a_traversal_segment_cannot_smuggle_a_different_binary(tmp_path: Path) -> None:
    """Same directory prefix, same filename, different program.

    A prefix or basename check passes this. Resolving first is what catches it.
    """
    allowed = tmp_path / "allowed" / "tool.exe"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("stand-in", encoding="utf-8")
    decoy = tmp_path / "other" / "tool.exe"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("a different program entirely", encoding="utf-8")

    allowlist = ExecutableAllowlist.from_paths([allowed])
    smuggled = allowed.parent / ".." / "other" / "tool.exe"
    assert smuggled.exists(), "the decoy must really be reachable by that path"

    with pytest.raises(ExecutableNotAllowedError) as excinfo:
        allowlist.resolve(smuggled)
    assert excinfo.value.details["resolved"] == str(decoy.resolve())


def test_a_traversal_that_resolves_back_to_the_allowlisted_binary_is_allowed(
    tmp_path: Path,
) -> None:
    """The check is on the resolved path, not on how the path was spelled."""
    allowed = tmp_path / "allowed" / "tool.exe"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("stand-in", encoding="utf-8")
    (allowed.parent / "sub").mkdir()

    allowlist = ExecutableAllowlist.from_paths([allowed])
    spelled_oddly = allowed.parent / "sub" / ".." / "tool.exe"
    assert allowlist.resolve(spelled_oddly) == allowed.resolve()


def test_a_symlink_swapped_in_after_configuration_is_refused(tmp_path: Path) -> None:
    """The allowlist pins the resolved path at construction, and that is the point."""
    allowed = tmp_path / "allowed" / "tool.exe"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("the real tool", encoding="utf-8")
    replacement = tmp_path / "other" / "evil.exe"
    replacement.parent.mkdir(parents=True)
    replacement.write_text("not the real tool", encoding="utf-8")

    allowlist = ExecutableAllowlist.from_paths([allowed])

    allowed.unlink()
    try:
        allowed.symlink_to(replacement)
    except (OSError, NotImplementedError):  # pragma: no cover - depends on OS privileges
        pytest.skip("creating a symlink needs a privilege this machine does not grant")

    with pytest.raises(ExecutableNotAllowedError):
        allowlist.resolve(allowed)


def test_a_relative_executable_is_refused(tmp_path: Path) -> None:
    allowed = tmp_path / "tool.exe"
    allowed.write_text("stand-in", encoding="utf-8")
    allowlist = ExecutableAllowlist.from_paths([allowed])
    with pytest.raises(ExecutableNotAllowedError, match="relative"):
        allowlist.resolve("tool.exe")


def test_a_relative_allowlist_entry_is_refused() -> None:
    with pytest.raises(ExecutableNotAllowedError, match="absolute"):
        ExecutableAllowlist.from_paths(["studio5000.exe"])


def test_an_allowlisted_program_that_is_not_installed_is_refused(tmp_path: Path) -> None:
    missing = tmp_path / "not-installed.exe"
    allowlist = ExecutableAllowlist.from_paths([missing])
    with pytest.raises(ExecutableNotAllowedError, match="not an existing file"):
        allowlist.resolve(missing)


# ---------------------------------------------------------------------------
# Refusal: the working directory
# ---------------------------------------------------------------------------
def test_a_working_directory_inside_the_project_store_is_refused(tmp_path: Path) -> None:
    """Never overwrite a production project."""
    settings = make_settings(tmp_path, enabled=False)
    inside_projects = settings.projects_dir / "workstation-scratch"
    with pytest.raises(WorkingDirectoryRefusedError, match="production project"):
        resolve_isolated_directory(inside_projects, forbidden_roots=(settings.projects_dir,))


def test_a_working_directory_that_contains_the_project_store_is_refused(
    tmp_path: Path,
) -> None:
    """Cleaning up the scratch root would otherwise remove every project."""
    settings = make_settings(tmp_path, enabled=False)
    with pytest.raises(WorkingDirectoryRefusedError, match="contains"):
        resolve_isolated_directory(settings.data_dir, forbidden_roots=(settings.projects_dir,))


def test_the_adapter_cannot_be_constructed_over_the_project_store(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, working_dir=tmp_path / "data" / "projects" / "scratch")
    with pytest.raises(WorkingDirectoryRefusedError, match="production project"):
        LocalWorkstationAdapter(settings)


def test_a_job_pointing_at_a_project_directory_is_refused(
    enabled_settings: Settings, tmp_path: Path
) -> None:
    """The refusal that stops a verification run destroying the thing it checks."""
    adapter = LocalWorkstationAdapter(enabled_settings)
    candidate = make_candidate(package_path=str(make_package(tmp_path)))
    project_dir = enabled_settings.project_dir(candidate.project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    job = make_job(candidate, working_directory=str(project_dir))

    with pytest.raises(WorkingDirectoryRefusedError):
        adapter.queue_verification(job)


def test_a_job_escaping_the_scratch_root_with_traversal_is_refused(
    enabled_settings: Settings, tmp_path: Path
) -> None:
    adapter = LocalWorkstationAdapter(enabled_settings)
    candidate = make_candidate(package_path=str(make_package(tmp_path)))
    job = make_job(candidate, working_directory="../elsewhere")

    with pytest.raises(WorkingDirectoryRefusedError):
        adapter.queue_verification(job)


def test_the_runner_refuses_a_directory_that_does_not_exist(tmp_path: Path) -> None:
    allowlist = ExecutableAllowlist.from_paths([STAND_IN_EXECUTABLE])
    with pytest.raises(WorkingDirectoryRefusedError, match="does not exist"):
        run_allowlisted(
            STAND_IN_EXECUTABLE,
            ("-c", "pass"),
            allowlist=allowlist,
            working_directory=tmp_path / "never-created",
            timeout_seconds=10,
        )


def test_the_runner_refuses_a_run_with_no_timeout(tmp_path: Path) -> None:
    allowlist = ExecutableAllowlist.from_paths([STAND_IN_EXECUTABLE])
    with pytest.raises(WorkingDirectoryRefusedError, match="not a timeout"):
        run_allowlisted(
            STAND_IN_EXECUTABLE,
            ("-c", "pass"),
            allowlist=allowlist,
            working_directory=tmp_path,
            timeout_seconds=0,
        )


# ---------------------------------------------------------------------------
# Refusal: the job itself
# ---------------------------------------------------------------------------
def test_a_job_without_a_named_human_approver_is_refused(
    enabled_settings: Settings, tmp_path: Path
) -> None:
    """A job runs licensed software against generated files on a real machine.

    ``requested_by_id`` is set here and is deliberately not enough: asking for a
    run and authorising one are different acts.
    """
    adapter = LocalWorkstationAdapter(enabled_settings)
    candidate = make_candidate(package_path=str(make_package(tmp_path)))
    job = make_job(candidate, approved=False)
    assert job.requested_by_id is not None

    with pytest.raises(VerificationRefusedError, match="named human approver"):
        adapter.queue_verification(job)


@pytest.mark.parametrize(
    "status",
    [
        ReleaseCandidateStatus.DRAFT,
        ReleaseCandidateStatus.GENERATING,
        ReleaseCandidateStatus.VALIDATION_FAILED,
        ReleaseCandidateStatus.FAILED,
        ReleaseCandidateStatus.SUPERSEDED,
        ReleaseCandidateStatus.RELEASED,
    ],
)
def test_a_candidate_that_is_not_generated_is_refused(
    enabled_settings: Settings, tmp_path: Path, status: ReleaseCandidateStatus
) -> None:
    adapter = LocalWorkstationAdapter(enabled_settings)
    candidate = make_candidate(status=status, package_path=str(make_package(tmp_path)))

    with pytest.raises(VerificationRefusedError, match="actually been generated"):
        adapter.queue_verification(make_job(candidate))


def test_a_job_with_no_candidate_is_refused(enabled_settings: Settings) -> None:
    adapter = LocalWorkstationAdapter(enabled_settings)
    job = WorkstationVerificationJob(id=uuid.uuid4(), approved_by_id=uuid.uuid4())
    with pytest.raises(VerificationRefusedError, match="no release candidate"):
        adapter.queue_verification(job)


def test_a_candidate_with_no_package_is_refused(enabled_settings: Settings) -> None:
    adapter = LocalWorkstationAdapter(enabled_settings)
    with pytest.raises(VerificationRefusedError, match="no package path"):
        adapter.queue_verification(make_job(make_candidate()))


def test_a_candidate_whose_package_is_missing_is_refused(
    enabled_settings: Settings, tmp_path: Path
) -> None:
    adapter = LocalWorkstationAdapter(enabled_settings)
    candidate = make_candidate(package_path=str(tmp_path / "never-assembled.zip"))
    with pytest.raises(VerificationRefusedError, match="does not exist"):
        adapter.queue_verification(make_job(candidate))


# ---------------------------------------------------------------------------
# Running: pass, fail, timeout
# ---------------------------------------------------------------------------
def test_a_successful_run_is_the_only_thing_that_reaches_verified(
    enabled_settings: Settings, tmp_path: Path
) -> None:
    adapter = LocalWorkstationAdapter(
        enabled_settings, command=echo_command("import sys; sys.exit(0)")
    )
    candidate = make_candidate(package_path=str(make_package(tmp_path)))
    outcome = adapter.queue_verification(make_job(candidate))

    assert outcome.status is ImportVerificationStatus.PASSED
    assert outcome.adapter_status is WorkstationAdapterStatus.READY
    assert outcome.attained_validation_level is ValidationLevel.ROCKWELL_IMPORT_VERIFIED
    assert outcome.exit_code == 0
    assert outcome.started_at is not None and outcome.finished_at is not None


def test_a_failing_run_is_reported_as_failed_and_stays_capped(
    enabled_settings: Settings, tmp_path: Path
) -> None:
    adapter = LocalWorkstationAdapter(
        enabled_settings,
        command=echo_command("import sys; sys.stderr.write('IMPORT ERROR 7'); sys.exit(7)"),
    )
    candidate = make_candidate(package_path=str(make_package(tmp_path)))
    outcome = adapter.queue_verification(make_job(candidate))

    assert outcome.status is ImportVerificationStatus.FAILED
    assert outcome.exit_code == 7
    assert outcome.attained_validation_level is MAX_OFFLINE_VALIDATION_LEVEL
    assert "IMPORT ERROR 7" in outcome.log_excerpt
    assert outcome.diagnostics and outcome.diagnostics[0].severity == "error"


def test_a_run_that_exceeds_its_timeout_is_killed_and_recorded_as_an_error(
    tmp_path: Path,
) -> None:
    """Killed, not left hanging — and ``ERROR``, not ``FAILED``.

    Nothing was established about the package, so recording a failure would be
    reporting a result nobody obtained.
    """
    settings = make_settings(tmp_path, timeout_seconds=1)
    assert settings.workstation_working_dir is not None
    settings.workstation_working_dir.mkdir(parents=True, exist_ok=True)

    adapter = LocalWorkstationAdapter(
        settings, command=echo_command("import time; time.sleep(120)")
    )
    candidate = make_candidate(package_path=str(make_package(tmp_path)))
    outcome = adapter.queue_verification(make_job(candidate))

    assert outcome.status is ImportVerificationStatus.ERROR
    assert outcome.attained_validation_level is MAX_OFFLINE_VALIDATION_LEVEL
    assert outcome.duration_seconds < 60, "the process was killed, not waited out"
    assert "timeout" in outcome.detail.lower()


def test_a_job_may_ask_for_less_time_but_not_more(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, timeout_seconds=2)
    assert settings.workstation_working_dir is not None
    settings.workstation_working_dir.mkdir(parents=True, exist_ok=True)
    adapter = LocalWorkstationAdapter(
        settings, command=echo_command("import time; time.sleep(120)")
    )
    candidate = make_candidate(package_path=str(make_package(tmp_path)))
    job = make_job(candidate, timeout_seconds=10_000)

    outcome = adapter.queue_verification(job)
    assert outcome.status is ImportVerificationStatus.ERROR
    assert outcome.duration_seconds < 60, "the administrator's ceiling is the ceiling"


def test_the_run_happens_inside_the_isolated_directory(
    enabled_settings: Settings, tmp_path: Path
) -> None:
    adapter = LocalWorkstationAdapter(
        enabled_settings,
        command=echo_command("import pathlib, sys; sys.stdout.write(str(pathlib.Path.cwd()))"),
    )
    candidate = make_candidate(package_path=str(make_package(tmp_path)))
    outcome = adapter.queue_verification(make_job(candidate))

    assert outcome.status is ImportVerificationStatus.PASSED
    working_root = enabled_settings.workstation_working_dir
    assert working_root is not None
    assert Path(outcome.working_directory).resolve().is_relative_to(working_root.resolve())
    printed = outcome.log_excerpt.split("[stdout]\n", 1)[1].strip()
    assert Path(printed).resolve() == Path(outcome.working_directory).resolve()


def test_arguments_are_passed_as_arguments_and_never_through_a_shell(
    tmp_path: Path,
) -> None:
    """A value containing shell metacharacters stays one argument."""
    allowlist = ExecutableAllowlist.from_paths([STAND_IN_EXECUTABLE])
    hostile = "package.zip && echo pwned"
    result = run_allowlisted(
        STAND_IN_EXECUTABLE,
        ("-c", "import sys; sys.stdout.write(sys.argv[1])", hostile),
        allowlist=allowlist,
        working_directory=tmp_path,
        timeout_seconds=60,
    )
    assert result.succeeded
    assert result.stdout == hostile
    assert "pwned\n" not in result.stdout


def test_the_child_environment_drops_credential_shaped_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ESCO_JWT_SECRET", "not-a-real-secret")
    monkeypatch.setenv("SOME_API_KEY", "not-a-real-key")
    monkeypatch.setenv("PATH_TO_TOOLING", "kept")

    environment = scrubbed_environment()
    assert "ESCO_JWT_SECRET" not in environment
    assert "SOME_API_KEY" not in environment
    assert environment["PATH_TO_TOOLING"] == "kept"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def test_the_capability_row_matches_the_plugin_registry_shape(tmp_path: Path) -> None:
    """Same keys as ``PluginRegistry.capability_rows()``, so one route serves both."""
    plugin_row = PluginRegistry().capability_rows()[0]
    row = capability_row(get_adapter(make_settings(tmp_path, enabled=False)))

    assert set(row) == set(plugin_row)
    status = CapabilityStatus(**row)
    assert status.name == CAPABILITY_NAME
    assert status.available is False
    assert "ESCO_WORKSTATION_ADAPTER_ENABLED" in status.detail


def test_the_capability_row_is_available_when_a_workstation_is_configured(
    enabled_settings: Settings,
) -> None:
    rows = capability_rows(enabled_settings)
    assert len(rows) == 1
    assert rows[0]["available"] is True
    assert rows[0]["detail"] == "", "a present capability needs no explanation"
    assert STAND_IN_EXECUTABLE.name in rows[0]["provider"]


def test_installed_products_never_invent_a_version(enabled_settings: Settings) -> None:
    inventory = LocalWorkstationAdapter(enabled_settings).installed_products()
    assert inventory.products
    for product in inventory.products:
        assert product.version == ""
        assert "no version was queried" in product.detail.lower()


def test_no_target_profile_is_claimed_unless_a_human_declared_it(
    enabled_settings: Settings,
) -> None:
    report = LocalWorkstationAdapter(enabled_settings).supported_target_profiles()
    assert report.profiles == ()
    assert report.available is False
    assert report.supports(ControllerFamily.CONTROLLOGIX) is False

    declared = LocalWorkstationAdapter(
        enabled_settings,
        profiles=[
            TargetProfileSupport(
                software_product="Studio 5000 Logix Designer",
                software_version="35.00",
                controller_families=(ControllerFamily.CONTROLLOGIX,),
            )
        ],
    ).supported_target_profiles()
    assert declared.supports(ControllerFamily.CONTROLLOGIX) is True
    assert declared.supports(ControllerFamily.GUARDLOGIX) is False
    assert declared.supports(ControllerFamily.CONTROLLOGIX, software_version="34.00") is False


def test_health_reports_unreachable_when_the_tooling_is_not_installed(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "Studio5000" / "LogixDesigner.exe"
    settings = make_settings(tmp_path, allowlist=[str(missing)])
    assert settings.workstation_working_dir is not None
    settings.workstation_working_dir.mkdir(parents=True, exist_ok=True)

    health = LocalWorkstationAdapter(settings).health()
    assert health.status is WorkstationAdapterStatus.UNREACHABLE
    assert health.available is False
    assert str(missing) in health.detail


def test_an_unhealthy_adapter_returns_an_error_outcome_rather_than_running(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "Studio5000" / "LogixDesigner.exe"
    settings = make_settings(tmp_path, allowlist=[str(missing)])
    assert settings.workstation_working_dir is not None
    settings.workstation_working_dir.mkdir(parents=True, exist_ok=True)

    adapter = LocalWorkstationAdapter(settings)
    candidate = make_candidate(package_path=str(make_package(tmp_path)))
    outcome = adapter.queue_verification(make_job(candidate))

    assert outcome.status is ImportVerificationStatus.ERROR
    assert outcome.attained_validation_level is MAX_OFFLINE_VALIDATION_LEVEL


def test_outcome_fields_map_onto_real_job_columns() -> None:
    """``as_job_fields`` exists so the service layer can persist without guessing."""
    outcome = DisabledWorkstationAdapter().queue_verification(make_job(make_candidate()))
    columns = {attribute.key for attribute in sa_inspect(WorkstationVerificationJob).attrs}
    assert set(outcome.as_job_fields()) <= columns


def test_diagnostics_are_read_back_from_what_the_caller_persisted(
    enabled_settings: Settings, tmp_path: Path
) -> None:
    adapter = LocalWorkstationAdapter(enabled_settings)
    candidate = make_candidate(package_path=str(make_package(tmp_path)))
    job = make_job(candidate)
    job.diagnostics = [
        {"severity": "error", "message": "Data type MTR_STATUS not found", "source": "import"}
    ]
    job.log_excerpt = "Import aborted."
    job.status = ImportVerificationStatus.FAILED

    report = adapter.collect_diagnostics(job)
    assert report.job_status is ImportVerificationStatus.FAILED
    assert len(report.diagnostics) == 1
    assert report.diagnostics[0].message == "Data type MTR_STATUS not found"
    assert report.log_excerpt == "Import aborted."


def test_health_is_stamped_with_the_time_it_was_checked() -> None:
    health: AdapterHealth = DisabledWorkstationAdapter().health()
    assert health.checked_at.tzinfo is not None


def test_the_stand_in_executable_is_the_interpreter_and_nothing_rockwell() -> None:
    """Stated so nobody later swaps it for real tooling to make a test pass."""
    assert STAND_IN_EXECUTABLE == Path(sys.executable).resolve()
    assert "rockwell" not in str(STAND_IN_EXECUTABLE).lower()
