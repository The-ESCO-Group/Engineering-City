"""The four review agents this backend owns, and their task handlers.

Importing this package registers the handlers for the agent task kinds, the same
way ``app.orchestration.steps`` registers the deterministic ones.
"""

from __future__ import annotations

from app.agents.review.handlers import (
    plan_project,
    request_approvals,
    review_interlocks,
    review_plc,
    senior_review,
)
from app.agents.review.interlock_review import InterlockReviewAgent
from app.agents.review.plc_review import PLCReviewAgent
from app.agents.review.project_manager import ProjectManagerAgent
from app.agents.review.senior_review import SeniorReviewAgent

__all__ = [
    "InterlockReviewAgent",
    "PLCReviewAgent",
    "ProjectManagerAgent",
    "SeniorReviewAgent",
    "plan_project",
    "request_approvals",
    "review_interlocks",
    "review_plc",
    "senior_review",
]
