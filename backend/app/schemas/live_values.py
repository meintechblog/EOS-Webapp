from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class LiveValueResponse(BaseModel):
    mapping_id: int
    eos_field: str
    mqtt_topic: str
    unit: str | None
    parsed_value: str | None
    ts: datetime | None
    last_seen_seconds: int | None
    status: Literal["healthy", "stale", "never"]

