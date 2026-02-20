from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies import get_input_ingest_service
from app.repositories.input_channels import (
    get_default_input_channel,
    get_input_channel_by_code,
)
from app.schemas.input_ingest import HttpInputIngestResponse, HttpInputPushRequest
from app.services.input_ingest import InputIngestPipelineService


router = APIRouter(tags=["input-ingest"])


@router.get("/eos/input/{channel_or_path:path}", response_model=HttpInputIngestResponse, status_code=status.HTTP_202_ACCEPTED)
def ingest_http_get(
    channel_or_path: str,
    request: Request,
    value: str | None = Query(default=None),
    ts: str | None = Query(default=None),
    timestamp: str | None = Query(default=None),
    db: Session = Depends(get_db),
    ingest_service: InputIngestPipelineService = Depends(get_input_ingest_service),
) -> HttpInputIngestResponse:
    channel, key_path = _resolve_channel_and_key_path(db, channel_or_path)
    key, payload_text = _extract_key_value_from_path(key_path=key_path, query_value=value)

    explicit_ts = _coerce_datetime(ts if ts is not None else timestamp)
    result = ingest_service.ingest(
        channel=channel,
        input_key=key,
        payload_text=payload_text,
        event_received_ts=datetime.now(timezone.utc),
        metadata={
            "source": "http",
            "method": "GET",
            "remote_addr": request.client.host if request.client else None,
            "path": request.url.path,
        },
        explicit_timestamp=explicit_ts,
    )
    return HttpInputIngestResponse(
        accepted=result.accepted,
        channel_code=result.channel_code,
        channel_type=result.channel_type,
        input_key=result.input_key,
        normalized_key=result.normalized_key,
        mapping_matched=result.mapping_matched,
        mapping_id=result.mapping_id,
        event_ts=result.event_ts,
    )


@router.post("/api/input/http/push", response_model=HttpInputIngestResponse, status_code=status.HTTP_202_ACCEPTED)
def ingest_http_post(
    payload: HttpInputPushRequest,
    request: Request,
    db: Session = Depends(get_db),
    ingest_service: InputIngestPipelineService = Depends(get_input_ingest_service),
) -> HttpInputIngestResponse:
    channel = _resolve_http_channel(db, payload.channel_code)

    payload_text: str
    if payload.payload is not None:
        payload_text = json.dumps(payload.payload, separators=(",", ":"), ensure_ascii=True)
    elif payload.value is not None:
        if isinstance(payload.value, str):
            payload_text = payload.value
        else:
            payload_text = json.dumps(payload.value, separators=(",", ":"), ensure_ascii=True)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either payload or value",
        )

    explicit_raw_ts = payload.ts if payload.ts is not None else payload.timestamp
    explicit_ts = _coerce_datetime(explicit_raw_ts)

    result = ingest_service.ingest(
        channel=channel,
        input_key=payload.input_key,
        payload_text=payload_text,
        event_received_ts=datetime.now(timezone.utc),
        metadata={
            "source": "http",
            "method": "POST",
            "remote_addr": request.client.host if request.client else None,
        },
        explicit_timestamp=explicit_ts,
    )
    return HttpInputIngestResponse(
        accepted=result.accepted,
        channel_code=result.channel_code,
        channel_type=result.channel_type,
        input_key=result.input_key,
        normalized_key=result.normalized_key,
        mapping_matched=result.mapping_matched,
        mapping_id=result.mapping_id,
        event_ts=result.event_ts,
    )


def _resolve_channel_and_key_path(db: Session, channel_or_path: str):
    path_value = channel_or_path.strip("/")
    if path_value == "":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="input key is required")

    parts = path_value.split("/")
    if len(parts) >= 2:
        candidate_code = parts[0]
        candidate_channel = get_input_channel_by_code(db, candidate_code)
        if candidate_channel is not None and candidate_channel.channel_type == "http":
            if not candidate_channel.enabled:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"HTTP channel '{candidate_code}' is disabled",
                )
            key_path = "/".join(parts[1:])
            return candidate_channel, key_path

    return _resolve_http_channel(db, None), path_value


def _resolve_http_channel(db: Session, channel_code: str | None):
    if channel_code is not None:
        channel = get_input_channel_by_code(db, channel_code)
        if channel is None or channel.channel_type != "http":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"HTTP channel '{channel_code}' not found",
            )
        if not channel.enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"HTTP channel '{channel_code}' is disabled",
            )
        return channel

    default_channel = get_default_input_channel(db, channel_type="http")
    if default_channel is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No default HTTP input channel configured",
        )
    if not default_channel.enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Default HTTP input channel is disabled",
        )
    return default_channel


def _extract_key_value_from_path(*, key_path: str, query_value: str | None) -> tuple[str, str]:
    if "=" in key_path:
        key, value = key_path.split("=", 1)
        key_trimmed = key.strip()
        if key_trimmed == "":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="input key is empty")
        return key_trimmed, value

    if query_value is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="value query parameter is required when path does not contain '=value'",
        )

    key_trimmed = key_path.strip()
    if key_trimmed == "":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="input key is empty")
    return key_trimmed, query_value


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
