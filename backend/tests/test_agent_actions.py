"""Structural validation of model-proposed actions.

These rules are the reason a model cannot smuggle an unsourced engineering claim
into the record. They are enforced by the schema, not by prompt wording.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.agents.base.actions import (
    ACTION_PAYLOAD_MODELS,
    AgentAction,
    CreateFindingPayload,
    UpdateFindingReviewPayload,
    payload_model_for,
)
from app.core.enums import ActionType, FindingCategory, FindingSeverity


def test_every_action_type_has_a_payload_model() -> None:
    missing = [action.value for action in ActionType if action not in ACTION_PAYLOAD_MODELS]
    assert missing == []
    for action in ActionType:
        assert payload_model_for(action) is ACTION_PAYLOAD_MODELS[action]


def test_finding_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="requires at least one evidence_anchor_id"):
        AgentAction(
            action_type=ActionType.CREATE_FINDING,
            summary="Interlock missing from logic",
            evidence_anchor_ids=[],
        )


def test_recommendation_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="requires at least one evidence_anchor_id"):
        AgentAction(
            action_type=ActionType.CREATE_RECOMMENDATION,
            summary="Add a permissive",
            evidence_anchor_ids=[],
        )


def test_finding_with_evidence_is_accepted() -> None:
    action = AgentAction(
        action_type=ActionType.CREATE_FINDING,
        summary="Interlock missing from logic",
        evidence_anchor_ids=[uuid.uuid4()],
        confidence=0.6,
    )
    assert action.confidence == 0.6
    assert action.requires_approval is False


def test_non_technical_actions_do_not_require_evidence() -> None:
    action = AgentAction(
        action_type=ActionType.REPORT_BLOCKER,
        summary="No interlock schedule was uploaded",
    )
    assert action.evidence_anchor_ids == []


def test_unknown_action_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentAction(action_type="delete_everything", summary="nope")  # type: ignore[arg-type]


def test_extra_fields_are_rejected() -> None:
    """A model cannot widen the action surface by inventing fields."""
    with pytest.raises(ValidationError):
        AgentAction(
            action_type=ActionType.POST_MESSAGE,
            summary="hello",
            sql="DROP TABLE findings",  # type: ignore[call-arg]
        )


def test_confidence_is_bounded() -> None:
    for bad in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            AgentAction(
                action_type=ActionType.POST_MESSAGE,
                summary="hello",
                confidence=bad,
            )


def test_safety_category_requires_a_suggested_review_action() -> None:
    with pytest.raises(ValidationError, match="suggested_review_action"):
        CreateFindingPayload(
            title="Permissive absent",
            category=FindingCategory.MISSING_LOGIC,
            severity=FindingSeverity.HIGH,
            fact="Rung 12 energises the output with no permissive contact.",
        )


def test_safety_category_accepted_with_a_review_action() -> None:
    payload = CreateFindingPayload(
        title="Permissive absent",
        category=FindingCategory.MISSING_LOGIC,
        severity=FindingSeverity.HIGH,
        fact="Rung 12 energises the output with no permissive contact.",
        suggested_review_action="Confirm with the control narrative whether a permissive applies.",
    )
    assert payload.inference == ""


def test_duplicate_verdict_requires_a_target() -> None:
    with pytest.raises(ValidationError, match="duplicate_of_finding_id"):
        UpdateFindingReviewPayload(finding_id=uuid.uuid4(), verdict="duplicate")


def test_review_payload_accepts_a_confidence_reduction() -> None:
    payload = UpdateFindingReviewPayload(
        finding_id=uuid.uuid4(),
        verdict="needs_more_evidence",
        adjusted_confidence=0.2,
        parser_limitations=["PDF text extraction yielded no content on page 4"],
    )
    assert payload.adjusted_confidence == 0.2
