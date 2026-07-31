"""The registry and ``run_validation``.

The test that matters most here is the one where a validator raises. A run that
dies halfway and reports the three things it found before it fell over is
indistinguishable, on screen, from a run that found three things — and the second
is believed. So the failure has to become a result, the run has to finish, and the
result has to block.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import validation
from app.core.enums import (
    ControllerFamily,
    EventType,
    ValidationDiscipline,
    ValidationRunStatus,
    ValidationSeverity,
)
from app.models.event import Event
from app.models.project import Project
from app.models.release import ValidationResult
from app.validation import VALIDATOR_FAILED_CODE, VALIDATORS, run_validation, validators_for
from app.validation.base import MAX_OBJECT_LABEL_CHARS, Issue, ValidationContext
from tests.validation import (
    make_interlock,
    make_ladder,
    make_target_platform,
    write_tag,
)

_NAMESPACES = {"requirements", "tags", "io", "logic", "cross"}


class BrokenValidator:
    """A check that raises, as a defect in this software would."""

    key = "logic.deliberately_broken"
    discipline = ValidationDiscipline.PLC_LOGIC
    description = "Raises, so the run's own failure handling can be exercised."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        raise RuntimeError("operand table was not loaded")


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
def test_every_validator_key_is_unique() -> None:
    keys = [validator.key for validator in VALIDATORS]
    assert len(keys) == len(set(keys))


def test_every_validator_key_is_namespaced_by_discipline() -> None:
    for validator in VALIDATORS:
        namespace = validator.key.split(".", 1)[0]
        assert namespace in _NAMESPACES, f"{validator.key} is outside the published namespaces"


def test_every_validator_describes_itself() -> None:
    for validator in VALIDATORS:
        assert validator.description.strip(), f"{validator.key} has no description"


def test_validators_can_be_selected_by_discipline() -> None:
    selected = validators_for([ValidationDiscipline.TAGS])

    assert selected
    assert {validator.discipline for validator in selected} == {ValidationDiscipline.TAGS}
    assert len(selected) < len(VALIDATORS)


# ---------------------------------------------------------------------------
# A run over a project with nothing in it
# ---------------------------------------------------------------------------
def test_a_run_records_what_it_covered(db: Session, project: Project) -> None:
    run = run_validation(db, project=project)

    assert run.status is ValidationRunStatus.COMPLETED
    assert run.checks_run == len(VALIDATORS)
    assert run.revision_number == project.current_revision_number
    assert run.finished_at is not None
    assert set(run.disciplines) == {discipline.value for discipline in validation.ALL_DISCIPLINES}


def test_an_empty_project_does_not_block_a_release(db: Session, project: Project) -> None:
    run = run_validation(db, project=project)

    assert run.error_count == 0
    assert run.is_blocking is False


def test_a_clean_run_still_says_what_it_could_not_check(db: Session, project: Project) -> None:
    """A run that found nothing must not read as a run that proved everything."""
    run = run_validation(db, project=project)

    assert run.unsupported_count > 0, "nothing stated the limits of the check"
    assert str(run.checks_run) in run.summary
    assert len(run.summary) <= 500


def test_a_run_can_be_narrowed_to_one_discipline(db: Session, project: Project) -> None:
    run = run_validation(db, project=project, disciplines=[ValidationDiscipline.TAGS])

    assert run.disciplines == [ValidationDiscipline.TAGS.value]
    assert run.checks_run == len(validators_for([ValidationDiscipline.TAGS]))
    assert run.checks_run < len(VALIDATORS)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def _events(db: Session, project: Project, event_type: EventType) -> list[Event]:
    return list(
        db.execute(
            select(Event)
            .where(Event.project_id == project.id, Event.event_type == event_type)
            .order_by(Event.seq)
        ).scalars()
    )


def test_a_run_emits_a_start_and_a_completion(db: Session, project: Project) -> None:
    run = run_validation(db, project=project)

    started = _events(db, project, EventType.VALIDATION_STARTED)
    completed = _events(db, project, EventType.VALIDATION_COMPLETED)

    assert len(started) == 1
    assert len(completed) == 1
    assert started[0].payload["validation_run_id"] == str(run.id)
    assert started[0].payload["revision_number"] == run.revision_number
    assert completed[0].payload["checks_run"] == run.checks_run
    assert completed[0].payload["is_blocking"] is False
    assert completed[0].payload["summary"] == run.summary


def test_the_completion_event_names_the_first_error(db: Session, project: Project) -> None:
    make_interlock(
        db,
        project,
        "ILK-003",
        source_reference="Interlock_Schedule.xlsx / row 5",
        is_implemented=False,
    )

    run = run_validation(db, project=project)
    payload = _events(db, project, EventType.VALIDATION_COMPLETED)[0].payload

    assert run.is_blocking is True
    assert payload["is_blocking"] is True
    assert payload["first_error_result_id"]
    assert payload["first_error_object_label"]


# ---------------------------------------------------------------------------
# Persisted results
# ---------------------------------------------------------------------------
def _results(db: Session, run_id: object) -> list[ValidationResult]:
    return list(
        db.execute(
            select(ValidationResult).where(ValidationResult.validation_run_id == run_id)
        ).scalars()
    )


def test_results_are_persisted_against_the_run(db: Session, project: Project) -> None:
    make_target_platform(db, project, controller_family=ControllerFamily.CONTROLLOGIX)
    rung = make_ladder(db, project)
    write_tag(db, project, rung, "Fault_Latched", mnemonic="OTL")

    run = run_validation(db, project=project)
    results = _results(db, run.id)

    assert results
    for result in results:
        assert result.project_id == project.id
        assert result.code
        assert len(result.code) <= 60
        assert result.message
        assert len(result.object_label) <= MAX_OBJECT_LABEL_CHARS


def test_counts_roll_up_the_results_that_were_stored(db: Session, project: Project) -> None:
    make_target_platform(db, project, controller_family=ControllerFamily.CONTROLLOGIX)
    rung = make_ladder(db, project)
    write_tag(db, project, rung, "Fault_Latched", mnemonic="OTL")

    run = run_validation(db, project=project)
    results = _results(db, run.id)

    assert run.error_count == sum(
        1 for item in results if item.severity is ValidationSeverity.ERROR
    )
    assert run.warning_count == sum(
        1 for item in results if item.severity is ValidationSeverity.WARNING
    )
    assert run.unsupported_count == sum(
        1 for item in results if item.severity is ValidationSeverity.UNSUPPORTED
    )
    assert run.human_review_count == sum(
        1
        for item in results
        if item.requires_human_decision or item.severity is ValidationSeverity.HUMAN_REVIEW_REQUIRED
    )
    assert run.human_review_count > 0, "an OTL is meant to reach a human"


# ---------------------------------------------------------------------------
# A validator that raises
# ---------------------------------------------------------------------------
def test_a_raising_validator_is_recorded_and_the_run_completes(
    db: Session, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validation, "VALIDATORS", (BrokenValidator(), *VALIDATORS))

    run = run_validation(db, project=project)
    results = _results(db, run.id)
    failures = [item for item in results if item.code == VALIDATOR_FAILED_CODE]

    assert run.status is ValidationRunStatus.COMPLETED
    assert run.finished_at is not None
    assert len(failures) == 1
    assert failures[0].severity is ValidationSeverity.ERROR
    assert "logic.deliberately_broken" in failures[0].message
    assert "RuntimeError" in failures[0].message
    assert failures[0].detail["exception"] == "operand table was not loaded"


def test_a_raising_validator_does_not_stop_the_others(
    db: Session, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validation, "VALIDATORS", (BrokenValidator(), *VALIDATORS))

    run = run_validation(db, project=project)
    results = _results(db, run.id)

    assert run.checks_run == len(VALIDATORS) + 1
    assert {item.code for item in results} - {VALIDATOR_FAILED_CODE}, (
        "the surviving validators produced nothing, so the run was not carried on"
    )


def test_a_failed_validator_blocks_the_release(
    db: Session, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its checks are unexamined, so the run must not read as a clean one."""
    monkeypatch.setattr(validation, "VALIDATORS", (BrokenValidator(),))

    run = run_validation(db, project=project)

    assert run.error_count == 1
    assert run.is_blocking is True
    assert "Release blocked" in run.summary
