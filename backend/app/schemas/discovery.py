from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.mappings import SignConvention


MappedStatus = Literal["mapped_correct", "mapped_other", "unmapped"]
MappedKind = Literal["signal_mapping", "parameter_binding", "unmapped"]
DiscoveryNamespace = Literal["input", "param"]


class DiscoveredInputItem(BaseModel):
    namespace: DiscoveryNamespace
    channel_id: int
    channel_code: str
    channel_type: str
    input_key: str
    normalized_key: str
    last_seen: datetime
    last_payload: str | None
    message_count: int
    last_meta_json: dict[str, object] = Field(default_factory=dict)
    suggested_eos_field: str | None = None
    suggested_multiplier: float | None = None
    confidence: float | None = None
    notes: list[str] = Field(default_factory=list)
    mapped_status: MappedStatus
    mapped_kind: MappedKind


class DiscoveredTopicItem(BaseModel):
    mqtt_topic: str
    last_seen: datetime
    last_payload: str | None
    message_count: int
    last_retain: bool
    last_qos: int
    normalized_topic: str
    suggested_eos_field: str | None = None
    suggested_multiplier: float | None = None
    confidence: float | None = None
    notes: list[str] = Field(default_factory=list)


class AutomapAppliedItem(BaseModel):
    mapping_id: int
    eos_field: str
    mqtt_topic: str
    channel_id: int | None = None
    channel_code: str | None = None
    channel_type: str | None = None
    value_multiplier: float
    sign_convention: SignConvention
    notes: list[str] = Field(default_factory=list)


class AutomapSkippedItem(BaseModel):
    mqtt_topic: str
    normalized_topic: str
    channel_id: int | None = None
    channel_code: str | None = None
    channel_type: str | None = None
    reason: str
    notes: list[str] = Field(default_factory=list)


class TopicNormalization(BaseModel):
    from_topic: str
    to_topic: str
    channel_id: int | None = None
    channel_code: str | None = None
    channel_type: str | None = None


class AutomapResult(BaseModel):
    created: list[AutomapAppliedItem] = Field(default_factory=list)
    updated: list[AutomapAppliedItem] = Field(default_factory=list)
    unchanged: list[AutomapAppliedItem] = Field(default_factory=list)
    skipped: list[AutomapSkippedItem] = Field(default_factory=list)
    normalizations: list[TopicNormalization] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
