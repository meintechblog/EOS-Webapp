from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


RunStatus = str


class CollectorStatusResponse(BaseModel):
    running: bool
    poll_seconds: int
    last_poll_ts: datetime | None = None
    last_successful_sync_ts: datetime | None = None
    last_observed_eos_run_datetime: datetime | None = None
    force_run_in_progress: bool
    last_force_request_ts: datetime | None = None
    last_error: str | None = None
    aligned_scheduler_enabled: bool = False
    aligned_scheduler_minutes: str = ""
    aligned_scheduler_delay_seconds: int = 0
    aligned_scheduler_next_due_ts: datetime | None = None
    aligned_scheduler_last_trigger_ts: datetime | None = None
    aligned_scheduler_last_skip_reason: str | None = None


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


class EosOutputCurrentItemResponse(BaseModel):
    run_id: int
    resource_id: str
    actuator_id: str | None = None
    operation_mode_id: str | None = None
    operation_mode_factor: float | None = None
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
    execution_time: datetime | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    source_instruction: dict[str, Any] = Field(default_factory=dict)
    deduped: bool = True


class OutputDispatchEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int | None = None
    resource_id: str | None = None
    execution_time: datetime | None = None
    dispatch_kind: str
    target_url: str | None = None
    request_payload_json: dict[str, Any] | list[Any]
    status: str
    http_status: int | None = None
    error_text: str | None = None
    idempotency_key: str
    created_at: datetime


class OutputDispatchForceRequest(BaseModel):
    resource_ids: list[str] | None = None


class OutputDispatchForceResponse(BaseModel):
    status: str
    message: str
    run_id: int | None = None
    queued_resources: list[str] = Field(default_factory=list)


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


class EosMqttOutputEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    topic: str
    payload_json: dict[str, Any] | list[Any]
    qos: int
    retain: bool
    output_kind: str = "unknown"
    resource_id: str | None = None
    publish_status: str
    error_text: str | None = None
    published_at: datetime


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


class ControlTargetBase(BaseModel):
    resource_id: str = Field(min_length=1, max_length=128)
    command_topic: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    dry_run_only: bool = True
    qos: int = Field(default=1, ge=0, le=2)
    retain: bool = False
    payload_template_json: dict[str, Any] | list[Any] | None = None


class ControlTargetCreateRequest(ControlTargetBase):
    pass


class ControlTargetUpdateRequest(BaseModel):
    resource_id: str | None = Field(default=None, min_length=1, max_length=128)
    command_topic: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    dry_run_only: bool | None = None
    qos: int | None = Field(default=None, ge=0, le=2)
    retain: bool | None = None
    payload_template_json: dict[str, Any] | list[Any] | None = None


class ControlTargetResponse(ControlTargetBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    updated_at: datetime


class OutputTargetBase(BaseModel):
    resource_id: str = Field(min_length=1, max_length=128)
    webhook_url: str = Field(min_length=1, max_length=512)
    method: str = Field(default="POST", min_length=3, max_length=8)
    headers_json: dict[str, Any] | list[Any] = Field(default_factory=dict)
    enabled: bool = True
    timeout_seconds: int = Field(default=10, ge=1, le=300)
    retry_max: int = Field(default=2, ge=0, le=10)
    payload_template_json: dict[str, Any] | list[Any] | None = None


class OutputTargetCreateRequest(OutputTargetBase):
    pass


class OutputTargetUpdateRequest(BaseModel):
    resource_id: str | None = Field(default=None, min_length=1, max_length=128)
    webhook_url: str | None = Field(default=None, min_length=1, max_length=512)
    method: str | None = Field(default=None, min_length=3, max_length=8)
    headers_json: dict[str, Any] | list[Any] | None = None
    enabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    retry_max: int | None = Field(default=None, ge=0, le=10)
    payload_template_json: dict[str, Any] | list[Any] | None = None


class OutputTargetResponse(OutputTargetBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    updated_at: datetime


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
