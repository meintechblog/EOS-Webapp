from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import InputMapping
from app.db.session import get_db
from app.dependencies import get_automap_service, get_mqtt_service, get_settings_from_app
from app.repositories.input_channels import (
    ensure_default_channel_exists,
    get_input_channel_by_id,
)
from app.repositories.mappings import (
    create_mapping,
    delete_mapping,
    get_mapping_by_id,
    list_mappings,
    update_mapping,
)
from app.repositories.signal_backbone import infer_value_type, ingest_signal_measurement
from app.schemas.discovery import AutomapResult, DiscoveredInputItem, DiscoveredTopicItem
from app.schemas.mappings import MappingCreate, MappingResponse, MappingUpdate
from app.services.automap import AutomapService
from app.services.mqtt_ingest import MqttIngestService


router = APIRouter(prefix="/api", tags=["mappings"])
logger = logging.getLogger("app.mappings_api")


def _raise_conflict(exc: IntegrityError) -> None:
    detail = "Mapping violates unique constraints"
    message = str(getattr(exc, "orig", exc)).lower()
    if "eos_field" in message:
        detail = "Mapping with this eos_field already exists"
    elif "channel_topic" in message or "mqtt_topic" in message:
        detail = "Mapping with this input key already exists in the selected channel"

    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _raise_bad_request(exc: ValueError) -> None:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/mappings", response_model=list[MappingResponse])
def get_all_mappings(db: Session = Depends(get_db)) -> list[InputMapping]:
    return list_mappings(db)


@router.post("/mappings", response_model=MappingResponse, status_code=status.HTTP_201_CREATED)
def create_mapping_endpoint(
    payload: MappingCreate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_from_app),
    mqtt_service: MqttIngestService = Depends(get_mqtt_service),
) -> InputMapping:
    payload = _resolve_create_payload_defaults(db, payload, settings)

    try:
        mapping = create_mapping(db, payload)
    except ValueError as exc:
        db.rollback()
        _raise_bad_request(exc)
    except IntegrityError as exc:
        db.rollback()
        _raise_conflict(exc)

    mqtt_service.sync_subscriptions_from_db()
    if mapping.fixed_value is not None and mapping.enabled:
        try:
            _ingest_fixed_mapping_signal(db, mapping)
        except Exception:
            logger.exception("failed to ingest fixed mapping into signal backbone mapping_id=%s", mapping.id)
    return mapping


@router.put("/mappings/{mapping_id}", response_model=MappingResponse)
def update_mapping_endpoint(
    mapping_id: int,
    payload: MappingUpdate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_from_app),
    mqtt_service: MqttIngestService = Depends(get_mqtt_service),
) -> InputMapping:
    mapping = get_mapping_by_id(db, mapping_id)
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")

    payload = _resolve_update_payload_defaults(db, mapping, payload, settings)

    try:
        updated = update_mapping(db, mapping, payload)
    except ValueError as exc:
        db.rollback()
        _raise_bad_request(exc)
    except IntegrityError as exc:
        db.rollback()
        _raise_conflict(exc)

    mqtt_service.sync_subscriptions_from_db()
    if updated.fixed_value is not None and updated.enabled:
        try:
            _ingest_fixed_mapping_signal(db, updated)
        except Exception:
            logger.exception("failed to ingest fixed mapping into signal backbone mapping_id=%s", updated.id)
    return updated


@router.delete("/mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mapping_endpoint(
    mapping_id: int,
    db: Session = Depends(get_db),
    mqtt_service: MqttIngestService = Depends(get_mqtt_service),
) -> Response:
    mapping = get_mapping_by_id(db, mapping_id)
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")

    delete_mapping(db, mapping)
    mqtt_service.sync_subscriptions_from_db()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/discovered-inputs", response_model=list[DiscoveredInputItem])
def get_discovered_inputs_endpoint(
    namespace: str = Query(default="all"),
    channel_type: str = Query(default="all"),
    channel_id: int | None = Query(default=None, ge=1),
    active_only: bool = Query(default=True),
    active_seconds: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    automap_service: AutomapService = Depends(get_automap_service),
) -> list[DiscoveredInputItem]:
    if channel_type not in {"all", "mqtt", "http"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid channel_type")
    if namespace not in {"all", "input", "param"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid namespace")

    return automap_service.list_discovered_inputs(
        db,
        namespace=namespace,
        channel_type=None if channel_type == "all" else channel_type,
        channel_id=channel_id,
        active_only=active_only,
        active_seconds=active_seconds,
    )


@router.get("/discovered-topics", response_model=list[DiscoveredTopicItem])
def get_discovered_topics_endpoint(
    db: Session = Depends(get_db),
    automap_service: AutomapService = Depends(get_automap_service),
) -> list[DiscoveredTopicItem]:
    return automap_service.list_discovered_topics(db)


@router.post("/mappings/automap", response_model=AutomapResult)
def run_automap_endpoint(
    channel_type: str = Query(default="all"),
    channel_id: list[int] | None = Query(default=None),
    db: Session = Depends(get_db),
    automap_service: AutomapService = Depends(get_automap_service),
    mqtt_service: MqttIngestService = Depends(get_mqtt_service),
) -> AutomapResult:
    if channel_type not in {"all", "mqtt", "http"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid channel_type")

    result = automap_service.apply_automap(
        db,
        channel_ids=channel_id,
        channel_type=None if channel_type == "all" else channel_type,
    )
    mqtt_service.sync_subscriptions_from_db()
    return result


def _resolve_create_payload_defaults(
    db: Session,
    payload: MappingCreate,
    settings: Settings,
) -> MappingCreate:
    topic_value = payload.input_key or payload.mqtt_topic
    if payload.fixed_value is not None:
        return payload

    channel_id = payload.channel_id
    if channel_id is None and payload.mqtt_topic is not None:
        default_channel = ensure_default_channel_exists(
            db,
            channel_type="mqtt",
            fallback_code="mqtt-default",
            fallback_name="MQTT Default",
            fallback_config={
                "host": settings.mqtt_broker_host,
                "port": settings.mqtt_broker_port,
                "client_id": settings.mqtt_client_id,
                "qos": settings.mqtt_qos,
                "discovery_topic": settings.mqtt_discovery_topic,
            },
        )
        channel_id = default_channel.id

    if channel_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="channel_id is required for channel-based mappings",
        )

    channel = get_input_channel_by_id(db, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Input channel not found")

    if topic_value is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="input_key/mqtt_topic is required for channel-based mappings",
        )

    return payload.model_copy(
        update={
            "channel_id": channel_id,
            "input_key": topic_value,
            "mqtt_topic": topic_value,
        }
    )


def _resolve_update_payload_defaults(
    db: Session,
    mapping: InputMapping,
    payload: MappingUpdate,
    settings: Settings,
) -> MappingUpdate:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return payload

    if "input_key" in updates and "mqtt_topic" not in updates:
        updates["mqtt_topic"] = updates["input_key"]

    if updates.get("fixed_value") is not None:
        updates.setdefault("mqtt_topic", None)
        updates.setdefault("channel_id", None)

    updates_topic = updates.get("mqtt_topic")
    updates_channel_id = updates.get("channel_id", mapping.channel_id)

    if updates_topic is not None:
        if updates_channel_id is None:
            default_channel = ensure_default_channel_exists(
                db,
                channel_type="mqtt",
                fallback_code="mqtt-default",
                fallback_name="MQTT Default",
                fallback_config={
                    "host": settings.mqtt_broker_host,
                    "port": settings.mqtt_broker_port,
                    "client_id": settings.mqtt_client_id,
                    "qos": settings.mqtt_qos,
                    "discovery_topic": settings.mqtt_discovery_topic,
                },
            )
            updates_channel_id = default_channel.id
            updates["channel_id"] = updates_channel_id

    if updates_channel_id is not None:
        channel = get_input_channel_by_id(db, updates_channel_id)
        if channel is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Input channel not found")

    return payload.model_copy(update=updates)


def _ingest_fixed_mapping_signal(db: Session, mapping: InputMapping) -> None:
    ingest_signal_measurement(
        db,
        signal_key=mapping.eos_field,
        label=mapping.eos_field,
        value_type=infer_value_type(mapping.fixed_value),
        canonical_unit=_canonical_unit_for_field(mapping.eos_field, mapping.unit),
        value=mapping.fixed_value,
        ts=datetime.now(timezone.utc),
        quality_status="ok",
        source_type="fixed_input",
        run_id=None,
        mapping_id=mapping.id,
        source_ref_id=None,
        tags_json={
            "eos_field": mapping.eos_field,
            "source": "fixed",
        },
    )


def _canonical_unit_for_field(eos_field: str, unit: str | None) -> str | None:
    field = eos_field.strip().lower()
    if field.endswith("_w"):
        return "W"
    if field.endswith("_wh"):
        return "Wh"
    if field.endswith("_pct") or field.endswith("_percentage"):
        return "%"
    if "euro_pro_wh" in field:
        return "EUR/Wh"
    return unit
