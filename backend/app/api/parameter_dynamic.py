from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies import (
    get_mqtt_service,
    get_parameter_dynamic_catalog_service,
    get_parameter_dynamic_service,
    get_setup_checklist_service,
)
from app.repositories.input_channels import (
    get_default_input_channel,
    get_input_channel_by_code,
    get_input_channel_by_id,
)
from app.repositories.parameter_bindings import (
    create_parameter_binding,
    delete_parameter_binding,
    get_parameter_binding_by_id,
    get_parameter_binding_with_channel_by_id,
    list_parameter_bindings,
    list_parameter_input_events,
    update_parameter_binding,
)
from app.schemas.parameter_dynamic import (
    DynamicCatalogResponse,
    HttpParamIngestResponse,
    HttpParamPushRequest,
    ParameterBindingCreateRequest,
    ParameterBindingEventResponse,
    ParameterBindingResponse,
    ParameterBindingUpdateRequest,
)
from app.schemas.setup import SetupChecklistResponse
from app.services.mqtt_ingest import MqttIngestService
from app.services.parameter_dynamic_catalog import ParameterDynamicCatalogService
from app.services.parameter_dynamic_ingest import ParameterDynamicIngestService
from app.services.setup_checklist import SetupChecklistService


router = APIRouter(tags=["parameter-dynamic"])


@router.get("/api/parameters/dynamic-catalog", response_model=DynamicCatalogResponse)
def get_dynamic_catalog(
    catalog_service: ParameterDynamicCatalogService = Depends(get_parameter_dynamic_catalog_service),
) -> DynamicCatalogResponse:
    return DynamicCatalogResponse.model_validate(catalog_service.build_catalog())


@router.get("/api/parameter-bindings", response_model=list[ParameterBindingResponse])
def get_parameter_bindings(db: Session = Depends(get_db)) -> list[ParameterBindingResponse]:
    rows = list_parameter_bindings(db)
    return [_binding_response(row.binding, row.channel.code, row.channel.channel_type) for row in rows]


@router.post("/api/parameter-bindings", response_model=ParameterBindingResponse, status_code=status.HTTP_201_CREATED)
def create_parameter_binding_endpoint(
    payload: ParameterBindingCreateRequest,
    db: Session = Depends(get_db),
    dynamic_service: ParameterDynamicIngestService = Depends(get_parameter_dynamic_service),
    mqtt_service: MqttIngestService = Depends(get_mqtt_service),
) -> ParameterBindingResponse:
    channel = get_input_channel_by_id(db, payload.channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Input channel not found")

    normalized_key = dynamic_service.normalize_key(payload.input_key)
    try:
        binding = create_parameter_binding(
            db,
            parameter_key=payload.parameter_key,
            selector_value=payload.selector_value,
            channel_id=payload.channel_id,
            input_key=normalized_key,
            payload_path=payload.payload_path,
            timestamp_path=payload.timestamp_path,
            incoming_unit=payload.incoming_unit,
            value_multiplier=payload.value_multiplier,
            enabled=payload.enabled,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Binding conflict: {exc.orig}")

    mqtt_service.sync_subscriptions_from_db()
    return _binding_response(binding, channel.code, channel.channel_type)


@router.put("/api/parameter-bindings/{binding_id}", response_model=ParameterBindingResponse)
def update_parameter_binding_endpoint(
    binding_id: int,
    payload: ParameterBindingUpdateRequest,
    db: Session = Depends(get_db),
    dynamic_service: ParameterDynamicIngestService = Depends(get_parameter_dynamic_service),
    mqtt_service: MqttIngestService = Depends(get_mqtt_service),
) -> ParameterBindingResponse:
    binding = get_parameter_binding_by_id(db, binding_id)
    if binding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter binding not found")

    updates = payload.model_dump(exclude_unset=True)
    next_channel_id = updates.get("channel_id", binding.channel_id)
    channel = get_input_channel_by_id(db, next_channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Input channel not found")

    input_key = updates.get("input_key")
    normalized_input_key = dynamic_service.normalize_key(input_key) if isinstance(input_key, str) else None
    try:
        updated = update_parameter_binding(
            db,
            binding,
            parameter_key=updates.get("parameter_key"),
            selector_value=updates.get("selector_value", ...),
            channel_id=updates.get("channel_id"),
            input_key=normalized_input_key,
            payload_path=updates.get("payload_path", ...),
            timestamp_path=updates.get("timestamp_path", ...),
            incoming_unit=updates.get("incoming_unit", ...),
            value_multiplier=updates.get("value_multiplier"),
            enabled=updates.get("enabled"),
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Binding conflict: {exc.orig}")

    mqtt_service.sync_subscriptions_from_db()
    snapshot = get_parameter_binding_with_channel_by_id(db, updated.id)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter binding not found after update")
    return _binding_response(snapshot.binding, snapshot.channel.code, snapshot.channel.channel_type)


@router.delete("/api/parameter-bindings/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_parameter_binding_endpoint(
    binding_id: int,
    db: Session = Depends(get_db),
    mqtt_service: MqttIngestService = Depends(get_mqtt_service),
) -> Response:
    binding = get_parameter_binding_by_id(db, binding_id)
    if binding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter binding not found")
    delete_parameter_binding(db, binding)
    mqtt_service.sync_subscriptions_from_db()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/parameter-bindings/events", response_model=list[ParameterBindingEventResponse])
def get_parameter_binding_events(
    limit: int = Query(default=100, ge=1, le=1000),
    channel_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
) -> list[ParameterBindingEventResponse]:
    rows = list_parameter_input_events(db, limit=limit, channel_id=channel_id)
    items: list[ParameterBindingEventResponse] = []
    for row in rows:
        event = row.event
        items.append(
            ParameterBindingEventResponse(
                id=event.id,
                binding_id=event.binding_id,
                channel_id=event.channel_id,
                channel_code=row.channel.code,
                channel_type=row.channel.channel_type,  # type: ignore[arg-type]
                input_key=event.input_key,
                normalized_key=event.normalized_key,
                raw_payload=event.raw_payload,
                parsed_value_text=event.parsed_value_text,
                event_ts=event.event_ts,
                revision_id=event.revision_id,
                apply_status=event.apply_status,  # type: ignore[arg-type]
                error_text=event.error_text,
                meta_json=dict(event.meta_json or {}),
                created_at=event.created_at,
            )
        )
    return items


@router.get("/eos/param/{channel_or_path:path}", response_model=HttpParamIngestResponse, status_code=status.HTTP_202_ACCEPTED)
def ingest_param_http_get(
    channel_or_path: str,
    request: Request,
    value: str | None = Query(default=None),
    ts: str | None = Query(default=None),
    timestamp: str | None = Query(default=None),
    db: Session = Depends(get_db),
    dynamic_service: ParameterDynamicIngestService = Depends(get_parameter_dynamic_service),
) -> HttpParamIngestResponse:
    channel, key_path = _resolve_channel_and_key_path(db, channel_or_path)
    key, payload_text = _extract_key_value_from_path(key_path=key_path, query_value=value)
    explicit_ts = _coerce_datetime(ts if ts is not None else timestamp)

    result = dynamic_service.ingest(
        channel=channel,
        input_key=key,
        payload_text=payload_text,
        event_received_ts=datetime.now(timezone.utc),
        metadata={
            "source": "http",
            "method": "GET",
            "path": request.url.path,
            "remote_addr": request.client.host if request.client else None,
        },
        explicit_timestamp=explicit_ts,
    )
    return HttpParamIngestResponse(
        accepted=result.accepted,
        channel_code=result.channel_code,
        channel_type=result.channel_type,  # type: ignore[arg-type]
        input_key=result.input_key,
        normalized_key=result.normalized_key,
        binding_matched=result.binding_matched,
        binding_id=result.binding_id,
        event_id=result.event_id,
        event_ts=result.event_ts,
        apply_status=result.apply_status,  # type: ignore[arg-type]
        error_text=result.error_text,
    )


@router.post("/api/input/param/push", response_model=HttpParamIngestResponse, status_code=status.HTTP_202_ACCEPTED)
def ingest_param_http_post(
    payload: HttpParamPushRequest,
    request: Request,
    db: Session = Depends(get_db),
    dynamic_service: ParameterDynamicIngestService = Depends(get_parameter_dynamic_service),
) -> HttpParamIngestResponse:
    channel = _resolve_http_channel(db, payload.channel_code)
    if payload.payload is not None:
        payload_text = json.dumps(payload.payload, separators=(",", ":"), ensure_ascii=True)
    elif payload.value is not None:
        payload_text = payload.value if isinstance(payload.value, str) else json.dumps(payload.value, separators=(",", ":"), ensure_ascii=True)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Provide either payload or value")

    explicit_raw_ts = payload.ts if payload.ts is not None else payload.timestamp
    explicit_ts = _coerce_datetime(explicit_raw_ts)
    result = dynamic_service.ingest(
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
    return HttpParamIngestResponse(
        accepted=result.accepted,
        channel_code=result.channel_code,
        channel_type=result.channel_type,  # type: ignore[arg-type]
        input_key=result.input_key,
        normalized_key=result.normalized_key,
        binding_matched=result.binding_matched,
        binding_id=result.binding_id,
        event_id=result.event_id,
        event_ts=result.event_ts,
        apply_status=result.apply_status,  # type: ignore[arg-type]
        error_text=result.error_text,
    )


@router.get("/api/setup/checklist", response_model=SetupChecklistResponse)
def get_setup_checklist(
    db: Session = Depends(get_db),
    setup_service: SetupChecklistService = Depends(get_setup_checklist_service),
) -> SetupChecklistResponse:
    return SetupChecklistResponse.model_validate(setup_service.build_checklist(db))


def _binding_response(binding, channel_code: str, channel_type: str) -> ParameterBindingResponse:
    return ParameterBindingResponse(
        id=binding.id,
        parameter_key=binding.parameter_key,
        selector_value=binding.selector_value,
        channel_id=binding.channel_id,
        channel_code=channel_code,
        channel_type=channel_type,  # type: ignore[arg-type]
        input_key=binding.input_key,
        payload_path=binding.payload_path,
        timestamp_path=binding.timestamp_path,
        incoming_unit=binding.incoming_unit,
        value_multiplier=binding.value_multiplier,
        enabled=binding.enabled,
        created_at=binding.created_at,
        updated_at=binding.updated_at,
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
            return candidate_channel, "/".join(parts[1:])
    return _resolve_http_channel(db, None), path_value


def _resolve_http_channel(db: Session, channel_code: str | None):
    if channel_code is not None:
        channel = get_input_channel_by_code(db, channel_code)
        if channel is None or channel.channel_type != "http":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"HTTP channel '{channel_code}' not found")
        if not channel.enabled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"HTTP channel '{channel_code}' is disabled")
        return channel

    default_channel = get_default_input_channel(db, channel_type="http")
    if default_channel is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No default HTTP input channel configured")
    if not default_channel.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Default HTTP input channel is disabled")
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
