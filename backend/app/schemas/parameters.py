from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ValidationStatus = Literal["valid", "invalid", "unknown"]
RevisionSource = Literal["manual", "import", "bootstrap", "eos_pull", "dynamic_input"]


class ParameterCatalogField(BaseModel):
    path: str
    label: str
    hint: str
    value_type: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: list[str] = Field(default_factory=list)


class ParameterCatalogSection(BaseModel):
    id: str
    title: str
    description: str
    repeatable: bool = False
    fields: list[ParameterCatalogField] = Field(default_factory=list)


class ParameterCatalogResponse(BaseModel):
    generated_at: datetime
    sections: list[ParameterCatalogSection] = Field(default_factory=list)
    provider_options: dict[str, list[str]] = Field(default_factory=dict)
    bidding_zone_options: list[str] = Field(default_factory=list)


class ParameterProfileRevisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    revision_no: int
    source: RevisionSource
    validation_status: ValidationStatus
    validation_issues_json: dict[str, Any] | list[Any] | None = None
    is_current_draft: bool
    is_last_applied: bool
    created_at: datetime
    applied_at: datetime | None = None


class ParameterProfileRevisionDetailResponse(ParameterProfileRevisionResponse):
    payload_json: dict[str, Any]


class ParameterProfileSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    current_draft_revision_no: int | None = None
    last_applied_revision_no: int | None = None
    last_applied_at: datetime | None = None
    current_draft_validation_status: ValidationStatus | None = None


class ParameterProfileDetailResponse(BaseModel):
    profile: ParameterProfileSummaryResponse
    current_draft: ParameterProfileRevisionDetailResponse | None = None
    last_applied: ParameterProfileRevisionDetailResponse | None = None
    revisions: list[ParameterProfileRevisionResponse] = Field(default_factory=list)


class ParameterProfileCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    clone_from_profile_id: int | None = None


class ParameterProfileUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    is_active: bool | None = None


class ParameterDraftUpdateRequest(BaseModel):
    payload_json: dict[str, Any]


class ParameterValidationResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    normalized_payload: dict[str, Any] | None = None


class ParameterApplyRequest(BaseModel):
    set_active_profile: bool = True


class ParameterExportResponse(BaseModel):
    format: str
    exported_at: datetime
    profile: dict[str, Any]
    masked_secrets: bool
    payload: dict[str, Any]


class ParameterImportRequest(BaseModel):
    package_json: dict[str, Any]


class ParameterImportDiffItem(BaseModel):
    path: str
    before: Any = None
    after: Any = None
    change_type: Literal["added", "removed", "changed"]


class ParameterImportPreviewResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    diff: list[ParameterImportDiffItem] = Field(default_factory=list)
    normalized_payload: dict[str, Any] | None = None


class ParameterStatusSnapshot(BaseModel):
    active_profile_id: int | None = None
    active_profile_name: str | None = None
    current_draft_revision: int | None = None
    last_applied_revision: int | None = None
    last_apply_ts: datetime | None = None
    last_apply_error: str | None = None
