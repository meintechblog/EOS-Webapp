from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


FieldGroup = Literal["mandatory", "optional", "live"]
FieldValueType = Literal["number", "string", "select", "string_list"]
FieldSource = Literal["ui", "http", "import", "system"]
FieldApplyStatus = Literal["saved", "rejected"]


class SetupFieldResponse(BaseModel):
    field_id: str
    group: FieldGroup
    label: str
    required: bool
    value_type: FieldValueType
    unit: str | None = None
    options: list[str] = Field(default_factory=list)
    current_value: Any = None
    valid: bool
    missing: bool
    dirty: bool
    last_source: FieldSource | None = None
    last_update_ts: datetime | None = None
    http_path_template: str
    http_override_active: bool
    http_override_last_ts: datetime | None = None
    error: str | None = None


class SetupFieldUpdate(BaseModel):
    field_id: str = Field(min_length=1, max_length=191)
    value: Any
    source: FieldSource = "ui"
    ts: str | int | float | None = None
    timestamp: str | int | float | None = None


class SetupFieldPatchRequest(BaseModel):
    updates: list[SetupFieldUpdate] = Field(default_factory=list)


class SetupFieldUpdateResult(BaseModel):
    field_id: str
    status: FieldApplyStatus
    error: str | None = None
    field: SetupFieldResponse


class SetupFieldPatchResponse(BaseModel):
    results: list[SetupFieldUpdateResult] = Field(default_factory=list)


class SetupReadinessItem(BaseModel):
    field_id: str
    required: bool
    status: Literal["ok", "warning", "blocked"]
    message: str


class SetupReadinessResponse(BaseModel):
    readiness_level: Literal["ready", "degraded", "blocked"]
    blockers_count: int
    warnings_count: int
    items: list[SetupReadinessItem] = Field(default_factory=list)


class SetupExportResponse(BaseModel):
    format: Literal["eos-webapp.inputs-setup.v2"]
    exported_at: datetime
    payload: dict[str, Any]


class SetupImportRequest(BaseModel):
    package_json: dict[str, Any]


class SetupImportResponse(BaseModel):
    applied: bool
    message: str
    warnings: list[str] = Field(default_factory=list)


class SetupSetRequest(BaseModel):
    path: str = Field(min_length=1, max_length=255)
    value: Any
    source: FieldSource = "http"
    ts: str | int | float | None = None
    timestamp: str | int | float | None = None


class SetupSetResponse(BaseModel):
    accepted: bool
    field_id: str
    status: FieldApplyStatus
    error: str | None = None
    field: SetupFieldResponse

