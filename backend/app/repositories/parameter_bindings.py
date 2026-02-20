from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import InputChannel, ParameterBinding, ParameterInputEvent


@dataclass(frozen=True)
class BindingWithChannelSnapshot:
    binding: ParameterBinding
    channel: InputChannel


@dataclass(frozen=True)
class EnabledParameterBindingSnapshot:
    id: int
    parameter_key: str
    selector_value: str | None
    channel_id: int
    channel_code: str
    channel_type: str
    input_key: str
    payload_path: str | None
    timestamp_path: str | None
    incoming_unit: str | None
    value_multiplier: float


@dataclass(frozen=True)
class ParameterInputEventWithChannelSnapshot:
    event: ParameterInputEvent
    channel: InputChannel


def list_parameter_bindings(db: Session) -> list[BindingWithChannelSnapshot]:
    rows = db.execute(
        select(ParameterBinding, InputChannel)
        .join(InputChannel, InputChannel.id == ParameterBinding.channel_id)
        .order_by(ParameterBinding.parameter_key.asc(), InputChannel.code.asc())
    ).all()
    return [BindingWithChannelSnapshot(binding=row[0], channel=row[1]) for row in rows]


def get_parameter_binding_by_id(db: Session, binding_id: int) -> ParameterBinding | None:
    return db.get(ParameterBinding, binding_id)


def get_parameter_binding_with_channel_by_id(
    db: Session,
    binding_id: int,
) -> BindingWithChannelSnapshot | None:
    row = db.execute(
        select(ParameterBinding, InputChannel)
        .join(InputChannel, InputChannel.id == ParameterBinding.channel_id)
        .where(ParameterBinding.id == binding_id)
    ).first()
    if row is None:
        return None
    return BindingWithChannelSnapshot(binding=row[0], channel=row[1])


def get_parameter_binding_by_channel_input_key(
    db: Session,
    *,
    channel_id: int,
    input_key: str,
) -> ParameterBinding | None:
    return db.scalars(
        select(ParameterBinding).where(
            ParameterBinding.channel_id == channel_id,
            ParameterBinding.input_key == input_key,
        )
    ).first()


def create_parameter_binding(
    db: Session,
    *,
    parameter_key: str,
    selector_value: str | None,
    channel_id: int,
    input_key: str,
    payload_path: str | None,
    timestamp_path: str | None,
    incoming_unit: str | None,
    value_multiplier: float,
    enabled: bool,
) -> ParameterBinding:
    binding = ParameterBinding(
        parameter_key=parameter_key,
        selector_value=selector_value,
        channel_id=channel_id,
        input_key=input_key,
        payload_path=payload_path,
        timestamp_path=timestamp_path,
        incoming_unit=incoming_unit,
        value_multiplier=value_multiplier,
        enabled=enabled,
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)
    return binding


def update_parameter_binding(
    db: Session,
    binding: ParameterBinding,
    *,
    parameter_key: str | None = None,
    selector_value: str | None = ...,
    channel_id: int | None = None,
    input_key: str | None = None,
    payload_path: str | None = ...,
    timestamp_path: str | None = ...,
    incoming_unit: str | None = ...,
    value_multiplier: float | None = None,
    enabled: bool | None = None,
) -> ParameterBinding:
    if parameter_key is not None:
        binding.parameter_key = parameter_key
    if selector_value is not ...:
        binding.selector_value = selector_value
    if channel_id is not None:
        binding.channel_id = channel_id
    if input_key is not None:
        binding.input_key = input_key
    if payload_path is not ...:
        binding.payload_path = payload_path
    if timestamp_path is not ...:
        binding.timestamp_path = timestamp_path
    if incoming_unit is not ...:
        binding.incoming_unit = incoming_unit
    if value_multiplier is not None:
        binding.value_multiplier = value_multiplier
    if enabled is not None:
        binding.enabled = enabled

    binding.updated_at = datetime.now(timezone.utc)
    db.add(binding)
    db.commit()
    db.refresh(binding)
    return binding


def delete_parameter_binding(db: Session, binding: ParameterBinding) -> None:
    db.delete(binding)
    db.commit()


def count_parameter_bindings_for_channel(db: Session, channel_id: int) -> int:
    count = db.scalar(
        select(func.count(ParameterBinding.id)).where(ParameterBinding.channel_id == channel_id)
    )
    return int(count or 0)


def list_enabled_parameter_binding_snapshots(
    db: Session,
    *,
    channel_type: str | None = None,
    channel_id: int | None = None,
) -> list[EnabledParameterBindingSnapshot]:
    statement = (
        select(ParameterBinding, InputChannel)
        .join(InputChannel, InputChannel.id == ParameterBinding.channel_id)
        .where(
            ParameterBinding.enabled.is_(True),
            InputChannel.enabled.is_(True),
        )
    )
    if channel_type is not None:
        statement = statement.where(InputChannel.channel_type == channel_type)
    if channel_id is not None:
        statement = statement.where(InputChannel.id == channel_id)

    rows = db.execute(statement).all()
    return [
        EnabledParameterBindingSnapshot(
            id=binding.id,
            parameter_key=binding.parameter_key,
            selector_value=binding.selector_value,
            channel_id=channel.id,
            channel_code=channel.code,
            channel_type=channel.channel_type,
            input_key=binding.input_key,
            payload_path=binding.payload_path,
            timestamp_path=binding.timestamp_path,
            incoming_unit=binding.incoming_unit,
            value_multiplier=binding.value_multiplier,
        )
        for binding, channel in rows
    ]


def create_parameter_input_event(
    db: Session,
    *,
    binding_id: int | None,
    channel_id: int,
    input_key: str,
    normalized_key: str,
    raw_payload: str,
    parsed_value_text: str | None,
    event_ts: datetime,
    revision_id: int | None,
    apply_status: str,
    error_text: str | None,
    meta_json: dict[str, Any] | None = None,
) -> ParameterInputEvent:
    event = ParameterInputEvent(
        binding_id=binding_id,
        channel_id=channel_id,
        input_key=input_key,
        normalized_key=normalized_key,
        raw_payload=raw_payload,
        parsed_value_text=parsed_value_text,
        event_ts=event_ts,
        revision_id=revision_id,
        apply_status=apply_status,
        error_text=error_text,
        meta_json=meta_json or {},
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def update_parameter_input_event_status(
    db: Session,
    *,
    event_id: int,
    apply_status: str,
    error_text: str | None = None,
) -> ParameterInputEvent | None:
    event = db.get(ParameterInputEvent, event_id)
    if event is None:
        return None
    event.apply_status = apply_status
    event.error_text = error_text
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def bulk_update_parameter_input_event_status(
    db: Session,
    *,
    event_ids: list[int],
    apply_status: str,
    error_text: str | None = None,
) -> int:
    if not event_ids:
        return 0
    affected = (
        db.query(ParameterInputEvent)
        .filter(ParameterInputEvent.id.in_(event_ids))
        .update(
            {
                ParameterInputEvent.apply_status: apply_status,
                ParameterInputEvent.error_text: error_text,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return int(affected)


def list_parameter_input_events(
    db: Session,
    *,
    limit: int = 100,
    channel_id: int | None = None,
) -> list[ParameterInputEventWithChannelSnapshot]:
    statement = (
        select(ParameterInputEvent, InputChannel)
        .join(InputChannel, InputChannel.id == ParameterInputEvent.channel_id)
        .order_by(ParameterInputEvent.created_at.desc())
        .limit(max(1, min(limit, 1000)))
    )
    if channel_id is not None:
        statement = statement.where(ParameterInputEvent.channel_id == channel_id)
    rows = db.execute(statement).all()
    return [ParameterInputEventWithChannelSnapshot(event=row[0], channel=row[1]) for row in rows]
