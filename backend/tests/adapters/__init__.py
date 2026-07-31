"""Fixtures for the optional Rockwell workstation adapter.

There is no Rockwell tooling on any machine that runs this suite, and there must
not be: these tests exercise the *interface and the refusals*, not the vendor
software. The stand-in program is the Python interpreter running ``-c``, placed
on a temporary allowlist — harmless, present everywhere the suite runs, and able
to exit with whatever code a test needs.

The ORM rows are built in memory rather than persisted. The adapter never
touches the database by design, so a session would only add a dependency on
another agent's fixtures without testing anything the adapter does.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from app.core.config import Settings
from app.core.enums import ImportVerificationStatus, ReleaseCandidateStatus
from app.models.release import ReleaseCandidate, WorkstationVerificationJob

#: The harmless stand-in for licensed tooling.
STAND_IN_EXECUTABLE = Path(sys.executable).resolve()


def make_settings(
    tmp_path: Path,
    *,
    enabled: bool = True,
    allowlist: list[str] | None = None,
    working_dir: Path | None = None,
    timeout_seconds: int = 900,
    adapter_key: str = "bench-01",
) -> Settings:
    """Settings with the adapter configured the way an administrator would."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        environment="test",
        data_dir=data_dir,
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'test.sqlite3').as_posix()}",
        workstation_adapter_enabled=enabled,
        workstation_adapter_key=adapter_key,
        workstation_executable_allowlist=(
            [str(STAND_IN_EXECUTABLE)] if allowlist is None and enabled else (allowlist or [])
        ),
        workstation_working_dir=working_dir if working_dir is not None else tmp_path / "scratch",
        workstation_timeout_seconds=timeout_seconds,
    )


def make_candidate(
    *,
    status: ReleaseCandidateStatus = ReleaseCandidateStatus.GENERATED,
    package_path: str = "",
) -> ReleaseCandidate:
    """A transient release candidate. ``id`` is set because nothing will flush."""
    return ReleaseCandidate(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        candidate_number=1,
        status=status,
        package_path=package_path,
    )


def make_job(
    candidate: ReleaseCandidate,
    *,
    approved: bool = True,
    working_directory: str = "",
    timeout_seconds: int | None = None,
) -> WorkstationVerificationJob:
    """A transient verification job, approved by a named human unless told not to."""
    return WorkstationVerificationJob(
        id=uuid.uuid4(),
        project_id=candidate.project_id,
        release_candidate=candidate,
        status=ImportVerificationStatus.QUEUED,
        requested_by_id=uuid.uuid4(),
        approved_by_id=uuid.uuid4() if approved else None,
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
    )


def make_package(tmp_path: Path, name: str = "RC1.zip") -> Path:
    """A stand-in for an assembled release package."""
    package = tmp_path / "packages" / name
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_bytes(b"not a real package")
    return package
