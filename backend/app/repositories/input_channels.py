from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import InputChannel, InputMapping, ParameterBinding


@dataclass(frozen=True)
class ChannelMappingUsage:
    channel_id: int
    mapping_count: int


@dataclass(frozen=True)
class ChannelDeleteCheck:
    blocked: bool
    reason: str | None
    mapping_count: int
    binding_count: int


def list_input_channels(db: Session, *, channel_type: str | None = None) -> list[InputChannel]:
    statement = select(InputChannel)
    if channel_type is not None:
        statement = statement.where(InputChannel.channel_type == channel_type)
    statement = statement.order_by(InputChannel.channel_type.asc(), InputChannel.code.asc())
    return list(db.scalars(statement))


def get_input_channel_by_id(db: Session, channel_id: int) -> InputChannel | None:
    return db.get(InputChannel, channel_id)


def get_input_channel_by_code(db: Session, code: str) -> InputChannel | None:
    return db.scalars(select(InputChannel).where(InputChannel.code == code)).first()


def get_default_input_channel(db: Session, *, channel_type: str) -> InputChannel | None:
    return db.scalars(
        select(InputChannel).where(
            InputChannel.channel_type == channel_type,
            InputChannel.is_default.is_(True),
        )
    ).first()


def create_input_channel(
    db: Session,
    *,
    code: str,
    name: str,
    channel_type: str,
    enabled: bool,
    is_default: bool,
    config_json: dict[str, Any],
) -> InputChannel:
    channel = InputChannel(
        code=code,
        name=name,
        channel_type=channel_type,
        enabled=enabled,
        is_default=is_default,
        config_json=config_json,
    )
    db.add(channel)
    db.flush()
    _ensure_default_constraints(db, channel_type=channel.channel_type, preferred_default_id=channel.id)
    db.commit()
    db.refresh(channel)
    return channel


def update_input_channel(
    db: Session,
    channel: InputChannel,
    *,
    code: str | None = None,
    name: str | None = None,
    enabled: bool | None = None,
    is_default: bool | None = None,
    config_json: dict[str, Any] | None = None,
) -> InputChannel:
    if code is not None:
        channel.code = code
    if name is not None:
        channel.name = name
    if enabled is not None:
        channel.enabled = enabled
    if is_default is not None:
        channel.is_default = is_default
    if config_json is not None:
        channel.config_json = config_json

    db.add(channel)
    db.flush()
    _ensure_default_constraints(db, channel_type=channel.channel_type, preferred_default_id=channel.id)
    db.commit()
    db.refresh(channel)
    return channel


def check_input_channel_delete(db: Session, channel: InputChannel) -> ChannelDeleteCheck:
    mapping_count = (
        db.scalar(
            select(func.count(InputMapping.id)).where(InputMapping.channel_id == channel.id)
        )
        or 0
    )
    binding_count = (
        db.scalar(
            select(func.count(ParameterBinding.id)).where(ParameterBinding.channel_id == channel.id)
        )
        or 0
    )
    if mapping_count > 0 or binding_count > 0:
        return ChannelDeleteCheck(
            blocked=True,
            reason="channel_in_use",
            mapping_count=int(mapping_count),
            binding_count=int(binding_count),
        )

    same_type_count = (
        db.scalar(
            select(func.count(InputChannel.id)).where(InputChannel.channel_type == channel.channel_type)
        )
        or 0
    )
    if channel.is_default and same_type_count <= 1:
        return ChannelDeleteCheck(
            blocked=True,
            reason="last_default_channel_of_type",
            mapping_count=0,
            binding_count=0,
        )

    return ChannelDeleteCheck(blocked=False, reason=None, mapping_count=0, binding_count=0)


def delete_input_channel(db: Session, channel: InputChannel) -> None:
    channel_type = channel.channel_type
    was_default = bool(channel.is_default)
    channel_id = channel.id
    db.delete(channel)
    db.flush()

    if was_default:
        replacement = db.scalars(
            select(InputChannel)
            .where(InputChannel.channel_type == channel_type, InputChannel.id != channel_id)
            .order_by(InputChannel.enabled.desc(), InputChannel.id.asc())
            .limit(1)
        ).first()
        if replacement is not None:
            replacement.is_default = True
            db.add(replacement)

    db.commit()


def ensure_default_channel_exists(
    db: Session,
    *,
    channel_type: str,
    fallback_code: str,
    fallback_name: str,
    fallback_config: dict[str, Any],
) -> InputChannel:
    default_channel = get_default_input_channel(db, channel_type=channel_type)
    if default_channel is not None:
        return default_channel

    existing = db.scalars(
        select(InputChannel)
        .where(InputChannel.channel_type == channel_type)
        .order_by(InputChannel.enabled.desc(), InputChannel.id.asc())
        .limit(1)
    ).first()
    if existing is not None:
        existing.is_default = True
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing

    created = InputChannel(
        code=fallback_code,
        name=fallback_name,
        channel_type=channel_type,
        enabled=True,
        is_default=True,
        config_json=fallback_config,
    )
    db.add(created)
    db.commit()
    db.refresh(created)
    return created


def _ensure_default_constraints(
    db: Session,
    *,
    channel_type: str,
    preferred_default_id: int | None = None,
) -> None:
    statement = select(InputChannel).where(InputChannel.channel_type == channel_type).order_by(InputChannel.id.asc())
    channels = list(db.scalars(statement))
    if not channels:
        return

    default_ids = [channel.id for channel in channels if channel.is_default]
    selected_default_id: int
    if preferred_default_id is not None:
        preferred = next((channel for channel in channels if channel.id == preferred_default_id), None)
        if preferred is not None and preferred.is_default:
            selected_default_id = preferred.id
        elif default_ids:
            selected_default_id = default_ids[0]
        else:
            selected_default_id = preferred.id if preferred is not None else channels[0].id
    elif default_ids:
        selected_default_id = default_ids[0]
    else:
        selected_default_id = channels[0].id

    db.execute(
        update(InputChannel)
        .where(InputChannel.channel_type == channel_type)
        .values(is_default=False)
    )
    db.execute(
        update(InputChannel)
        .where(InputChannel.id == selected_default_id)
        .values(is_default=True)
    )
