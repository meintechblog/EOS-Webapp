from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class LiveValueResponse(BaseModel):
    mapping_id: int
    eos_field: str
    channel_id: int | None
    channel_code: str | None
    channel_type: str | None
    input_key: str | None
    mqtt_topic: str | None
    unit: str | None
    parsed_value: str | None
    ts: datetime | None
    last_seen_seconds: int | None
    status: Literal["healthy", "stale", "never"]
