from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


FieldGroup = Literal["mandatory", "optional", "live"]
FieldValueType = Literal["number", "string", "select", "string_list"]
FieldSource = Literal["ui", "http", "import", "system"]
FieldApplyStatus = Literal["saved", "rejected"]
SetupEntityType = Literal["pv_plane", "electric_vehicle", "home_appliance", "home_appliance_window"]
SetupEntityAction = Literal["add", "remove"]
SetupCategoryRequirement = Literal["MUSS", "KANN", "MUSS/KANN"]


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
    advanced: bool = False
    item_key: str | None = None
    category_id: str | None = None
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


class SetupCategoryItemResponse(BaseModel):
    item_key: str
    label: str
    entity_type: SetupEntityType | None = None
    parent_item_key: str | None = None
    deletable: bool = False
    base_object: bool = False
    required_count: int = 0
    invalid_required_count: int = 0
    fields: list[SetupFieldResponse] = Field(default_factory=list)


class SetupCategoryResponse(BaseModel):
    category_id: str
    title: str
    description: str | None = None
    requirement_label: SetupCategoryRequirement
    repeatable: bool = False
    add_entity_type: SetupEntityType | None = None
    default_open: bool = False
    required_count: int = 0
    invalid_required_count: int = 0
    item_limit: int | None = None
    items: list[SetupCategoryItemResponse] = Field(default_factory=list)


class SetupLayoutResponse(BaseModel):
    generated_at: datetime
    invalid_required_total: int = 0
    categories: list[SetupCategoryResponse] = Field(default_factory=list)


class SetupEntityMutateRequest(BaseModel):
    action: SetupEntityAction
    entity_type: SetupEntityType
    item_key: str | None = Field(default=None, min_length=1, max_length=255)
    clone_from_item_key: str | None = Field(default=None, min_length=1, max_length=255)
    parent_item_key: str | None = Field(default=None, min_length=1, max_length=255)
    source: FieldSource = "ui"


class SetupEntityMutateResponse(BaseModel):
    status: FieldApplyStatus
    message: str
    warnings: list[str] = Field(default_factory=list)
    layout: SetupLayoutResponse
