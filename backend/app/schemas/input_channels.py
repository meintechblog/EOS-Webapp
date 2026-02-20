from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ChannelType = Literal["mqtt", "http"]


class InputChannelCreateRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    channel_type: ChannelType
    enabled: bool = True
    is_default: bool = False
    config_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code", "name", mode="before")
    @classmethod
    def _trim_text(cls, value: str) -> str:
        trimmed = value.strip()
        if trimmed == "":
            raise ValueError("value must not be empty")
        return trimmed


class InputChannelUpdateRequest(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    is_default: bool | None = None
    config_json: dict[str, Any] | None = None

    @field_validator("code", "name", mode="before")
    @classmethod
    def _trim_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None


class InputChannelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    channel_type: ChannelType
    enabled: bool
    is_default: bool
    config_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class InputChannelDeleteBlockedResponse(BaseModel):
    detail: str
    reason: str
    mapping_count: int
    binding_count: int | None = None
