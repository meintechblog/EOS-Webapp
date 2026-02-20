from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies import get_data_pipeline_service
from app.repositories.signal_backbone import (
    fetch_signal_series,
    list_latest_by_signal_keys,
    list_signals_with_latest,
)
from app.repositories.emr_pipeline import (
    EMR_KEY_BY_POWER_KEY,
    POWER_KEYS,
    get_emr_series,
    get_latest_emr_values,
    get_latest_power_samples,
    get_power_series,
)
from app.schemas.data_backbone import (
    DataRetentionStatusResponse,
    EmrPointResponse,
    EmrSeriesResponse,
    JobRunSnapshotResponse,
    PowerSamplePointResponse,
    PowerSeriesResponse,
    SignalLatestItemResponse,
    SignalSeriesPointResponse,
    SignalSeriesResponse,
)
from app.services.data_pipeline import DataPipelineService


router = APIRouter(prefix="/api/data", tags=["data-backbone"])


@router.get("/signals", response_model=list[SignalLatestItemResponse])
def get_signals(
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> list[SignalLatestItemResponse]:
    rows = list_signals_with_latest(db, limit=limit)
    return [SignalLatestItemResponse.model_validate(row) for row in rows]


@router.get("/latest", response_model=list[SignalLatestItemResponse])
def get_latest(
    signal_key: list[str] | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> list[SignalLatestItemResponse]:
    rows = list_latest_by_signal_keys(db, signal_keys=signal_key, limit=limit)
    return [SignalLatestItemResponse.model_validate(row) for row in rows]


@router.get("/series", response_model=SignalSeriesResponse)
def get_series(
    signal_key: str = Query(min_length=1, max_length=160),
    from_ts: datetime | None = Query(default=None, alias="from"),
    to_ts: datetime | None = Query(default=None, alias="to"),
    resolution: str = Query(default="raw", pattern="^(raw|5m|1h|1d)$"),
    db: Session = Depends(get_db),
) -> SignalSeriesResponse:
    from_value = from_ts or (datetime.now(timezone.utc) - timedelta(hours=24))
    to_value = to_ts or datetime.now(timezone.utc)
    if from_value >= to_value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from' must be before 'to'",
        )

    try:
        series = fetch_signal_series(
            db,
            signal_key=signal_key,
            from_ts=from_value,
            to_ts=to_value,
            resolution=resolution,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return SignalSeriesResponse(
        signal_key=series.signal_key,
        resolution=series.resolution,  # type: ignore[arg-type]
        points=[SignalSeriesPointResponse.model_validate(point) for point in series.points],
    )


@router.get("/retention/status", response_model=DataRetentionStatusResponse)
def get_retention_status(
    db: Session = Depends(get_db),
    pipeline_service: DataPipelineService = Depends(get_data_pipeline_service),
) -> DataRetentionStatusResponse:
    snapshot = pipeline_service.get_status_snapshot(db)
    return DataRetentionStatusResponse(
        last_rollup_run=(
            JobRunSnapshotResponse.model_validate(snapshot["last_rollup_run"])
            if snapshot.get("last_rollup_run")
            else None
        ),
        last_retention_run=(
            JobRunSnapshotResponse.model_validate(snapshot["last_retention_run"])
            if snapshot.get("last_retention_run")
            else None
        ),
        raw_rows_24h=int(snapshot.get("raw_rows_24h", 0)),
        rollup_rows_24h=int(snapshot.get("rollup_rows_24h", 0)),
        signal_catalog_count=int(snapshot.get("signal_catalog_count", 0)),
    )


@router.get("/power/latest", response_model=list[PowerSamplePointResponse])
def get_power_latest(
    key: list[str] | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[PowerSamplePointResponse]:
    keys = key if key else list(POWER_KEYS)
    rows = get_latest_power_samples(db, keys=keys)
    return [PowerSamplePointResponse.model_validate(row) for row in rows]


@router.get("/power/series", response_model=PowerSeriesResponse)
def get_power_series_endpoint(
    key: str = Query(min_length=1, max_length=64),
    from_ts: datetime | None = Query(default=None, alias="from"),
    to_ts: datetime | None = Query(default=None, alias="to"),
    db: Session = Depends(get_db),
) -> PowerSeriesResponse:
    from_value = from_ts or (datetime.now(timezone.utc) - timedelta(hours=24))
    to_value = to_ts or datetime.now(timezone.utc)
    if from_value >= to_value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from' must be before 'to'",
        )

    rows = get_power_series(db, key=key, from_ts=from_value, to_ts=to_value)
    return PowerSeriesResponse(
        key=key,
        points=[PowerSamplePointResponse.model_validate(row) for row in rows],
    )


@router.get("/emr/latest", response_model=list[EmrPointResponse])
def get_emr_latest(
    emr_key: list[str] | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[EmrPointResponse]:
    keys = emr_key if emr_key else sorted(EMR_KEY_BY_POWER_KEY.values())
    rows = get_latest_emr_values(db, emr_keys=keys)
    return [EmrPointResponse.model_validate(row) for row in rows]


@router.get("/emr/series", response_model=EmrSeriesResponse)
def get_emr_series_endpoint(
    emr_key: str = Query(min_length=1, max_length=64),
    from_ts: datetime | None = Query(default=None, alias="from"),
    to_ts: datetime | None = Query(default=None, alias="to"),
    db: Session = Depends(get_db),
) -> EmrSeriesResponse:
    from_value = from_ts or (datetime.now(timezone.utc) - timedelta(hours=24))
    to_value = to_ts or datetime.now(timezone.utc)
    if from_value >= to_value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from' must be before 'to'",
        )

    rows = get_emr_series(db, emr_key=emr_key, from_ts=from_value, to_ts=to_value)
    return EmrSeriesResponse(
        emr_key=emr_key,
        points=[EmrPointResponse.model_validate(row) for row in rows],
    )
