from bisect import bisect_left
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies import (
    get_eos_measurement_sync_service,
    get_eos_orchestrator_service,
    get_output_projection_service,
)
from app.repositories.eos_runtime import (
    get_latest_artifact_for_run,
    get_run_by_id,
    get_run_input_snapshot,
    list_artifacts_for_run,
    list_plan_instructions_for_run,
    list_runs,
)
from app.schemas.eos_runtime import (
    EosAutoRunUpdateRequest,
    EosAutoRunUpdateResponse,
    EosForceRunResponse,
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
    EosRunPredictionSeriesPointResponse,
    EosRunPredictionSeriesResponse,
    EosRunSolutionResponse,
    EosRunSummaryResponse,
    EosRuntimeConfigUpdateRequest,
    EosRuntimeConfigUpdateResponse,
    EosRuntimeResponse,
)
from app.services.eos_measurement_sync import EosMeasurementSyncService
from app.services.eos_orchestrator import EosOrchestratorService
from app.services.output_projection import OutputProjectionService


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


@router.put("/runtime/auto-run", response_model=EosAutoRunUpdateResponse)
def put_eos_runtime_auto_run(
    payload: EosAutoRunUpdateRequest,
    orchestrator: EosOrchestratorService = Depends(get_eos_orchestrator_service),
) -> EosAutoRunUpdateResponse:
    try:
        applied = orchestrator.update_auto_run_preset(preset=payload.preset)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    runtime_snapshot = EosRuntimeResponse.model_validate(orchestrator.get_runtime_snapshot())
    return EosAutoRunUpdateResponse(
        preset=payload.preset,
        applied_slots=[int(value) for value in applied.get("applied_slots", [])],
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


@router.get("/runs/{run_id}/prediction-series", response_model=EosRunPredictionSeriesResponse)
def get_run_prediction_series(
    run_id: int,
    db: Session = Depends(get_db),
) -> EosRunPredictionSeriesResponse:
    run = get_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    def _artifact_payload(key: str) -> Any | None:
        artifact = get_latest_artifact_for_run(
            db,
            run_id=run_id,
            artifact_type="prediction_series",
            artifact_key=key,
        )
        return artifact.payload_json if artifact else None

    date_payload = _artifact_payload("date_time")
    date_index = _extract_prediction_datetime_index_from_payload(date_payload)
    if not date_index:
        return EosRunPredictionSeriesResponse(run_id=run_id, source="none", points=[])

    def _pick_series(keys: list[str]) -> tuple[list[Any] | None, dict[datetime, float] | None]:
        for key in keys:
            payload = _artifact_payload(key)
            values = _extract_prediction_values_from_payload(payload)
            by_ts = _extract_prediction_numeric_map_from_payload(payload)
            if values is not None or by_ts:
                return values, by_ts
        return None, None

    price_kwh_values, price_kwh_by_ts = _pick_series(["elecprice_marketprice_kwh", "elec_price_amt_kwh"])
    price_wh_values, price_wh_by_ts = _pick_series(["elecprice_marketprice_wh", "strompreis_euro_pro_wh"])
    pv_ac_power_values, pv_ac_power_by_ts = _pick_series(["pvforecast_ac_power"])
    pv_dc_power_values, pv_dc_power_by_ts = _pick_series(["pvforecast_dc_power"])
    load_power_values, load_power_by_ts = _pick_series(
        [
            "loadforecast_power_w",
            "load_mean_adjusted",
            "load_mean",
            "loadakkudoktor_mean_power_w",
        ]
    )
    price_kwh_lookup = _build_prediction_numeric_lookup(price_kwh_by_ts)
    price_wh_lookup = _build_prediction_numeric_lookup(price_wh_by_ts)
    pv_ac_lookup = _build_prediction_numeric_lookup(pv_ac_power_by_ts)
    pv_dc_lookup = _build_prediction_numeric_lookup(pv_dc_power_by_ts)
    load_lookup = _build_prediction_numeric_lookup(load_power_by_ts)

    points: list[EosRunPredictionSeriesPointResponse] = []
    for index, date_time in enumerate(date_index):
        price_ct_per_kwh = _resolve_series_value(
            values=price_kwh_values,
            by_ts=price_kwh_by_ts,
            by_ts_lookup=price_kwh_lookup,
            ts=date_time,
            index=index,
            factor=100.0,
        )
        if price_ct_per_kwh is None:
            price_ct_per_kwh = _resolve_series_value(
                values=price_wh_values,
                by_ts=price_wh_by_ts,
                by_ts_lookup=price_wh_lookup,
                ts=date_time,
                index=index,
                factor=100000.0,
            )

        pv_ac_kw = _resolve_series_value(
            values=pv_ac_power_values,
            by_ts=pv_ac_power_by_ts,
            by_ts_lookup=pv_ac_lookup,
            ts=date_time,
            index=index,
            factor=0.001,
        )
        pv_dc_kw = _resolve_series_value(
            values=pv_dc_power_values,
            by_ts=pv_dc_power_by_ts,
            by_ts_lookup=pv_dc_lookup,
            ts=date_time,
            index=index,
            factor=0.001,
        )
        load_kw = _resolve_series_value(
            values=load_power_values,
            by_ts=load_power_by_ts,
            by_ts_lookup=load_lookup,
            ts=date_time,
            index=index,
            factor=0.001,
        )

        points.append(
            EosRunPredictionSeriesPointResponse(
                date_time=date_time,
                elec_price_ct_per_kwh=price_ct_per_kwh,
                pv_ac_kw=pv_ac_kw,
                pv_dc_kw=pv_dc_kw,
                load_kw=load_kw,
            )
        )

    return EosRunPredictionSeriesResponse(
        run_id=run_id,
        source="artifact_prediction_series",
        points=points,
    )


@router.get("/outputs/current", response_model=list[EosOutputCurrentItemResponse])
def get_outputs_current(
    run_id: int | None = None,
    db: Session = Depends(get_db),
    projection_service: OutputProjectionService = Depends(get_output_projection_service),
) -> list[EosOutputCurrentItemResponse]:
    selected_run_id, rows = projection_service.get_current_outputs(db, run_id=run_id)
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
    projection_service: OutputProjectionService = Depends(get_output_projection_service),
) -> list[EosOutputTimelineItemResponse]:
    parsed_from = _parse_iso_datetime(from_ts) if from_ts else None
    parsed_to = _parse_iso_datetime(to_ts) if to_ts else None
    _, rows = projection_service.get_timeline(
        db,
        run_id=run_id,
        from_ts=parsed_from,
        to_ts=parsed_to,
        resource_id=resource_id,
    )
    return [EosOutputTimelineItemResponse.model_validate(row) for row in rows]


@router.get("/runs/{run_id}/plausibility", response_model=EosRunPlausibilityResponse)
def get_run_plausibility(
    run_id: int,
    db: Session = Depends(get_db),
    projection_service: OutputProjectionService = Depends(get_output_projection_service),
) -> EosRunPlausibilityResponse:
    run = get_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    effective_run_id = projection_service.resolve_dispatchable_run_id(db, run_id=run_id)
    plausibility_run_id = effective_run_id if effective_run_id is not None else run_id

    plan = get_latest_artifact_for_run(
        db,
        run_id=plausibility_run_id,
        artifact_type="plan",
        artifact_key="latest",
    )
    solution = get_latest_artifact_for_run(
        db,
        run_id=plausibility_run_id,
        artifact_type="solution",
        artifact_key=None,
    )
    _, timeline = projection_service.get_timeline(db, run_id=plausibility_run_id)

    findings: list[EosRunPlausibilityFinding] = []
    if plausibility_run_id != run_id:
        findings.append(
            EosRunPlausibilityFinding(
                level="ok",
                code="fallback_run_used",
                message=(
                    "Ausgewaehlter Run hat keine verwertbaren Entscheidungen; "
                    f"Plausibilitaet basiert auf Run #{plausibility_run_id}."
                ),
                details={
                    "requested_run_id": run_id,
                    "effective_run_id": plausibility_run_id,
                },
            )
        )
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
        grid_consumption_kwh = (grid_consumption_wh / 1000.0) if grid_consumption_wh is not None else None
        grid_feedin_kwh = (grid_feedin_wh / 1000.0) if grid_feedin_wh is not None else None

        if grid_consumption_wh is not None and grid_consumption_wh < 0:
            findings.append(
                EosRunPlausibilityFinding(
                    level="error",
                    code="negative_grid_consumption",
                    message="Netzbezug (kWh) ist negativ.",
                    details={"grid_consumption_energy_kwh": grid_consumption_kwh},
                )
            )
        if grid_feedin_wh is not None and grid_feedin_wh < 0:
            findings.append(
                EosRunPlausibilityFinding(
                    level="error",
                    code="negative_grid_feedin",
                    message="Netzeinspeisung (kWh) ist negativ.",
                    details={"grid_feedin_energy_kwh": grid_feedin_kwh},
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
                        "grid_consumption_energy_kwh": grid_consumption_kwh,
                        "grid_feedin_energy_kwh": grid_feedin_kwh,
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

    return EosRunPlausibilityResponse(
        run_id=plausibility_run_id,
        status=status_value,
        findings=findings,
    )


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


def _coerce_series_value(values: list[Any] | None, index: int, *, factor: float = 1.0) -> float | None:
    if values is None or index < 0 or index >= len(values):
        return None
    raw = _coerce_float(values[index])
    if raw is None:
        return None
    return raw * factor


def _resolve_series_value(
    *,
    values: list[Any] | None,
    by_ts: dict[datetime, float] | None,
    by_ts_lookup: tuple[list[datetime], float] | None = None,
    ts: datetime,
    index: int,
    factor: float = 1.0,
) -> float | None:
    if by_ts is not None:
        mapped = _resolve_series_value_from_map(
            by_ts=by_ts,
            ts=ts,
            lookup=by_ts_lookup,
        )
        if mapped is not None:
            return mapped * factor
    return _coerce_series_value(values, index, factor=factor)


def _resolve_series_value_from_map(
    *,
    by_ts: dict[datetime, float],
    ts: datetime,
    lookup: tuple[list[datetime], float] | None = None,
) -> float | None:
    direct = by_ts.get(ts)
    if direct is not None:
        return direct

    resolved_lookup = lookup or _build_prediction_numeric_lookup(by_ts)
    if resolved_lookup is None:
        return None
    sorted_ts, max_age_seconds = resolved_lookup
    if not sorted_ts:
        return None

    insert_index = bisect_left(sorted_ts, ts)
    if insert_index > 0:
        previous_ts = sorted_ts[insert_index - 1]
        previous_value = by_ts.get(previous_ts)
        if previous_value is not None:
            age_seconds = (ts - previous_ts).total_seconds()
            if 0.0 <= age_seconds <= max_age_seconds:
                return previous_value

    # Start-of-series fallback only when there is no previous value.
    if insert_index == 0 and insert_index < len(sorted_ts):
        next_ts = sorted_ts[insert_index]
        next_value = by_ts.get(next_ts)
        if next_value is not None:
            next_distance = (next_ts - ts).total_seconds()
            if 0.0 <= next_distance <= (max_age_seconds / 2.0):
                return next_value

    return None


def _build_prediction_numeric_lookup(
    by_ts: dict[datetime, float] | None,
) -> tuple[list[datetime], float] | None:
    if not by_ts:
        return None
    sorted_ts = sorted(by_ts.keys())
    if not sorted_ts:
        return None
    source_step_seconds = _infer_prediction_source_step_seconds(sorted_ts)
    max_age_seconds = max(3600.0, source_step_seconds * 1.25)
    return sorted_ts, max_age_seconds


def _infer_prediction_source_step_seconds(sorted_ts: list[datetime]) -> float:
    if len(sorted_ts) <= 1:
        return 3600.0

    diffs = [
        (sorted_ts[index] - sorted_ts[index - 1]).total_seconds()
        for index in range(1, len(sorted_ts))
        if (sorted_ts[index] - sorted_ts[index - 1]).total_seconds() > 0
    ]
    if not diffs:
        return 3600.0
    diffs.sort()
    mid = len(diffs) // 2
    if len(diffs) % 2 == 0:
        median_seconds = (diffs[mid - 1] + diffs[mid]) / 2.0
    else:
        median_seconds = diffs[mid]
    return max(1.0, median_seconds)


def _extract_prediction_values_from_payload(payload: Any) -> list[Any] | None:
    if isinstance(payload, dict):
        values = payload.get("values")
        if isinstance(values, list):
            return values
        return None
    if isinstance(payload, list):
        return payload
    return None


def _extract_prediction_data_pairs_from_payload(payload: Any) -> list[tuple[datetime, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []

    pairs: list[tuple[datetime, Any]] = []
    for raw_ts, raw_value in data.items():
        ts = _coerce_prediction_series_datetime(raw_ts)
        if isinstance(raw_value, dict):
            explicit_ts = _extract_prediction_row_datetime(raw_value)
            if explicit_ts is not None:
                ts = explicit_ts
        if ts is None:
            continue

        value: Any = raw_value
        if isinstance(raw_value, dict):
            value = _extract_prediction_row_value(raw_value)
        pairs.append((ts, value))

    pairs.sort(key=lambda item: item[0])
    return pairs


def _extract_prediction_datetime_index_from_payload(payload: Any) -> list[datetime]:
    data_pairs = _extract_prediction_data_pairs_from_payload(payload)
    if data_pairs:
        datetimes: list[datetime] = []
        for ts, raw_value in data_pairs:
            parsed = _coerce_prediction_series_datetime(raw_value)
            datetimes.append(parsed or ts)
        return _dedupe_datetimes(datetimes)

    values = _extract_prediction_values_from_payload(payload)
    if values is None:
        return []
    datetimes = [_coerce_prediction_series_datetime(raw) for raw in values]
    return _dedupe_datetimes([dt for dt in datetimes if dt is not None])


def _extract_prediction_numeric_map_from_payload(payload: Any) -> dict[datetime, float] | None:
    data_pairs = _extract_prediction_data_pairs_from_payload(payload)
    if not data_pairs:
        return None

    values_by_ts: dict[datetime, float] = {}
    for ts, raw_value in data_pairs:
        numeric = _coerce_float(raw_value)
        if numeric is None:
            continue
        values_by_ts[ts] = numeric
    return values_by_ts or None


def _extract_prediction_row_datetime(row: dict[str, Any]) -> datetime | None:
    for key in ("date_time", "datetime", "ts", "timestamp", "start_datetime"):
        parsed = _coerce_prediction_series_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _extract_prediction_row_value(row: dict[str, Any]) -> Any:
    for key in ("value", "y", "v"):
        if key in row:
            return row[key]
    for key in ("date_time", "datetime", "ts", "timestamp", "start_datetime"):
        if key in row:
            return row[key]
    for value in row.values():
        if isinstance(value, (str, int, float, bool)):
            return value
    return None


def _dedupe_datetimes(values: list[datetime]) -> list[datetime]:
    if not values:
        return []
    deduped: list[datetime] = []
    last: datetime | None = None
    for value in sorted(values):
        if last is not None and value == last:
            continue
        deduped.append(value)
        last = value
    return deduped


def _coerce_prediction_series_datetime(value: Any) -> datetime | None:
    if isinstance(value, str):
        raw = value.strip()
        if raw == "":
            return None
        if raw.lstrip("-").isdigit():
            try:
                numeric_int = int(raw)
            except ValueError:
                numeric_int = None
            if numeric_int is not None:
                return _timestamp_to_datetime_utc(numeric_int)

        numeric_float = _coerce_float(raw)
        if numeric_float is not None:
            return _timestamp_to_datetime_utc(numeric_float)
        candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    if isinstance(value, int):
        return _timestamp_to_datetime_utc(value)

    numeric_float = _coerce_float(value)
    if numeric_float is None:
        return None
    return _timestamp_to_datetime_utc(numeric_float)


def _timestamp_to_datetime_utc(value: int | float) -> datetime | None:
    try:
        if isinstance(value, int):
            absolute = abs(value)
            if absolute >= 10**18:
                seconds = value // 1_000_000_000
                nanos = value % 1_000_000_000
                if nanos < 0:
                    nanos += 1_000_000_000
                    seconds -= 1
                return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
                    microseconds=round(nanos / 1000)
                )
            if absolute >= 10**15:
                seconds = value // 1_000_000
                micros = value % 1_000_000
                if micros < 0:
                    micros += 1_000_000
                    seconds -= 1
                return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(microseconds=micros)
            if absolute >= 10**12:
                return datetime.fromtimestamp(value / 1_000.0, tz=timezone.utc)
            return datetime.fromtimestamp(value, tz=timezone.utc)

        absolute = abs(value)
        if absolute >= 1e18:
            seconds = value / 1e9
        elif absolute >= 1e15:
            seconds = value / 1e6
        elif absolute >= 1e12:
            seconds = value / 1e3
        else:
            seconds = value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
