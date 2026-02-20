from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DynamicApplyStatus = Literal[
    "accepted",
    "rejected",
    "applied",
    "apply_failed",
    "ignored_unbound",
    "blocked_no_active_profile",
]

DynamicValueType = Literal["number", "string", "string_list", "enum"]


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


class DynamicCatalogItem(BaseModel):
    parameter_key: str
    label: str
    hint: str
    value_type: DynamicValueType
    expected_unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    options: list[str] = Field(default_factory=list)
    requires_selector: bool = False
    selector_hint: str | None = None
    examples: list[str] = Field(default_factory=list)


class DynamicCatalogResponse(BaseModel):
    generated_at: datetime
    items: list[DynamicCatalogItem] = Field(default_factory=list)


class ParameterBindingCreateRequest(BaseModel):
    parameter_key: str = Field(min_length=1, max_length=160)
    selector_value: str | None = Field(default=None, max_length=128)
    channel_id: int = Field(ge=1)
    input_key: str = Field(min_length=1, max_length=255)
    payload_path: str | None = Field(default=None, max_length=255)
    timestamp_path: str | None = Field(default=None, max_length=255)
    incoming_unit: str | None = Field(default=None, max_length=32)
    value_multiplier: float = Field(default=1.0, gt=0.0)
    enabled: bool = True

    @field_validator(
        "parameter_key",
        "selector_value",
        "input_key",
        "payload_path",
        "timestamp_path",
        "incoming_unit",
        mode="before",
    )
    @classmethod
    def _trim_values(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class ParameterBindingUpdateRequest(BaseModel):
    parameter_key: str | None = Field(default=None, min_length=1, max_length=160)
    selector_value: str | None = Field(default=None, max_length=128)
    channel_id: int | None = Field(default=None, ge=1)
    input_key: str | None = Field(default=None, min_length=1, max_length=255)
    payload_path: str | None = Field(default=None, max_length=255)
    timestamp_path: str | None = Field(default=None, max_length=255)
    incoming_unit: str | None = Field(default=None, max_length=32)
    value_multiplier: float | None = Field(default=None, gt=0.0)
    enabled: bool | None = None

    @field_validator(
        "parameter_key",
        "selector_value",
        "input_key",
        "payload_path",
        "timestamp_path",
        "incoming_unit",
        mode="before",
    )
    @classmethod
    def _trim_values(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def _validate_non_empty_update(self) -> "ParameterBindingUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class ParameterBindingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    parameter_key: str
    selector_value: str | None
    channel_id: int
    channel_code: str
    channel_type: Literal["mqtt", "http"]
    input_key: str
    payload_path: str | None
    timestamp_path: str | None
    incoming_unit: str | None
    value_multiplier: float
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ParameterBindingEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    binding_id: int | None
    channel_id: int
    channel_code: str
    channel_type: Literal["mqtt", "http"]
    input_key: str
    normalized_key: str
    raw_payload: str
    parsed_value_text: str | None
    event_ts: datetime
    revision_id: int | None
    apply_status: DynamicApplyStatus
    error_text: str | None
    meta_json: dict[str, Any]
    created_at: datetime


class HttpParamPushRequest(BaseModel):
    channel_code: str | None = None
    input_key: str = Field(min_length=1, max_length=255)
    value: Any | None = None
    payload: Any | None = None
    ts: str | int | float | None = None
    timestamp: str | int | float | None = None

    @field_validator("channel_code", "input_key", mode="before")
    @classmethod
    def _trim_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None


class HttpParamIngestResponse(BaseModel):
    accepted: bool
    channel_code: str
    channel_type: Literal["mqtt", "http"]
    input_key: str
    normalized_key: str
    binding_matched: bool
    binding_id: int | None
    event_id: int | None
    event_ts: datetime
    apply_status: DynamicApplyStatus
    error_text: str | None = None
