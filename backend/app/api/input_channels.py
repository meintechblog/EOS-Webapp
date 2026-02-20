from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import InputChannel
from app.db.session import get_db
from app.repositories.input_channels import (
    check_input_channel_delete,
    create_input_channel,
    delete_input_channel,
    get_input_channel_by_id,
    list_input_channels,
    update_input_channel,
)
from app.schemas.input_channels import (
    InputChannelCreateRequest,
    InputChannelResponse,
    InputChannelUpdateRequest,
)


router = APIRouter(prefix="/api", tags=["input-channels"])


@router.get("/input-channels", response_model=list[InputChannelResponse])
def get_input_channels(
    channel_type: str | None = None,
    include_secrets: bool = False,
    db: Session = Depends(get_db),
) -> list[InputChannelResponse]:
    if channel_type not in (None, "mqtt", "http"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid channel_type")

    channels = list_input_channels(db, channel_type=channel_type)
    return [_to_response(channel, include_secrets=include_secrets) for channel in channels]


@router.post("/input-channels", response_model=InputChannelResponse, status_code=status.HTTP_201_CREATED)
def post_input_channel(
    payload: InputChannelCreateRequest,
    db: Session = Depends(get_db),
) -> InputChannelResponse:
    config_json = _validate_channel_config(payload.channel_type, payload.config_json)
    try:
        channel = create_input_channel(
            db,
            code=payload.code,
            name=payload.name,
            channel_type=payload.channel_type,
            enabled=payload.enabled,
            is_default=payload.is_default,
            config_json=config_json,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Input channel conflict: {exc.orig}",
        )

    return _to_response(channel)


@router.put("/input-channels/{channel_id}", response_model=InputChannelResponse)
def put_input_channel(
    channel_id: int,
    payload: InputChannelUpdateRequest,
    db: Session = Depends(get_db),
) -> InputChannelResponse:
    channel = get_input_channel_by_id(db, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Input channel not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field must be provided",
        )

    config_json = None
    if "config_json" in updates:
        config_json = _validate_channel_config(channel.channel_type, updates["config_json"] or {})

    try:
        updated = update_input_channel(
            db,
            channel,
            code=updates.get("code"),
            name=updates.get("name"),
            enabled=updates.get("enabled"),
            is_default=updates.get("is_default"),
            config_json=config_json,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Input channel conflict: {exc.orig}",
        )

    return _to_response(updated)


@router.delete(
    "/input-channels/{channel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_input_channel_endpoint(
    channel_id: int,
    db: Session = Depends(get_db),
) -> Response:
    channel = get_input_channel_by_id(db, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Input channel not found")

    delete_check = check_input_channel_delete(db, channel)
    if delete_check.blocked:
        detail = "Input channel is still referenced by mappings"
        if delete_check.reason == "last_default_channel_of_type":
            detail = "Cannot delete the last default channel for this channel_type"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": detail,
                "reason": delete_check.reason,
                "mapping_count": delete_check.mapping_count,
                "binding_count": delete_check.binding_count,
            },
        )

    delete_input_channel(db, channel)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _to_response(channel: InputChannel, *, include_secrets: bool = False) -> InputChannelResponse:
    config = dict(channel.config_json or {})
    if not include_secrets and "password" in config:
        config["password"] = "***"
    return InputChannelResponse(
        id=channel.id,
        code=channel.code,
        name=channel.name,
        channel_type=channel.channel_type,  # type: ignore[arg-type]
        enabled=channel.enabled,
        is_default=channel.is_default,
        config_json=config,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


def _validate_channel_config(channel_type: str, config_json: dict[str, Any]) -> dict[str, Any]:
    config = dict(config_json)
    if channel_type == "mqtt":
        port = config.get("port")
        qos = config.get("qos")
        if port is not None:
            port_int = int(port)
            if port_int < 1 or port_int > 65535:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mqtt port out of range")
            config["port"] = port_int
        if qos is not None:
            qos_int = int(qos)
            if qos_int < 0 or qos_int > 2:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mqtt qos out of range")
            config["qos"] = qos_int

        for key in ("host", "client_id", "discovery_topic", "username", "password"):
            value = config.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"mqtt config field '{key}' must be string")
            stripped = value.strip()
            if stripped == "":
                config.pop(key, None)
            else:
                config[key] = stripped
    elif channel_type == "http":
        for key in ("path_prefix",):
            value = config.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"http config field '{key}' must be string")
            config[key] = value.strip()

    return config
