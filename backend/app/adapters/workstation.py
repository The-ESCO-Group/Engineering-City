"""The optional Rockwell workstation adapter: an interface, and a refusal.

``ROCKWELL_IMPORT_VERIFIED`` is the one validation level this backend cannot
reach on its own. Everything it can do offline stops at ``ROUND_TRIP_PARSED`` —
proving our own generator's output parses back into the same model, which is
worth real money and is *not* the same as proving Studio 5000 will import it.
Closing that gap honestly needs a machine with the licensed software on it, so
this package describes what such a machine would be asked to do, and what
happens when there isn't one.

What happens when there isn't one is the normal case, and it is the default:
:class:`DisabledWorkstationAdapter` answers every question with a structured
"unavailable, and here is why". It does not raise, because a missing optional
capability is not an error — the product still generates packages, they are
still labelled *import-ready candidates*, and the capability report says
plainly that no workstation is configured. Every deployment that never buys a
licence lives here forever, and it works.

The one invariant that must survive every future edit to this file:

    **With the adapter disabled, no code path can set**
    ``ValidationLevel.ROCKWELL_IMPORT_VERIFIED``.

It is enforced structurally rather than by convention.
:class:`VerificationOutcome` refuses to exist with ``status=PASSED`` unless it
came from an adapter reporting ``WorkstationAdapterStatus.READY``, and
:attr:`VerificationOutcome.attained_validation_level` returns
``MAX_OFFLINE_VALIDATION_LEVEL`` for everything else. A disabled adapter cannot
report ``READY``, so there is no sequence of calls that produces a verified
level without a real, enabled, configured workstation behind it. The test suite
asserts this directly rather than trusting the reading.

**Nothing in this package connects to a controller.** There is no upload, no
download, no online edit, no mode change, no force and no firmware operation —
absent, not disabled, not feature-flagged. The adapter's entire vocabulary is
"run this allowlisted program against these generated files in this scratch
directory, and tell me what it printed". The only thing it can be pointed at is
official Rockwell tooling an administrator installed and named; no proprietary
protocol is emulated and no proprietary format is fabricated.

Enabling it requires, all of them, and each is checked:

* an administrator setting ``ESCO_WORKSTATION_ADAPTER_ENABLED``;
* a local licensed workstation with the tooling installed;
* an explicit absolute allowlist of executables;
* a release candidate that has actually been generated;
* a **named human approver** on the job — an agent's say-so is not enough;
* an isolated working directory that is not, and does not contain, the project
  store;
* a timeout.

The adapter never touches the database. It returns structured outcomes and the
caller persists them, so the audit row, the job status and the transaction
boundary stay with the service layer that owns them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.adapters.subprocess_runner import (
    CommandResult,
    ExecutableAllowlist,
    resolve_isolated_directory,
    run_allowlisted,
)
from app.core.clock import utcnow
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
from app.core.errors import AppError, CapabilityUnavailableError
from app.core.logging import get_logger
from app.models.release import ReleaseCandidate, WorkstationVerificationJob

logger = get_logger(__name__)

#: The capability name reported by ``GET /api/system/capabilities``.
CAPABILITY_NAME = "rockwell_workstation_adapter"

#: Said in one place, and repeated verbatim wherever the absence is reported, so
#: an operator reading the API, the logs and the manifest sees the same sentence.
DISABLED_DETAIL = (
    "No licensed Rockwell workstation is configured, so import verification "
    "cannot be attempted. Generated packages stay labelled import-ready "
    "candidates and stop at the round-trip-parsed validation level. Set "
    "ESCO_WORKSTATION_ADAPTER_ENABLED and ESCO_WORKSTATION_EXECUTABLE_ALLOWLIST "
    "on a machine that holds the licence to change that."
)

#: Statuses a candidate may hold when a verification job is allowed to start.
#: A draft has nothing to import, a candidate still generating has an incomplete
#: package, and one whose validation failed must be fixed rather than carried to
#: a workstation to fail again more expensively.
VERIFIABLE_CANDIDATE_STATUSES: frozenset[ReleaseCandidateStatus] = frozenset(
    {
        ReleaseCandidateStatus.GENERATED,
        ReleaseCandidateStatus.AWAITING_APPROVAL,
        ReleaseCandidateStatus.APPROVED,
    }
)


class WorkstationAdapterDisabledError(CapabilityUnavailableError):
    """A real adapter was constructed while the capability is switched off.

    Reaching this is a programming error rather than a user error: the whole
    point of :func:`get_adapter` is that callers never have to ask. It is raised
    from :meth:`LocalWorkstationAdapter.__init__` so that "the adapter exists"
    and "an administrator enabled the adapter" cannot come apart.
    """

    code = "workstation_adapter_disabled"


class VerificationRefusedError(AppError):
    """The job itself is not allowed to start.

    Distinct from the adapter being unavailable. The adapter is right here and
    working; this particular request does not meet the conditions — no named
    approver, a candidate that was never generated, a working directory pointing
    somewhere it must not.
    """

    status_code = 409
    code = "verification_refused"


# ---------------------------------------------------------------------------
# Structured answers
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AdapterHealth:
    """Whether the adapter can be used, and why not when it cannot."""

    status: WorkstationAdapterStatus
    adapter_key: str = ""
    detail: str = ""
    checked_at: datetime = field(default_factory=utcnow)

    @property
    def available(self) -> bool:
        return self.status is WorkstationAdapterStatus.READY


@dataclass(frozen=True, slots=True)
class InstalledProduct:
    """One program the adapter is permitted to run.

    ``version`` is empty unless something actually told us a version. The
    adapter does not scan the registry or guess from a path, because a guessed
    Studio version in a release manifest is worse than a blank one: it looks
    like a fact.
    """

    name: str
    path: str = ""
    version: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ProductInventory:
    """What is installed, or a statement that nothing could be inspected."""

    status: WorkstationAdapterStatus
    products: tuple[InstalledProduct, ...] = ()
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.status is WorkstationAdapterStatus.READY and bool(self.products)


@dataclass(frozen=True, slots=True)
class TargetProfileSupport:
    """A target profile a workstation has been declared able to import for."""

    software_product: str
    software_version: str = ""
    controller_families: tuple[ControllerFamily, ...] = ()
    l5x_schema_revision: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ProfileSupportReport:
    """Which profiles are supported — empty meaning *none are claimed*."""

    status: WorkstationAdapterStatus
    profiles: tuple[TargetProfileSupport, ...] = ()
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.status is WorkstationAdapterStatus.READY and bool(self.profiles)

    def supports(self, family: ControllerFamily, software_version: str = "") -> bool:
        """True only if a profile was explicitly declared for this target.

        An empty report supports nothing. Silence is not consent: a workstation
        that has not been declared able to import for GuardLogix must not be
        asked to prove a GuardLogix package.
        """
        for profile in self.profiles:
            if family not in profile.controller_families:
                continue
            if software_version and profile.software_version != software_version:
                continue
            return True
        return False


@dataclass(frozen=True, slots=True)
class VerificationDiagnostic:
    """One message the import reported."""

    severity: str
    message: str
    source: str = ""
    location: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
            "location": self.location,
        }


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """The result of asking an adapter to verify a package.

    The invariant this class exists to hold: ``status=PASSED`` is constructible
    **only** with ``adapter_status=READY``. A disabled or unconfigured adapter
    cannot report ``READY``, so it cannot manufacture a pass, so it cannot lift
    a candidate to ``ROCKWELL_IMPORT_VERIFIED``. Checking it in ``__post_init__``
    rather than at each call site means a future caller that gets it wrong fails
    immediately and loudly instead of quietly overclaiming.
    """

    status: ImportVerificationStatus
    adapter_status: WorkstationAdapterStatus
    detail: str = ""
    adapter_key: str = ""
    diagnostics: tuple[VerificationDiagnostic, ...] = ()
    log_excerpt: str = ""
    working_directory: str = ""
    exit_code: int | None = None
    duration_seconds: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if (
            self.status is ImportVerificationStatus.PASSED
            and self.adapter_status is not WorkstationAdapterStatus.READY
        ):
            raise ValueError(
                "A passed import verification can only come from an adapter reporting "
                f"READY; adapter_status={self.adapter_status.value!r}. Without a licensed "
                "workstation the highest honest level is "
                f"{MAX_OFFLINE_VALIDATION_LEVEL.value!r}."
            )

    @property
    def attained_validation_level(self) -> ValidationLevel:
        """The highest level this outcome justifies, and never more.

        Only an actual pass on a ready workstation reaches
        ``ROCKWELL_IMPORT_VERIFIED``. Everything else — unavailable, queued,
        failed, errored, timed out — caps at
        :data:`~app.core.enums.MAX_OFFLINE_VALIDATION_LEVEL`, because none of
        those establish anything a Studio import would.
        """
        if self.status is ImportVerificationStatus.PASSED:
            return ValidationLevel.ROCKWELL_IMPORT_VERIFIED
        return MAX_OFFLINE_VALIDATION_LEVEL

    @property
    def verified(self) -> bool:
        return self.status is ImportVerificationStatus.PASSED

    def cap(self, level: ValidationLevel) -> ValidationLevel:
        """Lower ``level`` to what this outcome supports, if it claims more.

        Callers use this instead of assigning a level directly, so "the package
        was round-trip parsed" can never be written down as "Studio imported it".
        """
        if VALIDATION_LEVEL_ORDER[level] <= VALIDATION_LEVEL_ORDER[self.attained_validation_level]:
            return level
        return self.attained_validation_level

    def as_job_fields(self) -> dict[str, Any]:
        """The exact column values to write onto a ``WorkstationVerificationJob``.

        Offered so the service layer that owns the transaction can persist an
        outcome without this module reaching into the database.
        """
        return {
            "status": self.status,
            "adapter_key": self.adapter_key,
            "working_directory": self.working_directory,
            "diagnostics": [diagnostic.as_dict() for diagnostic in self.diagnostics],
            "log_excerpt": self.log_excerpt,
            "result_detail": self.detail,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticsReport:
    """Import diagnostics for a job, or a statement that there are none."""

    status: WorkstationAdapterStatus
    job_status: ImportVerificationStatus
    diagnostics: tuple[VerificationDiagnostic, ...] = ()
    log_excerpt: str = ""
    working_directory: str = ""
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.status is WorkstationAdapterStatus.READY


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------
@runtime_checkable
class RockwellWorkstationAdapter(Protocol):
    """What a licensed workstation can be asked, in full.

    Every method returns a structured report rather than raising for an absent
    capability, so an implementation that cannot do something says so in the
    same shape as one that can. Refusals of a specific *request* — a job with no
    approver, an executable that is not allowlisted — do raise, because those
    are decisions about this call and the caller must not be able to miss them.
    """

    @property
    def adapter_key(self) -> str:
        """Which configured workstation this is.

        A site may have more than one, at different Studio versions. Declared
        read-only so an implementation is free to make it a frozen dataclass
        field — an adapter's identity is not something a caller reassigns.
        """
        ...

    def health(self) -> AdapterHealth:
        """Can this adapter be used right now, and if not, why not."""
        ...

    def installed_products(self) -> ProductInventory:
        """Which official products this adapter may drive."""
        ...

    def supported_target_profiles(self) -> ProfileSupportReport:
        """Which target profiles this workstation is declared able to import for."""
        ...

    def queue_verification(self, job: WorkstationVerificationJob) -> VerificationOutcome:
        """Run, or refuse to run, one import-verification job."""
        ...

    def collect_diagnostics(self, job: WorkstationVerificationJob) -> DiagnosticsReport:
        """Everything the import reported for ``job``."""
        ...


# ---------------------------------------------------------------------------
# The default
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DisabledWorkstationAdapter:
    """The default adapter: present, honest, and incapable of verifying anything.

    Modelled on the plugin registry's treatment of an absent delivery package —
    a missing optional capability degrades the product to a clearly-reported
    absence, it does not break it. Nothing here raises, so no caller needs a
    branch for "there is no workstation"; they get an outcome that says
    ``UNAVAILABLE`` and caps at the offline level, and they record it.
    """

    adapter_key: str = ""
    status: WorkstationAdapterStatus = WorkstationAdapterStatus.DISABLED
    reason: str = DISABLED_DETAIL

    def health(self) -> AdapterHealth:
        return AdapterHealth(status=self.status, adapter_key=self.adapter_key, detail=self.reason)

    def installed_products(self) -> ProductInventory:
        return ProductInventory(status=self.status, products=(), detail=self.reason)

    def supported_target_profiles(self) -> ProfileSupportReport:
        return ProfileSupportReport(status=self.status, profiles=(), detail=self.reason)

    def queue_verification(self, job: WorkstationVerificationJob) -> VerificationOutcome:
        """Refuse the job as a *result*, not an exception.

        ``UNAVAILABLE`` rather than ``FAILED``: the package was not found
        wanting, it was never examined, and those two must not look the same in
        a manifest.
        """
        logger.info(
            "import verification requested with no workstation adapter configured",
            extra={"job_id": str(getattr(job, "id", "")), "adapter_status": self.status.value},
        )
        return VerificationOutcome(
            status=ImportVerificationStatus.UNAVAILABLE,
            adapter_status=self.status,
            adapter_key=self.adapter_key,
            detail=self.reason,
        )

    def collect_diagnostics(self, job: WorkstationVerificationJob) -> DiagnosticsReport:
        return DiagnosticsReport(
            status=self.status,
            job_status=ImportVerificationStatus.UNAVAILABLE,
            detail=self.reason,
        )


# ---------------------------------------------------------------------------
# The real one
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class VerificationCommand:
    """The program and arguments an administrator configured for verification.

    Deliberately data rather than a code path per product: this backend does not
    know what a given site's licensed tooling is called or how it is invoked,
    and inventing a command line for software we have never run would be a
    guess dressed as support. ``{package_path}`` and ``{working_dir}`` are
    substituted; everything else is passed through untouched, as separate
    arguments, never through a shell.
    """

    executable: str
    arguments: tuple[str, ...] = ()

    def render(self, *, package_path: str, working_dir: str) -> tuple[str, ...]:
        return tuple(
            argument.replace("{package_path}", package_path).replace("{working_dir}", working_dir)
            for argument in self.arguments
        )


class LocalWorkstationAdapter:
    """Drives allowlisted local tooling on a licensed workstation.

    Constructing this **is** the assertion that an administrator turned the
    capability on. There is no ``enabled`` flag to check afterwards and no
    "degraded mode": if the setting is off, the constructor raises, which is
    why :func:`get_adapter` is the only sanctioned way to obtain an adapter.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        command: VerificationCommand | None = None,
        profiles: Sequence[TargetProfileSupport] = (),
    ) -> None:
        if not settings.workstation_adapter_enabled:
            raise WorkstationAdapterDisabledError(
                "The Rockwell workstation adapter is disabled. It drives licensed "
                "software against generated files on a real machine, so it cannot be "
                "constructed until an administrator sets "
                "ESCO_WORKSTATION_ADAPTER_ENABLED on a workstation that holds the "
                "licence.",
                details={"setting": "ESCO_WORKSTATION_ADAPTER_ENABLED"},
            )

        self._settings = settings
        self.adapter_key = settings.workstation_adapter_key
        self.allowlist = ExecutableAllowlist.from_paths(settings.workstation_executable_allowlist)
        if not self.allowlist:
            raise WorkstationAdapterDisabledError(
                "The workstation executable allowlist is empty, so the adapter could "
                "never run the official tooling it exists to drive.",
                details={"setting": "ESCO_WORKSTATION_EXECUTABLE_ALLOWLIST"},
            )

        working_dir = settings.workstation_working_dir
        if working_dir is None:
            raise WorkstationAdapterDisabledError(
                "ESCO_WORKSTATION_WORKING_DIR is unset. Import verification unpacks and "
                "writes files, and it is given an isolated scratch directory rather "
                "than being allowed to choose one.",
                details={"setting": "ESCO_WORKSTATION_WORKING_DIR"},
            )

        # Proves the root is absolute, is not inside the project store and does
        # not contain it. Done in the constructor so a dangerous configuration
        # cannot produce a usable adapter at all.
        self.working_root = resolve_isolated_directory(
            Path(working_dir), forbidden_roots=self.forbidden_roots
        )
        self.command = command or VerificationCommand(executable=str(self.allowlist.entries[0]))
        self._profiles = tuple(profiles)

    # -- configuration ----------------------------------------------------
    @property
    def forbidden_roots(self) -> tuple[Path, ...]:
        """Directories a verification run may never write into.

        The project store, first and foremost. A run that could be pointed at
        ``data/projects/`` could overwrite the production project whose package
        it was asked to check.
        """
        return (self._settings.projects_dir,)

    @property
    def timeout_seconds(self) -> int:
        return self._settings.workstation_timeout_seconds

    # -- interface --------------------------------------------------------
    def health(self) -> AdapterHealth:
        """Report configuration state, and nothing stronger.

        ``READY`` means the allowlisted programs exist on this machine and the
        scratch directory is usable. It does not mean Studio 5000 is licensed,
        activated, or willing — none of which can be established without running
        it, and none of which this method pretends to know.
        """
        missing = [str(entry) for entry in self.allowlist.entries if not entry.is_file()]
        if missing:
            return AdapterHealth(
                status=WorkstationAdapterStatus.UNREACHABLE,
                adapter_key=self.adapter_key,
                detail=(
                    "Allowlisted programs are not present on this machine: "
                    f"{', '.join(missing)}. The adapter is enabled but has nothing to run."
                ),
            )
        if not self.working_root.is_dir():
            return AdapterHealth(
                status=WorkstationAdapterStatus.NOT_CONFIGURED,
                adapter_key=self.adapter_key,
                detail=(
                    f"The isolated working directory {self.working_root} does not exist. "
                    f"Create it on the workstation before queueing a job."
                ),
            )
        return AdapterHealth(
            status=WorkstationAdapterStatus.READY,
            adapter_key=self.adapter_key,
            detail=(
                "Configured, with an allowlist of "
                f"{len(self.allowlist.entries)} program(s) and an isolated working "
                f"directory. Whether the licensed software will actually import a "
                f"package is established by running it, not by this check."
            ),
        )

    def installed_products(self) -> ProductInventory:
        """The allowlisted programs, reported as what they are.

        Not a registry scan and not a version probe. The adapter reports the
        programs an administrator named and says explicitly that no version was
        queried, because a version number in a release manifest that was guessed
        from a directory name is a fabricated fact.
        """
        products = tuple(
            InstalledProduct(
                name=entry.name,
                path=str(entry),
                version="",
                detail=(
                    "Named in the administrator's allowlist and present on disk. No "
                    "version was queried; the adapter does not interrogate installed "
                    "software."
                )
                if entry.is_file()
                else "Named in the administrator's allowlist but not present on disk.",
            )
            for entry in self.allowlist.entries
        )
        return ProductInventory(
            status=self.health().status,
            products=products,
            detail="Derived from ESCO_WORKSTATION_EXECUTABLE_ALLOWLIST.",
        )

    def supported_target_profiles(self) -> ProfileSupportReport:
        """Only what a human declared this workstation can import for.

        An empty report is the honest default and means *no profile is claimed*.
        The adapter has no way to discover which controller families and schema
        revisions a given installation handles, and assuming it handles the one
        in front of it is exactly the overclaim this product exists to avoid.
        """
        if not self._profiles:
            return ProfileSupportReport(
                status=self.health().status,
                profiles=(),
                detail=(
                    "No target profile has been declared for this workstation, so none "
                    "is claimed as supported. Declare the products and controller "
                    "families this machine can import for before relying on its result."
                ),
            )
        return ProfileSupportReport(
            status=self.health().status,
            profiles=self._profiles,
            detail="Declared by the administrator who configured this workstation.",
        )

    def queue_verification(self, job: WorkstationVerificationJob) -> VerificationOutcome:
        """Check every precondition, then run the configured command.

        Raises :class:`VerificationRefusedError` when the job may not start.
        Returns an outcome — including a failed or errored one — when it did.
        """
        candidate = self._require_candidate(job)
        self._require_named_approver(job)
        self._require_generated_candidate(candidate)
        package_path = self._require_package(candidate)
        working_directory = self._require_working_directory(job, candidate)

        health = self.health()
        if health.status is not WorkstationAdapterStatus.READY:
            return VerificationOutcome(
                status=ImportVerificationStatus.ERROR,
                adapter_status=health.status,
                adapter_key=self.adapter_key,
                detail=health.detail,
                working_directory=str(working_directory),
            )

        working_directory.mkdir(parents=True, exist_ok=True)
        effective_timeout = self._effective_timeout(job)

        logger.info(
            "workstation verification starting",
            extra={
                "job_id": str(getattr(job, "id", "")),
                "adapter_key": self.adapter_key,
                "release_candidate_id": str(getattr(candidate, "id", "")),
                "working_directory": str(working_directory),
                "timeout_seconds": effective_timeout,
            },
        )

        started_at = utcnow()
        result = run_allowlisted(
            self.command.executable,
            self.command.render(package_path=str(package_path), working_dir=str(working_directory)),
            allowlist=self.allowlist,
            working_directory=working_directory,
            timeout_seconds=effective_timeout,
        )
        finished_at = utcnow()

        return self._outcome_from(result, started_at=started_at, finished_at=finished_at)

    def collect_diagnostics(self, job: WorkstationVerificationJob) -> DiagnosticsReport:
        """Read back what a run recorded on the job.

        The adapter does not keep state between calls, so this reports what the
        caller persisted. The full log stays on the workstation, at
        ``job.working_directory`` — recorded precisely so a failed run can be
        reproduced by hand on the same machine.
        """
        job_status = getattr(job, "status", None) or ImportVerificationStatus.NOT_ATTEMPTED
        raw_diagnostics = getattr(job, "diagnostics", None) or []
        diagnostics = tuple(
            VerificationDiagnostic(
                severity=str(entry.get("severity", "information")),
                message=str(entry.get("message", "")),
                source=str(entry.get("source", "")),
                location=str(entry.get("location", "")),
            )
            for entry in raw_diagnostics
            if isinstance(entry, dict)
        )
        log_excerpt = str(getattr(job, "log_excerpt", "") or "")
        working_directory = str(getattr(job, "working_directory", "") or "")
        return DiagnosticsReport(
            status=self.health().status,
            job_status=job_status,
            diagnostics=diagnostics,
            log_excerpt=log_excerpt,
            working_directory=working_directory,
            detail=(
                "Recorded by the run itself; the complete log stays on the workstation."
                if diagnostics or log_excerpt
                else "This job has not recorded any diagnostics."
            ),
        )

    # -- refusals ---------------------------------------------------------
    @staticmethod
    def _require_candidate(job: WorkstationVerificationJob) -> ReleaseCandidate:
        try:
            candidate = job.release_candidate
        except Exception as exc:  # pragma: no cover - detached session, not a normal path
            raise VerificationRefusedError(
                "The verification job's release candidate could not be loaded, so there "
                "is nothing to verify.",
                details={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        if candidate is None:
            raise VerificationRefusedError(
                "The verification job has no release candidate. A job exists to prove a "
                "specific generated package imports; without one there is nothing to run."
            )
        return candidate

    @staticmethod
    def _require_named_approver(job: WorkstationVerificationJob) -> None:
        """A job runs licensed software against generated files. A human says so.

        ``requested_by_id`` is not a substitute and is not consulted here: asking
        for a run and authorising one are different acts, and letting a request
        approve itself would make the approval ceremonial.
        """
        if getattr(job, "approved_by_id", None) is None:
            raise VerificationRefusedError(
                "This verification job has no named human approver. It would run "
                "licensed software against generated files on a real workstation, and "
                "it does not begin on an agent's say-so.",
                details={"job_id": str(getattr(job, "id", "") or "")},
            )

    @staticmethod
    def _require_generated_candidate(candidate: ReleaseCandidate) -> None:
        status = getattr(candidate, "status", None)
        if status not in VERIFIABLE_CANDIDATE_STATUSES:
            raise VerificationRefusedError(
                f"Release candidate status is {getattr(status, 'value', status)!r}. Import "
                f"verification runs against a package that has actually been generated; "
                f"allowed statuses are "
                f"{sorted(s.value for s in VERIFIABLE_CANDIDATE_STATUSES)}.",
                details={
                    "release_candidate_id": str(getattr(candidate, "id", "") or ""),
                    "status": str(getattr(status, "value", status)),
                },
            )

    @staticmethod
    def _require_package(candidate: ReleaseCandidate) -> Path:
        package_path = str(getattr(candidate, "package_path", "") or "")
        if not package_path:
            raise VerificationRefusedError(
                "The release candidate has no package path, so no files were assembled "
                "for a workstation to import.",
                details={"release_candidate_id": str(getattr(candidate, "id", "") or "")},
            )
        resolved = Path(package_path).resolve()
        if not resolved.exists():
            raise VerificationRefusedError(
                f"The release candidate package {resolved} does not exist on this machine. "
                f"Copy it to the workstation before queueing verification.",
                details={"package_path": str(resolved)},
            )
        return resolved

    def _require_working_directory(
        self, job: WorkstationVerificationJob, candidate: ReleaseCandidate
    ) -> Path:
        """Resolve where this run may write, or refuse.

        A job that names its own directory has it checked against the configured
        isolated root and against the project store. A job that names none gets
        a per-job subdirectory of the root, so two runs cannot tread on each
        other's files.
        """
        requested = str(getattr(job, "working_directory", "") or "")
        if requested:
            return resolve_isolated_directory(
                self.working_root, requested, forbidden_roots=self.forbidden_roots
            )
        job_id = str(getattr(job, "id", "") or "") or str(getattr(candidate, "id", "") or "")
        return resolve_isolated_directory(
            self.working_root,
            f"job-{job_id}" if job_id else "job",
            forbidden_roots=self.forbidden_roots,
        )

    # -- helpers ----------------------------------------------------------
    def _effective_timeout(self, job: WorkstationVerificationJob) -> float:
        """The job's timeout, bounded by the administrator's ceiling.

        A job may ask for less. It may not ask for more: the ceiling is what
        stops one stuck run from holding the site's only licensed workstation.
        """
        ceiling = float(self.timeout_seconds)
        requested = getattr(job, "timeout_seconds", None)
        if requested is None:
            return ceiling
        try:
            requested_value = float(requested)
        except (TypeError, ValueError):
            return ceiling
        if requested_value <= 0:
            return ceiling
        return min(requested_value, ceiling)

    def _outcome_from(
        self, result: CommandResult, *, started_at: datetime, finished_at: datetime
    ) -> VerificationOutcome:
        """Map a completed run onto a verification status.

        A timeout is ``ERROR``, never ``FAILED``. The run was killed, so nothing
        is known about whether the package imports, and recording "failed" would
        be reporting a result nobody obtained.
        """
        if result.timed_out:
            return VerificationOutcome(
                status=ImportVerificationStatus.ERROR,
                adapter_status=WorkstationAdapterStatus.ERROR,
                adapter_key=self.adapter_key,
                detail=(
                    f"The verification run exceeded its {result.timeout_seconds:.0f} second "
                    f"timeout and was killed. Nothing was established about the package; "
                    f"the working directory is kept so the run can be finished by hand."
                ),
                log_excerpt=result.output_excerpt,
                working_directory=result.working_directory,
                exit_code=None,
                duration_seconds=result.duration_seconds,
                started_at=started_at,
                finished_at=finished_at,
            )

        if result.succeeded:
            return VerificationOutcome(
                status=ImportVerificationStatus.PASSED,
                adapter_status=WorkstationAdapterStatus.READY,
                adapter_key=self.adapter_key,
                detail="The configured verification command completed successfully.",
                log_excerpt=result.output_excerpt,
                working_directory=result.working_directory,
                exit_code=result.returncode,
                duration_seconds=result.duration_seconds,
                started_at=started_at,
                finished_at=finished_at,
            )

        return VerificationOutcome(
            status=ImportVerificationStatus.FAILED,
            adapter_status=WorkstationAdapterStatus.READY,
            adapter_key=self.adapter_key,
            detail=(f"The configured verification command exited with code {result.returncode}."),
            diagnostics=(
                VerificationDiagnostic(
                    severity="error",
                    message=result.stderr.strip() or result.stdout.strip() or "no output",
                    source=Path(result.executable).name,
                ),
            ),
            log_excerpt=result.output_excerpt,
            working_directory=result.working_directory,
            exit_code=result.returncode,
            duration_seconds=result.duration_seconds,
            started_at=started_at,
            finished_at=finished_at,
        )


# ---------------------------------------------------------------------------
# Selection and capability reporting
# ---------------------------------------------------------------------------
def get_adapter(
    settings: Settings,
    *,
    command: VerificationCommand | None = None,
    profiles: Sequence[TargetProfileSupport] = (),
) -> RockwellWorkstationAdapter:
    """The adapter for these settings — disabled unless configured otherwise.

    Never raises. An enabled-but-broken configuration degrades to a
    :class:`DisabledWorkstationAdapter` carrying the reason, exactly as an
    unloadable plugin degrades to a reported absence: an administrator who
    mistyped a path should get a capability report that says so, not an
    application that will not start.
    """
    if not settings.workstation_adapter_enabled:
        return DisabledWorkstationAdapter(adapter_key=settings.workstation_adapter_key)

    try:
        return LocalWorkstationAdapter(settings, command=command, profiles=profiles)
    except AppError as exc:
        logger.warning(
            "workstation adapter is enabled but not usable",
            extra={"error": exc.message, "code": exc.code},
        )
        return DisabledWorkstationAdapter(
            adapter_key=settings.workstation_adapter_key,
            status=WorkstationAdapterStatus.NOT_CONFIGURED,
            reason=(
                f"The workstation adapter is enabled but cannot be used: {exc.message} "
                f"Import verification is unavailable until it is fixed."
            ),
        )


def capability_row(adapter: RockwellWorkstationAdapter) -> dict[str, Any]:
    """One row in the shape ``PluginRegistry.capability_rows()`` produces.

    Same keys, same meanings — ``name``, ``available``, ``provider``,
    ``version``, ``detail`` — so ``GET /api/system/capabilities`` can append it
    without a second response shape. ``detail`` is populated when the capability
    is *unavailable*, which is the normal case and the one an operator needs
    explained.
    """
    health = adapter.health()
    inventory = adapter.installed_products()
    return {
        "name": CAPABILITY_NAME,
        "available": health.available,
        "provider": ", ".join(product.name for product in inventory.products),
        "version": "",
        "detail": "" if health.available else health.detail,
    }


def capability_rows(settings: Settings) -> list[dict[str, Any]]:
    """Capability rows for the configured adapter. Convenience for the route."""
    return [capability_row(get_adapter(settings))]
