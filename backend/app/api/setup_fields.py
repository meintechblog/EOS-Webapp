from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies import get_setup_field_service
from app.schemas.setup_fields import (
    SetupExportResponse,
    SetupFieldPatchRequest,
    SetupFieldPatchResponse,
    SetupFieldResponse,
    SetupImportRequest,
    SetupImportResponse,
    SetupReadinessResponse,
    SetupSetRequest,
    SetupSetResponse,
)
from app.services.setup_fields import SetupFieldService


router = APIRouter(tags=["setup-fields"])


@router.get("/api/setup/fields", response_model=list[SetupFieldResponse])
def get_setup_fields(
    db: Session = Depends(get_db),
    setup_service: SetupFieldService = Depends(get_setup_field_service),
) -> list[SetupFieldResponse]:
    return setup_service.list_fields(db)


@router.patch("/api/setup/fields", response_model=SetupFieldPatchResponse)
def patch_setup_fields(
    payload: SetupFieldPatchRequest,
    db: Session = Depends(get_db),
    setup_service: SetupFieldService = Depends(get_setup_field_service),
) -> SetupFieldPatchResponse:
    results = setup_service.patch_fields(db, updates=payload.updates)
    return SetupFieldPatchResponse(results=results)


@router.get("/api/setup/readiness", response_model=SetupReadinessResponse)
def get_setup_readiness(
    db: Session = Depends(get_db),
    setup_service: SetupFieldService = Depends(get_setup_field_service),
) -> SetupReadinessResponse:
    return setup_service.readiness(db)


@router.get("/api/setup/export", response_model=SetupExportResponse)
def get_setup_export(
    db: Session = Depends(get_db),
    setup_service: SetupFieldService = Depends(get_setup_field_service),
) -> SetupExportResponse:
    return setup_service.export_package(db)


@router.post("/api/setup/import", response_model=SetupImportResponse)
def post_setup_import(
    payload: SetupImportRequest,
    db: Session = Depends(get_db),
    setup_service: SetupFieldService = Depends(get_setup_field_service),
) -> SetupImportResponse:
    try:
        return setup_service.import_package(db, package_json=payload.package_json)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/api/setup/set", response_model=SetupSetResponse, status_code=status.HTTP_202_ACCEPTED)
def post_setup_set(
    payload: SetupSetRequest,
    db: Session = Depends(get_db),
    setup_service: SetupFieldService = Depends(get_setup_field_service),
) -> SetupSetResponse:
    raw_path, extracted_value = _extract_path_and_value(path=payload.path, query_value=None)
    value_to_write = extracted_value if extracted_value is not None else payload.value
    explicit_ts = _coerce_datetime(payload.ts if payload.ts is not None else payload.timestamp)
    try:
        result = setup_service.parse_set_path(
            db,
            raw_path=raw_path,
            value=value_to_write,
            source=payload.source,
            explicit_ts=explicit_ts,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return SetupSetResponse(
        accepted=result.status == "saved",
        field_id=result.field_id,
        status=result.status,
        error=result.error,
        field=result.field,
    )


@router.get("/eos/set/{path:path}", response_model=SetupSetResponse, status_code=status.HTTP_202_ACCEPTED)
def eos_set_get(
    path: str,
    value: str | None = Query(default=None),
    ts: str | int | float | None = Query(default=None),
    timestamp: str | int | float | None = Query(default=None),
    db: Session = Depends(get_db),
    setup_service: SetupFieldService = Depends(get_setup_field_service),
) -> SetupSetResponse:
    raw_path, extracted_value = _extract_path_and_value(path=path, query_value=value)
    if extracted_value is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="value query parameter is required when path does not contain '=value'",
        )
    explicit_ts = _coerce_datetime(ts if ts is not None else timestamp)
    try:
        result = setup_service.parse_set_path(
            db,
            raw_path=raw_path,
            value=extracted_value,
            source="http",
            explicit_ts=explicit_ts,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return SetupSetResponse(
        accepted=result.status == "saved",
        field_id=result.field_id,
        status=result.status,
        error=result.error,
        field=result.field,
    )


@router.get("/eos/input/{legacy_path:path}", response_model=SetupSetResponse, status_code=status.HTTP_202_ACCEPTED)
def eos_input_legacy_get(
    legacy_path: str,
    value: str | None = Query(default=None),
    ts: str | int | float | None = Query(default=None),
    timestamp: str | int | float | None = Query(default=None),
    db: Session = Depends(get_db),
    setup_service: SetupFieldService = Depends(get_setup_field_service),
) -> SetupSetResponse:
    mapped = legacy_path.strip("/")
    if mapped.startswith("eos/input/"):
        mapped = mapped[len("eos/input/") :]
    normalized_path = f"signal/{mapped}"
    return _run_set_get(
        db=db,
        setup_service=setup_service,
        path=normalized_path,
        query_value=value,
        ts=ts,
        timestamp=timestamp,
    )


@router.get("/eos/param/{legacy_path:path}", response_model=SetupSetResponse, status_code=status.HTTP_202_ACCEPTED)
def eos_param_legacy_get(
    legacy_path: str,
    value: str | None = Query(default=None),
    ts: str | int | float | None = Query(default=None),
    timestamp: str | int | float | None = Query(default=None),
    db: Session = Depends(get_db),
    setup_service: SetupFieldService = Depends(get_setup_field_service),
) -> SetupSetResponse:
    mapped = legacy_path.strip("/")
    if mapped.startswith("param/"):
        mapped = mapped[len("param/") :]
    normalized_path = f"param/{mapped}"
    return _run_set_get(
        db=db,
        setup_service=setup_service,
        path=normalized_path,
        query_value=value,
        ts=ts,
        timestamp=timestamp,
    )


def _run_set_get(
    *,
    db: Session,
    setup_service: SetupFieldService,
    path: str,
    query_value: str | None,
    ts: str | int | float | None,
    timestamp: str | int | float | None,
) -> SetupSetResponse:
    raw_path, extracted_value = _extract_path_and_value(path=path, query_value=query_value)
    if extracted_value is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="value query parameter is required when path does not contain '=value'",
        )
    explicit_ts = _coerce_datetime(ts if ts is not None else timestamp)
    try:
        result = setup_service.parse_set_path(
            db,
            raw_path=raw_path,
            value=extracted_value,
            source="http",
            explicit_ts=explicit_ts,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return SetupSetResponse(
        accepted=result.status == "saved",
        field_id=result.field_id,
        status=result.status,
        error=result.error,
        field=result.field,
    )


def _extract_path_and_value(*, path: str, query_value: Any) -> tuple[str, Any]:
    value_path = path.strip("/")
    if value_path == "":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is required")
    if "=" in value_path:
        raw_path, raw_value = value_path.split("=", 1)
        if raw_path.strip() == "":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is required")
        return raw_path.strip(), raw_value
    if query_value is None:
        return value_path, None
    return value_path, query_value


def _coerce_datetime(value: str | int | float | None) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _epoch_to_datetime(float(value))

    if isinstance(value, str):
        raw = value.strip()
        if raw == "":
            return None
        try:
            numeric = float(raw)
        except ValueError:
            numeric = None
        if numeric is not None:
            return _epoch_to_datetime(numeric)

        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid timestamp value: {value}",
            ) from exc
        return _to_utc(parsed)

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid timestamp type")


def _epoch_to_datetime(value: float) -> datetime:
    try:
        seconds = value / 1000.0 if abs(value) > 1_000_000_000_000 else value
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid epoch timestamp: {value}",
        ) from exc
    return _to_utc(parsed)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
