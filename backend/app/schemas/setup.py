from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


SetupItemStatus = Literal["ok", "warning", "blocked"]
SetupReadinessLevel = Literal["ready", "degraded", "blocked"]


class SetupChecklistItem(BaseModel):
    key: str
    required: bool
    status: SetupItemStatus
    message: str
    action_hint: str | None = None


class SetupChecklistResponse(BaseModel):
    last_check_ts: datetime
    readiness_level: SetupReadinessLevel
    blockers_count: int
    warnings_count: int
    items: list[SetupChecklistItem] = Field(default_factory=list)
