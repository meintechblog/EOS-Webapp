from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import InputChannel, InputMapping
from app.schemas.mappings import MappingCreate, MappingUpdate


@dataclass(frozen=True)
class EnabledMappingSnapshot:
    id: int
    eos_field: str
    channel_id: int
    channel_code: str
    channel_type: str
    mqtt_topic: str
    payload_path: str | None
    timestamp_path: str | None
    unit: str | None
    value_multiplier: float
    sign_convention: str


@dataclass(frozen=True)
class MappingWithChannelSnapshot:
    mapping: InputMapping
    channel: InputChannel | None


def list_mappings(db: Session) -> list[InputMapping]:
    return list(db.scalars(select(InputMapping).order_by(InputMapping.eos_field)))


def list_mappings_with_channels(db: Session) -> list[MappingWithChannelSnapshot]:
    rows = db.execute(
        select(InputMapping, InputChannel)
        .outerjoin(InputChannel, InputChannel.id == InputMapping.channel_id)
        .order_by(InputMapping.eos_field)
    ).all()
    return [MappingWithChannelSnapshot(mapping=row[0], channel=row[1]) for row in rows]


def get_mapping_by_id(db: Session, mapping_id: int) -> InputMapping | None:
    return db.get(InputMapping, mapping_id)


def get_mapping_by_eos_field(db: Session, eos_field: str) -> InputMapping | None:
    return db.scalars(select(InputMapping).where(InputMapping.eos_field == eos_field)).first()


def get_mapping_by_topic(db: Session, mqtt_topic: str) -> InputMapping | None:
    return db.scalars(select(InputMapping).where(InputMapping.mqtt_topic == mqtt_topic)).first()


def get_mapping_by_channel_input_key(
    db: Session,
    *,
    channel_id: int,
    input_key: str,
) -> InputMapping | None:
    return db.scalars(
        select(InputMapping).where(
            InputMapping.channel_id == channel_id,
            InputMapping.mqtt_topic == input_key,
        )
    ).first()


def create_mapping(db: Session, payload: MappingCreate) -> InputMapping:
    mqtt_topic = payload.input_key if payload.input_key is not None else payload.mqtt_topic
    mapping = InputMapping(
        eos_field=payload.eos_field,
        channel_id=payload.channel_id,
        mqtt_topic=mqtt_topic,
        fixed_value=payload.fixed_value,
        payload_path=payload.payload_path,
        timestamp_path=payload.timestamp_path,
        unit=payload.unit,
        value_multiplier=payload.value_multiplier,
        sign_convention=payload.sign_convention,
        enabled=payload.enabled,
    )
    _validate_mapping_source(mapping)
    if mapping.fixed_value is not None:
        mapping.payload_path = None
        mapping.timestamp_path = None
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping


def update_mapping(db: Session, mapping: InputMapping, payload: MappingUpdate) -> InputMapping:
    updates = payload.model_dump(exclude_unset=True)

    if "input_key" in updates and "mqtt_topic" not in updates:
        updates["mqtt_topic"] = updates.pop("input_key")
    elif "input_key" in updates and "mqtt_topic" in updates:
        updates.pop("input_key")

    for key, value in updates.items():
        setattr(mapping, key, value)

    _validate_mapping_source(mapping)
    if mapping.fixed_value is not None:
        mapping.payload_path = None
        mapping.timestamp_path = None

    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping


def delete_mapping(db: Session, mapping: InputMapping) -> None:
    db.delete(mapping)
    db.commit()


def list_enabled_mapping_snapshots(
    db: Session,
    *,
    channel_type: str | None = None,
    channel_id: int | None = None,
) -> list[EnabledMappingSnapshot]:
    statement = (
        select(InputMapping, InputChannel)
        .join(InputChannel, InputChannel.id == InputMapping.channel_id)
        .where(
            InputMapping.enabled.is_(True),
            InputMapping.mqtt_topic.is_not(None),
            InputMapping.channel_id.is_not(None),
            InputChannel.enabled.is_(True),
        )
    )
    if channel_type is not None:
        statement = statement.where(InputChannel.channel_type == channel_type)
    if channel_id is not None:
        statement = statement.where(InputChannel.id == channel_id)

    rows = db.execute(statement).all()
    snapshots: list[EnabledMappingSnapshot] = []
    for mapping, channel in rows:
        if mapping.channel_id is None or mapping.mqtt_topic is None:
            continue
        snapshots.append(
            EnabledMappingSnapshot(
                id=mapping.id,
                eos_field=mapping.eos_field,
                channel_id=mapping.channel_id,
                channel_code=channel.code,
                channel_type=channel.channel_type,
                mqtt_topic=mapping.mqtt_topic,
                payload_path=mapping.payload_path,
                timestamp_path=mapping.timestamp_path,
                unit=mapping.unit,
                value_multiplier=mapping.value_multiplier,
                sign_convention=mapping.sign_convention,
            )
        )
    return snapshots


def _validate_mapping_source(mapping: InputMapping) -> None:
    has_topic = mapping.mqtt_topic is not None
    has_fixed = mapping.fixed_value is not None
    has_channel = mapping.channel_id is not None

    if has_fixed:
        if has_topic or has_channel:
            raise ValueError("fixed_value mappings cannot define channel/input key")
        return

    if not has_topic:
        raise ValueError("Channel mappings require input_key or mqtt_topic")
    if not has_channel:
        raise ValueError("Channel mappings require channel_id")
