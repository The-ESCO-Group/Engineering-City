"""Engineering-intake and clarification API schemas.

These are the explicit contract for the information that decides whether a
project may enter the production workflow.  Keeping it out of ``attributes``
means the workflow never has to infer engineering authority from an opaque UI
blob.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.core.enums import (
    ClarificationStatus,
    ControllerFamily,
    HmiProduct,
    ProfileFieldStatus,
    ProjectType,
    QuestionCategory,
    ReleaseMode,
    RestrictedDomain,
    RestrictedDomainMode,
    SafetyClassification,
    UnitSystem,
)


class TargetPlatformInput(ApiModel):
    controller_family: ControllerFamily
    controller_catalog_number: Annotated[str, Field(min_length=1, max_length=60)]
    firmware_revision: Annotated[str, Field(min_length=1, max_length=20)]
    studio_5000_version: Annotated[str, Field(min_length=1, max_length=20)]
    safety_classification: SafetyClassification = SafetyClassification.UNKNOWN
    hmi_product: HmiProduct = HmiProduct.NONE
    hmi_version: Annotated[str, Field(max_length=20)] = ""
    hmi_terminal_model: Annotated[str, Field(max_length=80)] = ""
    hmi_resolution: Annotated[str, Field(max_length=40)] = ""
    factorytalk_architecture: str = ""
    supported_instruction_set_key: Annotated[str, Field(max_length=80)] = ""
    known_limitations: list[Any] = Field(default_factory=list)


class EngineeringStandardsInput(ApiModel):
    naming_standard_version: Annotated[str, Field(min_length=1, max_length=40)]
    alarm_philosophy_version: Annotated[str, Field(min_length=1, max_length=40)]
    controller_template: Annotated[str, Field(min_length=1, max_length=200)]
    aoi_udt_library: Annotated[str, Field(max_length=200)] = ""
    hmi_library: Annotated[str, Field(max_length=200)] = ""
    test_template_set: Annotated[str, Field(min_length=1, max_length=200)]


class EngineeringProfileInput(ApiModel):
    project_name: Annotated[str, Field(min_length=1, max_length=200)]
    customer_label: Annotated[str, Field(max_length=200)] = ""
    plant_or_site: Annotated[str, Field(min_length=1, max_length=200)]
    project_description: Annotated[str, Field(min_length=1)]
    project_type: ProjectType
    units: UnitSystem = UnitSystem.METRIC
    locale: Annotated[str, Field(min_length=2, max_length=35)] = "en-US"
    network_architecture: str = ""
    release_mode: ReleaseMode = ReleaseMode.IMPORT_PACKAGE_ONLY
    restricted_domains: list[RestrictedDomain] = Field(default_factory=list)
    target: TargetPlatformInput
    standards: EngineeringStandardsInput


class TargetPlatformRead(ApiModel):
    id: uuid.UUID
    controller_family: ControllerFamily
    controller_catalog_number: str
    firmware_revision: str
    studio_5000_version: str
    safety_classification: SafetyClassification
    hmi_product: HmiProduct
    hmi_version: str
    hmi_terminal_model: str
    hmi_resolution_width: int
    hmi_resolution_height: int
    factorytalk_architecture: str
    supported_instruction_set_key: str
    known_limitations: list[Any] = Field(default_factory=list)


class EngineeringProfileRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    project_name: str
    customer_label: str
    plant_or_site: str
    project_description: str
    project_type: ProjectType
    naming_standard_version: str
    alarm_philosophy_version: str
    template_set_id: uuid.UUID | None = None
    engineering_standard_set_id: uuid.UUID | None = None
    units: UnitSystem
    locale: str
    network_architecture: str
    release_mode: ReleaseMode
    restricted_domains: list[Any] = Field(default_factory=list)
    restricted_domain_mode: RestrictedDomainMode
    completeness: dict[str, ProfileFieldStatus]
    missing_required_fields: list[Any] = Field(default_factory=list)
    is_complete_for_generation: bool
    blocks_generation: bool
    target: TargetPlatformRead
    created_at: datetime
    updated_at: datetime


class ProjectQuestionRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    reference: str
    category: QuestionCategory
    question: str
    why_it_matters: str
    status: ClarificationStatus
    is_mandatory: bool
    is_blocking: bool
    blocks_task_kinds: list[Any] = Field(default_factory=list)
    answer: str
    answered_by_id: uuid.UUID | None = None
    answered_at: datetime | None = None
    asked_by_agent: str
    raised_in_task_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class ClarificationRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    requirement_id: uuid.UUID | None = None
    object_type: str | None = None
    object_id: uuid.UUID | None = None
    question: str
    ambiguity: str
    status: ClarificationStatus
    resolution: str
    resolved_by_id: uuid.UUID | None = None
    resolved_at: datetime | None = None
    blocks_generation: bool
    created_at: datetime
    updated_at: datetime


class ClarificationInbox(ApiModel):
    project_questions: list[ProjectQuestionRead] = Field(default_factory=list)
    clarifications: list[ClarificationRead] = Field(default_factory=list)
    blocking_count: int = 0


class ResolveClarificationRequest(ApiModel):
    answer: Annotated[str, Field(min_length=1, max_length=10_000)]
    resolution: Literal["answered", "dismissed"] = "answered"


class ClarificationResolutionRead(ApiModel):
    kind: Literal["project_question", "clarification"]
    id: uuid.UUID
    project_id: uuid.UUID
    status: ClarificationStatus
    decision_id: uuid.UUID
    answered_by_id: uuid.UUID
    answered_at: datetime
    unblocked_task_count: int = 0
