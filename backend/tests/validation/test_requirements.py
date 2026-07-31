"""The requirements validators.

Every check has a case that makes it fire and a case that must leave it silent.
The negative cases are the ones that matter most here: a traceability sweep that
reported every parsed tag as baseless would produce hundreds of results on a
correctly imported project, and an engineer who has been shown that once stops
reading the category at all.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import (
    ClarificationStatus,
    EvidenceKind,
    LogicOrigin,
    ObjectType,
    RequirementPriority,
    RequirementStatus,
    ValidationSeverity,
)
from app.models.project import Project
from app.validation.requirements import (
    ClarificationValidator,
    CoverageValidator,
    TraceabilityValidator,
)
from tests.validation import (
    codes,
    make_clarification,
    make_evidence_link,
    make_question,
    make_requirement,
    make_tag,
    only,
    run_check,
)

# ---------------------------------------------------------------------------
# requirements.unresolved_clarification
# ---------------------------------------------------------------------------
CLARIFICATION = "requirements.unresolved_clarification"


def test_blocking_clarification_is_an_error(db: Session, project: Project) -> None:
    requirement = make_requirement(db, project, "REQ-014")
    make_clarification(
        db,
        project,
        requirement_id=requirement.id,
        status=ClarificationStatus.OPEN,
        blocks_generation=True,
    )

    issue = only(run_check(ClarificationValidator(), db, project), CLARIFICATION)

    assert issue.severity is ValidationSeverity.ERROR
    assert "REQ-014" in issue.object_label
    assert issue.suggested_action


def test_deferred_blocking_clarification_still_blocks(db: Session, project: Project) -> None:
    """Deferring records that a human looked; it does not supply the answer."""
    make_clarification(db, project, status=ClarificationStatus.DEFERRED, blocks_generation=True)

    issue = only(run_check(ClarificationValidator(), db, project), CLARIFICATION)

    assert issue.severity is ValidationSeverity.ERROR


def test_non_blocking_clarification_is_a_warning(db: Session, project: Project) -> None:
    make_clarification(db, project, status=ClarificationStatus.OPEN, blocks_generation=False)

    issue = only(run_check(ClarificationValidator(), db, project), CLARIFICATION)

    assert issue.severity is ValidationSeverity.WARNING


def test_answered_clarification_does_not_fire(db: Session, project: Project) -> None:
    make_clarification(
        db,
        project,
        status=ClarificationStatus.ANSWERED,
        blocks_generation=True,
        resolution="A controlled ramp-down.",
    )

    assert CLARIFICATION not in codes(run_check(ClarificationValidator(), db, project))


# ---------------------------------------------------------------------------
# requirements.unanswered_question
# ---------------------------------------------------------------------------
QUESTION = "requirements.unanswered_question"


def test_mandatory_open_question_is_an_error(db: Session, project: Project) -> None:
    make_question(db, project, "Q-003", is_mandatory=True, status=ClarificationStatus.OPEN)

    issue = only(run_check(ClarificationValidator(), db, project), QUESTION)

    assert issue.severity is ValidationSeverity.ERROR
    assert "Q-003" in issue.object_label


def test_optional_open_question_is_a_warning(db: Session, project: Project) -> None:
    make_question(db, project, "Q-004", is_mandatory=False, status=ClarificationStatus.OPEN)

    issue = only(run_check(ClarificationValidator(), db, project), QUESTION)

    assert issue.severity is ValidationSeverity.WARNING


def test_answered_question_does_not_fire(db: Session, project: Project) -> None:
    make_question(
        db,
        project,
        "Q-005",
        is_mandatory=True,
        status=ClarificationStatus.ANSWERED,
        answer="ControlLogix 1756-L83E.",
    )

    assert QUESTION not in codes(run_check(ClarificationValidator(), db, project))


# ---------------------------------------------------------------------------
# requirements.object_without_basis
# ---------------------------------------------------------------------------
BASIS = "requirements.object_without_basis"


def test_generated_tag_with_no_basis_is_an_error(db: Session, project: Project) -> None:
    make_tag(db, project, "Motor_Run_Cmd", origin=LogicOrigin.AGENT_PROPOSED)

    issue = only(run_check(TraceabilityValidator(), db, project), BASIS)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.object_type is ObjectType.TAG
    assert "Motor_Run_Cmd" in issue.object_label


def test_generated_tag_with_a_template_basis_does_not_fire(db: Session, project: Project) -> None:
    tag = make_tag(db, project, "Motor_Run_Cmd", origin=LogicOrigin.TEMPLATE_EXPANSION)
    make_evidence_link(
        db,
        project,
        object_type=ObjectType.TAG,
        object_id=tag.id,
        basis_kind=EvidenceKind.TEMPLATE_RULE,
    )

    assert BASIS not in codes(run_check(TraceabilityValidator(), db, project))


def test_parsed_tag_with_no_basis_does_not_fire(db: Session, project: Project) -> None:
    """A tag read out of the customer's export is not something we generated."""
    make_tag(db, project, "Motor_Run_Cmd", origin=LogicOrigin.PARSED_FROM_SOURCE)

    assert BASIS not in codes(run_check(TraceabilityValidator(), db, project))


def test_a_parser_warning_is_not_an_acceptable_basis(db: Session, project: Project) -> None:
    """``PARSER_WARNING`` is evidence about a document, not a reason to exist."""
    tag = make_tag(db, project, "Motor_Run_Cmd", origin=LogicOrigin.AGENT_PROPOSED)
    make_evidence_link(
        db,
        project,
        object_type=ObjectType.TAG,
        object_id=tag.id,
        basis_kind=EvidenceKind.PARSER_WARNING,
    )

    assert BASIS in codes(run_check(TraceabilityValidator(), db, project))


# ---------------------------------------------------------------------------
# requirements.no_implementation_coverage
# ---------------------------------------------------------------------------
COVERAGE = "requirements.no_implementation_coverage"


def test_uncited_mandatory_requirement_is_an_error(db: Session, project: Project) -> None:
    make_requirement(
        db,
        project,
        "REQ-001",
        status=RequirementStatus.ACCEPTED,
        priority=RequirementPriority.MANDATORY,
    )

    issue = only(run_check(CoverageValidator(), db, project), COVERAGE)

    assert issue.severity is ValidationSeverity.ERROR
    assert "REQ-001" in issue.object_label


def test_uncited_expected_requirement_is_a_warning(db: Session, project: Project) -> None:
    make_requirement(
        db,
        project,
        "REQ-002",
        status=RequirementStatus.ACCEPTED,
        priority=RequirementPriority.EXPECTED,
    )

    issue = only(run_check(CoverageValidator(), db, project), COVERAGE)

    assert issue.severity is ValidationSeverity.WARNING


def test_cited_requirement_does_not_fire(db: Session, project: Project) -> None:
    requirement = make_requirement(
        db,
        project,
        "REQ-003",
        status=RequirementStatus.ACCEPTED,
        priority=RequirementPriority.MANDATORY,
    )
    tag = make_tag(db, project, "Motor_Run_Cmd")
    make_evidence_link(
        db,
        project,
        object_type=ObjectType.TAG,
        object_id=tag.id,
        basis_kind=EvidenceKind.DIRECT,
        requirement_id=requirement.id,
    )

    assert COVERAGE not in codes(run_check(CoverageValidator(), db, project))


def test_proposed_requirement_does_not_fire(db: Session, project: Project) -> None:
    """Nothing is supposed to be built for a requirement nobody has accepted."""
    make_requirement(
        db,
        project,
        "REQ-004",
        status=RequirementStatus.PROPOSED,
        priority=RequirementPriority.MANDATORY,
    )

    assert COVERAGE not in codes(run_check(CoverageValidator(), db, project))


# ---------------------------------------------------------------------------
# requirements.coverage_is_citation_only
# ---------------------------------------------------------------------------
UNSUPPORTED = "requirements.coverage_is_citation_only"


def test_coverage_states_what_it_did_not_check(db: Session, project: Project) -> None:
    make_requirement(db, project, "REQ-001", status=RequirementStatus.ACCEPTED)

    issue = only(run_check(CoverageValidator(), db, project), UNSUPPORTED)

    assert issue.severity is ValidationSeverity.UNSUPPORTED
    assert "judgement" in issue.message


def test_no_requirements_means_no_coverage_claim(db: Session, project: Project) -> None:
    assert UNSUPPORTED not in codes(run_check(CoverageValidator(), db, project))
