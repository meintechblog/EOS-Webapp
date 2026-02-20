from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class HttpInputPushRequest(BaseModel):
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


class HttpInputIngestResponse(BaseModel):
    accepted: bool
    channel_code: str
    channel_type: str
    input_key: str
    normalized_key: str
    mapping_matched: bool
    mapping_id: int | None
    event_ts: datetime
