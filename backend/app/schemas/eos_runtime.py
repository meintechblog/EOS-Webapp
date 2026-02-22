from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


RunStatus = str
AutoRunPreset = Literal["off", "15m", "30m", "60m"]


class CollectorStatusResponse(BaseModel):
    running: bool
    poll_seconds: int
    last_poll_ts: datetime | None = None
    last_successful_sync_ts: datetime | None = None
    last_observed_eos_run_datetime: datetime | None = None
    force_run_in_progress: bool
    last_force_request_ts: datetime | None = None
    last_error: str | None = None
    auto_run_preset: AutoRunPreset = "off"
    auto_run_enabled: bool = False
    auto_run_interval_minutes: int | None = None
    aligned_scheduler_enabled: bool = False
    aligned_scheduler_minutes: str = ""
    aligned_scheduler_delay_seconds: int = 0
    aligned_scheduler_next_due_ts: datetime | None = None
    aligned_scheduler_last_trigger_ts: datetime | None = None
    aligned_scheduler_last_skip_reason: str | None = None
    price_backfill_last_check_ts: datetime | None = None
    price_backfill_last_attempt_ts: datetime | None = None
    price_backfill_last_success_ts: datetime | None = None
    price_backfill_last_status: str | None = None
    price_backfill_last_history_hours: float | None = None
    price_backfill_cooldown_until_ts: datetime | None = None


class EosRuntimeResponse(BaseModel):
    eos_base_url: str
    health_ok: bool
    health_payload: dict[str, Any] | None = None
    config_payload: dict[str, Any] | None = None
    collector: CollectorStatusResponse


class EosRuntimeConfigUpdateRequest(BaseModel):
    ems_mode: str = Field(min_length=1, max_length=64)
    ems_interval_seconds: int = Field(ge=1, le=86400)


class EosRuntimeConfigUpdateResponse(BaseModel):
    ems_mode: str
    ems_interval_seconds: int
    applied_mode_path: str
    applied_interval_path: str
    runtime: EosRuntimeResponse


class EosAutoRunUpdateRequest(BaseModel):
    preset: AutoRunPreset


class EosAutoRunUpdateResponse(BaseModel):
    preset: AutoRunPreset
    applied_slots: list[int] = Field(default_factory=list)
    runtime: EosRuntimeResponse


class EosRunSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trigger_source: str
    run_mode: str
    eos_last_run_datetime: datetime | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    error_text: str | None = None
    created_at: datetime


class EosRunDetailResponse(EosRunSummaryResponse):
    artifact_summary: dict[str, int] = Field(default_factory=dict)


class EosRunPlanInstructionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    instruction_index: int
    instruction_type: str
    resource_id: str | None = None
    actuator_id: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    execution_time: datetime | None = None
    operation_mode_id: str | None = None
    operation_mode_factor: float | None = None
    payload_json: dict[str, Any] | list[Any]


class EosRunPlanResponse(BaseModel):
    run_id: int
    payload_json: dict[str, Any] | list[Any] | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    instructions: list[EosRunPlanInstructionResponse] = Field(default_factory=list)


class EosRunSolutionResponse(BaseModel):
    run_id: int
    payload_json: dict[str, Any] | list[Any] | None = None


class EosRunPredictionSeriesPointResponse(BaseModel):
    date_time: datetime
    elec_price_ct_per_kwh: float | None = None
    pv_ac_kw: float | None = None
    pv_dc_kw: float | None = None
    load_kw: float | None = None


class EosRunPredictionSeriesResponse(BaseModel):
    run_id: int
    source: str
    points: list[EosRunPredictionSeriesPointResponse] = Field(default_factory=list)


class EosOutputCurrentItemResponse(BaseModel):
    run_id: int
    resource_id: str
    actuator_id: str | None = None
    operation_mode_id: str | None = None
    operation_mode_factor: float | None = None
    requested_power_kw: float | None = None
    effective_at: datetime | None = None
    source_instruction: dict[str, Any] = Field(default_factory=dict)
    safety_status: str


class EosOutputTimelineItemResponse(BaseModel):
    run_id: int
    instruction_id: int
    instruction_index: int
    resource_id: str
    actuator_id: str | None = None
    instruction_type: str
    operation_mode_id: str | None = None
    operation_mode_factor: float | None = None
    requested_power_kw: float | None = None
    execution_time: datetime | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    source_instruction: dict[str, Any] = Field(default_factory=dict)
    deduped: bool = True


class EosOutputSignalItemResponse(BaseModel):
    signal_key: str
    label: str
    resource_id: str | None = None
    requested_power_kw: float | None = None
    unit: Literal["kW"] = "kW"
    operation_mode_id: str | None = None
    operation_mode_factor: float | None = None
    effective_at: datetime | None = None
    run_id: int | None = None
    json_path_value: str
    last_fetch_ts: datetime | None = None
    last_fetch_client: str | None = None
    fetch_count: int = 0
    status: str


class EosOutputSignalsBundleResponse(BaseModel):
    central_http_path: str = "/eos/get/outputs"
    run_id: int | None = None
    fetched_at: datetime
    signals: dict[str, EosOutputSignalItemResponse] = Field(default_factory=dict)


class EosRunPlausibilityFinding(BaseModel):
    level: str
    code: str
    message: str
    details: dict[str, Any] | None = None


class EosRunPlausibilityResponse(BaseModel):
    run_id: int
    status: str
    findings: list[EosRunPlausibilityFinding] = Field(default_factory=list)


class EosForceRunResponse(BaseModel):
    run_id: int
    status: str
    message: str


class EosPredictionRefreshRequest(BaseModel):
    scope: Literal["all", "pv", "prices", "load"] = "all"


class EosPredictionRefreshResponse(BaseModel):
    run_id: int
    scope: Literal["all", "pv", "prices", "load"]
    status: str
    message: str


class EosRunContextResponse(BaseModel):
    run_id: int
    parameter_profile_id: int | None = None
    parameter_revision_id: int | None = None
    parameter_payload_json: dict[str, Any] | list[Any]
    mappings_snapshot_json: dict[str, Any] | list[Any]
    live_state_snapshot_json: dict[str, Any] | list[Any]
    runtime_config_snapshot_json: dict[str, Any] | list[Any]
    assembled_eos_input_json: dict[str, Any] | list[Any]
    created_at: datetime


class MeasurementSyncRunResponse(BaseModel):
    id: int
    trigger_source: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    pushed_count: int
    details_json: dict[str, Any] | list[Any] | None = None
    error_text: str | None = None


class EosMeasurementSyncStatusResponse(BaseModel):
    enabled: bool
    running: bool
    sync_seconds: int | None = None
    next_due_ts: datetime | None = None
    force_in_progress: bool
    last_run_id: int | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_run: MeasurementSyncRunResponse | None = None


class EosMeasurementSyncForceResponse(BaseModel):
    run_id: int
    status: str
    message: str
