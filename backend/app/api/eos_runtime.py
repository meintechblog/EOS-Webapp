from collections import Counter
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies import (
    get_eos_measurement_sync_service,
    get_eos_orchestrator_service,
    get_output_dispatch_service,
)
from app.repositories.eos_runtime import (
    create_control_target,
    create_output_target,
    get_latest_artifact_for_run,
    get_run_input_snapshot,
    get_run_by_id,
    get_control_target_by_id,
    get_output_target_by_id,
    list_artifacts_for_run,
    list_control_targets,
    list_output_dispatch_events,
    list_output_events,
    list_output_targets,
    list_plan_instructions_for_run,
    list_runs,
    update_control_target,
    update_output_target,
)
from app.schemas.eos_runtime import (
    ControlTargetCreateRequest,
    ControlTargetResponse,
    ControlTargetUpdateRequest,
    EosForceRunResponse,
    EosMqttOutputEventResponse,
    EosMeasurementSyncForceResponse,
    EosMeasurementSyncStatusResponse,
    EosOutputCurrentItemResponse,
    EosOutputTimelineItemResponse,
    EosPredictionRefreshRequest,
    EosPredictionRefreshResponse,
    EosRunPlausibilityFinding,
    EosRunPlausibilityResponse,
    EosRunContextResponse,
    EosRunDetailResponse,
    EosRunPlanResponse,
    EosRunSolutionResponse,
    EosRunSummaryResponse,
    EosRuntimeConfigUpdateRequest,
    EosRuntimeConfigUpdateResponse,
    EosRuntimeResponse,
    OutputDispatchEventResponse,
    OutputDispatchForceRequest,
    OutputDispatchForceResponse,
    OutputTargetCreateRequest,
    OutputTargetResponse,
    OutputTargetUpdateRequest,
)
from app.services.eos_measurement_sync import EosMeasurementSyncService
from app.services.eos_orchestrator import EosOrchestratorService
from app.services.output_dispatch import OutputDispatchService


router = APIRouter(prefix="/api/eos", tags=["eos-runtime"])


@router.get("/runtime", response_model=EosRuntimeResponse)
def get_eos_runtime(
    orchestrator: EosOrchestratorService = Depends(get_eos_orchestrator_service),
) -> EosRuntimeResponse:
    return EosRuntimeResponse.model_validate(orchestrator.get_runtime_snapshot())


@router.put("/runtime/config", response_model=EosRuntimeConfigUpdateResponse)
def put_eos_runtime_config(
    payload: EosRuntimeConfigUpdateRequest,
    orchestrator: EosOrchestratorService = Depends(get_eos_orchestrator_service),
) -> EosRuntimeConfigUpdateResponse:
    try:
        applied = orchestrator.update_runtime_config(
            mode=payload.ems_mode,
            interval_seconds=payload.ems_interval_seconds,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    runtime_snapshot = EosRuntimeResponse.model_validate(orchestrator.get_runtime_snapshot())
    return EosRuntimeConfigUpdateResponse(
        ems_mode=payload.ems_mode,
        ems_interval_seconds=payload.ems_interval_seconds,
        applied_mode_path=applied["mode_path"],
        applied_interval_path=applied["interval_path"],
        runtime=runtime_snapshot,
    )


@router.post("/runs/force", response_model=EosForceRunResponse, status_code=status.HTTP_202_ACCEPTED)
def post_force_run(
    orchestrator: EosOrchestratorService = Depends(get_eos_orchestrator_service),
) -> EosForceRunResponse:
    try:
        run_id = orchestrator.request_force_run()
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    return EosForceRunResponse(
        run_id=run_id,
        status="accepted",
        message="Force run started asynchronously",
    )


@router.post(
    "/runs/predictions/refresh",
    response_model=EosPredictionRefreshResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def post_prediction_refresh(
    payload: EosPredictionRefreshRequest,
    orchestrator: EosOrchestratorService = Depends(get_eos_orchestrator_service),
) -> EosPredictionRefreshResponse:
    try:
        run_id = orchestrator.request_prediction_refresh(scope=payload.scope)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    return EosPredictionRefreshResponse(
        run_id=run_id,
        scope=payload.scope,
        status="accepted",
        message="Prediction refresh run started asynchronously",
    )


@router.get("/runs", response_model=list[EosRunSummaryResponse])
def get_runs(db: Session = Depends(get_db)) -> list[EosRunSummaryResponse]:
    runs = list_runs(db)
    return [EosRunSummaryResponse.model_validate(run) for run in runs]


@router.get("/runs/{run_id}", response_model=EosRunDetailResponse)
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
) -> EosRunDetailResponse:
    run = get_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    artifacts = list_artifacts_for_run(db, run_id)
    summary = Counter(artifact.artifact_type for artifact in artifacts)
    return EosRunDetailResponse(
        id=run.id,
        trigger_source=run.trigger_source,
        run_mode=run.run_mode,
        eos_last_run_datetime=run.eos_last_run_datetime,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error_text=run.error_text,
        created_at=run.created_at,
        artifact_summary=dict(summary),
    )


@router.get("/runs/{run_id}/plan", response_model=EosRunPlanResponse)
def get_run_plan(
    run_id: int,
    db: Session = Depends(get_db),
) -> EosRunPlanResponse:
    run = get_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    artifact = get_latest_artifact_for_run(
        db,
        run_id=run_id,
        artifact_type="plan",
        artifact_key="latest",
    )
    instructions = list_plan_instructions_for_run(db, run_id)

    return EosRunPlanResponse(
        run_id=run_id,
        payload_json=artifact.payload_json if artifact else None,
        valid_from=artifact.valid_from if artifact else None,
        valid_until=artifact.valid_until if artifact else None,
        instructions=instructions,
    )


@router.get("/runs/{run_id}/solution", response_model=EosRunSolutionResponse)
def get_run_solution(
    run_id: int,
    db: Session = Depends(get_db),
) -> EosRunSolutionResponse:
    run = get_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    artifact = get_latest_artifact_for_run(
        db,
        run_id=run_id,
        artifact_type="solution",
        artifact_key=None,
    )
    return EosRunSolutionResponse(run_id=run_id, payload_json=artifact.payload_json if artifact else None)


@router.get("/outputs/current", response_model=list[EosOutputCurrentItemResponse])
def get_outputs_current(
    run_id: int | None = None,
    db: Session = Depends(get_db),
    dispatch_service: OutputDispatchService = Depends(get_output_dispatch_service),
) -> list[EosOutputCurrentItemResponse]:
    selected_run_id, rows = dispatch_service.get_current_outputs(db, run_id=run_id)
    if selected_run_id is None:
        return []
    return [EosOutputCurrentItemResponse.model_validate(row) for row in rows]


@router.get("/outputs/timeline", response_model=list[EosOutputTimelineItemResponse])
def get_outputs_timeline(
    run_id: int | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    resource_id: str | None = None,
    db: Session = Depends(get_db),
    dispatch_service: OutputDispatchService = Depends(get_output_dispatch_service),
) -> list[EosOutputTimelineItemResponse]:
    parsed_from = _parse_iso_datetime(from_ts) if from_ts else None
    parsed_to = _parse_iso_datetime(to_ts) if to_ts else None
    _, rows = dispatch_service.get_timeline(
        db,
        run_id=run_id,
        from_ts=parsed_from,
        to_ts=parsed_to,
        resource_id=resource_id,
    )
    return [EosOutputTimelineItemResponse.model_validate(row) for row in rows]


@router.get("/outputs/events", response_model=list[OutputDispatchEventResponse])
def get_outputs_events(
    run_id: int | None = None,
    resource_id: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
) -> list[OutputDispatchEventResponse]:
    parsed_from = _parse_iso_datetime(from_ts) if from_ts else None
    parsed_to = _parse_iso_datetime(to_ts) if to_ts else None
    events = list_output_dispatch_events(
        db,
        run_id=run_id,
        resource_id=resource_id,
        from_ts=parsed_from,
        to_ts=parsed_to,
        limit=max(1, min(limit, 1000)),
    )
    return [OutputDispatchEventResponse.model_validate(event) for event in events]


@router.post(
    "/outputs/dispatch/force",
    response_model=OutputDispatchForceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def post_outputs_dispatch_force(
    payload: OutputDispatchForceRequest,
    dispatch_service: OutputDispatchService = Depends(get_output_dispatch_service),
) -> OutputDispatchForceResponse:
    try:
        result = dispatch_service.request_force_dispatch(resource_ids=payload.resource_ids)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    return OutputDispatchForceResponse(
        status=str(result.get("status", "accepted")),
        message=str(result.get("message", "Force dispatch queued")),
        run_id=int(result["run_id"]) if result.get("run_id") is not None else None,
        queued_resources=[str(item) for item in result.get("queued_resources", [])],
    )


@router.get("/runs/{run_id}/plausibility", response_model=EosRunPlausibilityResponse)
def get_run_plausibility(
    run_id: int,
    db: Session = Depends(get_db),
    dispatch_service: OutputDispatchService = Depends(get_output_dispatch_service),
) -> EosRunPlausibilityResponse:
    run = get_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    plan = get_latest_artifact_for_run(db, run_id=run_id, artifact_type="plan", artifact_key="latest")
    solution = get_latest_artifact_for_run(db, run_id=run_id, artifact_type="solution", artifact_key=None)
    _, timeline = dispatch_service.get_timeline(db, run_id=run_id)

    findings: list[EosRunPlausibilityFinding] = []
    if plan is None:
        findings.append(
            EosRunPlausibilityFinding(
                level="error",
                code="missing_plan",
                message="Kein Plan-Artefakt vorhanden.",
            )
        )
    if solution is None:
        findings.append(
            EosRunPlausibilityFinding(
                level="error",
                code="missing_solution",
                message="Kein Solution-Artefakt vorhanden.",
            )
        )

    if timeline:
        findings.append(
            EosRunPlausibilityFinding(
                level="ok",
                code="timeline_available",
                message=f"{len(timeline)} Zustandswechsel aus Plan-Instruktionen abgeleitet.",
            )
        )
    else:
        findings.append(
            EosRunPlausibilityFinding(
                level="warn",
                code="timeline_empty",
                message="Keine ableitbaren Zustandswechsel im Plan gefunden.",
            )
        )

    if isinstance(solution.payload_json if solution else None, dict):
        payload = solution.payload_json  # type: ignore[assignment]
        grid_consumption_wh = _coerce_float(payload.get("grid_consumption_energy_wh"))
        grid_feedin_wh = _coerce_float(payload.get("grid_feedin_energy_wh"))
        costs_amt = _coerce_float(payload.get("costs_amt"))

        if grid_consumption_wh is not None and grid_consumption_wh < 0:
            findings.append(
                EosRunPlausibilityFinding(
                    level="error",
                    code="negative_grid_consumption",
                    message="`grid_consumption_energy_wh` ist negativ.",
                    details={"value": grid_consumption_wh},
                )
            )
        if grid_feedin_wh is not None and grid_feedin_wh < 0:
            findings.append(
                EosRunPlausibilityFinding(
                    level="error",
                    code="negative_grid_feedin",
                    message="`grid_feedin_energy_wh` ist negativ.",
                    details={"value": grid_feedin_wh},
                )
            )
        if costs_amt is not None and costs_amt > 1000:
            findings.append(
                EosRunPlausibilityFinding(
                    level="warn",
                    code="high_costs",
                    message="`costs_amt` ist ungewöhnlich hoch.",
                    details={"value": costs_amt},
                )
            )
        if costs_amt is not None and grid_consumption_wh is not None:
            findings.append(
                EosRunPlausibilityFinding(
                    level="ok",
                    code="cost_energy_context",
                    message="Kosten/Netzbezug sind vorhanden und auswertbar.",
                    details={
                        "costs_amt": costs_amt,
                        "grid_consumption_energy_wh": grid_consumption_wh,
                        "grid_feedin_energy_wh": grid_feedin_wh,
                    },
                )
            )

    if not findings:
        findings.append(
            EosRunPlausibilityFinding(
                level="warn",
                code="no_signals",
                message="Keine Plausibilitätsmerkmale verfügbar.",
            )
        )

    status_value = "ok"
    if any(item.level == "error" for item in findings):
        status_value = "error"
    elif any(item.level == "warn" for item in findings):
        status_value = "warn"

    return EosRunPlausibilityResponse(run_id=run_id, status=status_value, findings=findings)


@router.get("/runs/{run_id}/context", response_model=EosRunContextResponse)
def get_run_context(
    run_id: int,
    db: Session = Depends(get_db),
) -> EosRunContextResponse:
    run = get_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    snapshot = get_run_input_snapshot(db, run_id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run context snapshot not available",
        )

    return EosRunContextResponse(
        run_id=run_id,
        parameter_profile_id=snapshot.parameter_profile_id,
        parameter_revision_id=snapshot.parameter_revision_id,
        parameter_payload_json=snapshot.parameter_payload_json,
        mappings_snapshot_json=snapshot.mappings_snapshot_json,
        live_state_snapshot_json=snapshot.live_state_snapshot_json,
        runtime_config_snapshot_json=snapshot.runtime_config_snapshot_json,
        assembled_eos_input_json=snapshot.assembled_eos_input_json,
        created_at=snapshot.created_at,
    )


@router.get("/output-events", response_model=list[EosMqttOutputEventResponse])
def get_output_events(db: Session = Depends(get_db)) -> list[EosMqttOutputEventResponse]:
    events = list_output_events(db)
    return [EosMqttOutputEventResponse.model_validate(event) for event in events]


@router.get("/output-targets", response_model=list[OutputTargetResponse])
def get_output_targets(db: Session = Depends(get_db)) -> list[OutputTargetResponse]:
    targets = list_output_targets(db)
    return [OutputTargetResponse.model_validate(target) for target in targets]


@router.post("/output-targets", response_model=OutputTargetResponse, status_code=status.HTTP_201_CREATED)
def create_target_http(
    payload: OutputTargetCreateRequest,
    db: Session = Depends(get_db),
) -> OutputTargetResponse:
    method = payload.method.upper().strip()
    try:
        target = create_output_target(
            db,
            resource_id=payload.resource_id,
            webhook_url=payload.webhook_url,
            method=method,
            headers_json=payload.headers_json,
            enabled=payload.enabled,
            timeout_seconds=payload.timeout_seconds,
            retry_max=payload.retry_max,
            payload_template_json=payload.payload_template_json,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Output target conflict: {exc.orig}",
        )
    return OutputTargetResponse.model_validate(target)


@router.put("/output-targets/{target_id}", response_model=OutputTargetResponse)
def update_target_http(
    target_id: int,
    payload: OutputTargetUpdateRequest,
    db: Session = Depends(get_db),
) -> OutputTargetResponse:
    target = get_output_target_by_id(db, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output target not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field must be provided",
        )

    try:
        updated = update_output_target(
            db,
            target,
            resource_id=updates.get("resource_id"),
            webhook_url=updates.get("webhook_url"),
            method=updates.get("method").upper().strip() if updates.get("method") else None,
            headers_json=updates.get("headers_json", ...),
            enabled=updates.get("enabled"),
            timeout_seconds=updates.get("timeout_seconds"),
            retry_max=updates.get("retry_max"),
            payload_template_json=updates.get("payload_template_json", ...),
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Output target conflict: {exc.orig}",
        )

    return OutputTargetResponse.model_validate(updated)


@router.get("/measurement-sync/status", response_model=EosMeasurementSyncStatusResponse)
def get_measurement_sync_status(
    db: Session = Depends(get_db),
    sync_service: EosMeasurementSyncService = Depends(get_eos_measurement_sync_service),
) -> EosMeasurementSyncStatusResponse:
    snapshot = sync_service.get_status_snapshot(db)
    return EosMeasurementSyncStatusResponse.model_validate(snapshot)


@router.post(
    "/measurement-sync/force",
    response_model=EosMeasurementSyncForceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def post_measurement_sync_force(
    sync_service: EosMeasurementSyncService = Depends(get_eos_measurement_sync_service),
) -> EosMeasurementSyncForceResponse:
    try:
        run_id = sync_service.request_force_sync()
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    return EosMeasurementSyncForceResponse(
        run_id=run_id,
        status="accepted",
        message="Measurement sync started asynchronously",
    )


@router.get("/control-targets", response_model=list[ControlTargetResponse])
def get_targets(db: Session = Depends(get_db)) -> list[ControlTargetResponse]:
    targets = list_control_targets(db)
    return [ControlTargetResponse.model_validate(target) for target in targets]


@router.post("/control-targets", response_model=ControlTargetResponse, status_code=status.HTTP_201_CREATED)
def create_target(
    payload: ControlTargetCreateRequest,
    db: Session = Depends(get_db),
) -> ControlTargetResponse:
    try:
        target = create_control_target(
            db,
            resource_id=payload.resource_id,
            command_topic=payload.command_topic,
            enabled=payload.enabled,
            dry_run_only=payload.dry_run_only,
            qos=payload.qos,
            retain=payload.retain,
            payload_template_json=payload.payload_template_json,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Control target conflict: {exc.orig}",
        )
    return ControlTargetResponse.model_validate(target)


@router.put("/control-targets/{target_id}", response_model=ControlTargetResponse)
def update_target(
    target_id: int,
    payload: ControlTargetUpdateRequest,
    db: Session = Depends(get_db),
) -> ControlTargetResponse:
    target = get_control_target_by_id(db, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Control target not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field must be provided",
        )

    try:
        updated = update_control_target(
            db,
            target,
            resource_id=updates.get("resource_id"),
            command_topic=updates.get("command_topic"),
            enabled=updates.get("enabled"),
            dry_run_only=updates.get("dry_run_only"),
            qos=updates.get("qos"),
            retain=updates.get("retain"),
            payload_template_json=updates.get("payload_template_json", ...),
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Control target conflict: {exc.orig}",
        )

    return ControlTargetResponse.model_validate(updated)


def _parse_iso_datetime(value: str) -> datetime:
    raw = value.strip()
    if raw == "":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="datetime query value is empty")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid datetime value: {value}",
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
