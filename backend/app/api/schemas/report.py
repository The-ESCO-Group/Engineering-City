"""Report schemas.

Report *generation* is provided by GPT-owned plugins under ``app.reporting``.
These schemas are the core-owned contract those plugins produce results into.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.core.enums import ReportStatus


class ReportRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    task_id: uuid.UUID | None = None
    report_type: str
    export_format: str
    provider_name: str = ""
    provider_version: str = ""
    title: str
    status: ReportStatus
    media_type: str = ""
    size_bytes: int = 0
    sha256: str = ""
    finding_ids: list[Any] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    error_message: str = ""
    generated_by_id: uuid.UUID | None = None
    created_at: datetime
    #: Relative download path, or empty when the artifact was never written.
    download_path: str = ""
