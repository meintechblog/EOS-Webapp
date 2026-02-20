from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SignConvention = Literal[
    "canonical",
    "unknown",
    "positive_is_import",
    "positive_is_export",
]


InputChannelType = Literal["mqtt", "http"]


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


class MappingCreate(BaseModel):
    eos_field: str = Field(min_length=1, max_length=128)
    channel_id: int | None = Field(default=None, ge=1)
    input_key: str | None = Field(default=None, max_length=255)
    mqtt_topic: str | None = Field(default=None, max_length=255)
    fixed_value: str | None = None
    payload_path: str | None = Field(default=None, max_length=255)
    timestamp_path: str | None = Field(default=None, max_length=255)
    unit: str | None = Field(default=None, max_length=32)
    value_multiplier: float = Field(default=1.0, gt=0.0)
    sign_convention: SignConvention = "canonical"
    enabled: bool = True

    @field_validator(
        "eos_field",
        "input_key",
        "mqtt_topic",
        "fixed_value",
        "payload_path",
        "timestamp_path",
        "unit",
        mode="before",
    )
    @classmethod
    def _normalize_text_fields(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def _validate_create_payload(self) -> "MappingCreate":
        if self.input_key is not None and self.mqtt_topic is not None and self.input_key != self.mqtt_topic:
            raise ValueError("input_key and mqtt_topic must match when both are provided")

        topic_value = self.input_key if self.input_key is not None else self.mqtt_topic
        has_topic = topic_value is not None
        has_fixed = self.fixed_value is not None

        if has_topic == has_fixed:
            raise ValueError("Provide exactly one source: input_key/mqtt_topic or fixed_value")

        if has_fixed and (self.payload_path is not None or self.timestamp_path is not None):
            raise ValueError("payload_path/timestamp_path are not supported for fixed_value mappings")

        if has_fixed and self.channel_id is not None:
            raise ValueError("fixed_value mappings cannot define channel_id")

        return self


class MappingUpdate(BaseModel):
    eos_field: str | None = Field(default=None, max_length=128)
    channel_id: int | None = Field(default=None, ge=1)
    input_key: str | None = Field(default=None, max_length=255)
    mqtt_topic: str | None = Field(default=None, max_length=255)
    fixed_value: str | None = None
    payload_path: str | None = Field(default=None, max_length=255)
    timestamp_path: str | None = Field(default=None, max_length=255)
    unit: str | None = Field(default=None, max_length=32)
    value_multiplier: float | None = Field(default=None, gt=0.0)
    sign_convention: SignConvention | None = None
    enabled: bool | None = None

    @field_validator(
        "eos_field",
        "input_key",
        "mqtt_topic",
        "fixed_value",
        "payload_path",
        "timestamp_path",
        "unit",
        mode="before",
    )
    @classmethod
    def _normalize_text_fields(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def _validate_update_payload(self) -> "MappingUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided for update")
        if "enabled" in self.model_fields_set and self.enabled is None:
            raise ValueError("enabled must be true or false")

        if self.input_key is not None and self.mqtt_topic is not None and self.input_key != self.mqtt_topic:
            raise ValueError("input_key and mqtt_topic must match when both are provided")

        if self.fixed_value is not None and (
            self.payload_path is not None or self.timestamp_path is not None
        ):
            raise ValueError("payload_path/timestamp_path are not supported for fixed_value mappings")
        return self


class MappingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    eos_field: str
    channel_id: int | None
    channel_code: str | None
    channel_type: InputChannelType | None
    input_key: str | None
    mqtt_topic: str | None
    fixed_value: str | None
    payload_path: str | None
    timestamp_path: str | None
    unit: str | None
    value_multiplier: float
    sign_convention: SignConvention
    enabled: bool
    created_at: datetime
    updated_at: datetime
