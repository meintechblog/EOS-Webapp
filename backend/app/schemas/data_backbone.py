from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


Resolution = Literal["raw", "5m", "1h", "1d"]


class SignalLatestItemResponse(BaseModel):
    signal_key: str
    label: str
    value_type: str
    canonical_unit: str | None = None
    tags_json: dict[str, Any] = Field(default_factory=dict)
    last_ts: datetime | None = None
    last_value_num: float | None = None
    last_value_text: str | None = None
    last_value_bool: bool | None = None
    last_value_json: dict[str, Any] | list[Any] | None = None
    last_quality_status: str | None = None
    last_source_type: str | None = None
    last_run_id: int | None = None
    updated_at: datetime | None = None


class SignalSeriesPointResponse(BaseModel):
    ts: datetime
    value_num: float | None = None
    value_text: str | None = None
    value_bool: bool | None = None
    value_json: dict[str, Any] | list[Any] | None = None
    quality_status: str | None = None
    source_type: str | None = None
    run_id: int | None = None
    mapping_id: int | None = None
    min_num: float | None = None
    max_num: float | None = None
    avg_num: float | None = None
    sum_num: float | None = None
    count_num: int | None = None
    last_num: float | None = None


class SignalSeriesResponse(BaseModel):
    signal_key: str
    resolution: Resolution
    points: list[SignalSeriesPointResponse] = Field(default_factory=list)


class JobRunSnapshotResponse(BaseModel):
    id: int
    job_name: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    affected_rows: int
    details_json: dict[str, Any] | list[Any] | None = None
    error_text: str | None = None


class DataRetentionStatusResponse(BaseModel):
    last_rollup_run: JobRunSnapshotResponse | None = None
    last_retention_run: JobRunSnapshotResponse | None = None
    raw_rows_24h: int
    rollup_rows_24h: int
    signal_catalog_count: int


class PowerSamplePointResponse(BaseModel):
    key: str
    ts: datetime
    value_w: float
    source: str
    quality: str
    mapping_id: int | None = None
    raw_payload: str | None = None
    ingested_at: datetime | None = None


class PowerSeriesResponse(BaseModel):
    key: str
    points: list[PowerSamplePointResponse] = Field(default_factory=list)


class EmrPointResponse(BaseModel):
    emr_key: str
    ts: datetime
    emr_kwh: float
    last_power_w: float | None = None
    last_ts: datetime | None = None
    method: str
    notes: str | None = None
    created_at: datetime | None = None


class EmrSeriesResponse(BaseModel):
    emr_key: str
    points: list[EmrPointResponse] = Field(default_factory=list)
