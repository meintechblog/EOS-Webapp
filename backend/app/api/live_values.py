from datetime import datetime, timezone
import math

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import get_db
from app.dependencies import get_settings_from_app
from app.repositories.mappings import list_mappings
from app.repositories.telemetry import get_latest_events_by_mapping
from app.schemas.live_values import LiveValueResponse


router = APIRouter(prefix="/api", tags=["live-values"])


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _apply_value_transform(
    raw_value: str | None,
    *,
    value_multiplier: float,
    sign_convention: str,
) -> str | None:
    if raw_value is None:
        return None

    try:
        numeric_value = float(raw_value)
    except (TypeError, ValueError):
        return raw_value

    transformed = numeric_value * value_multiplier
    if sign_convention == "positive_is_export":
        transformed = transformed * -1.0

    if math.isclose(transformed, round(transformed), rel_tol=0.0, abs_tol=1e-9):
        return str(int(round(transformed)))
    return format(transformed, ".12g")


@router.get("/live-values", response_model=list[LiveValueResponse])
def get_live_values(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_from_app),
) -> list[LiveValueResponse]:
    mappings = list_mappings(db)
    latest_events = get_latest_events_by_mapping(db)
    now = datetime.now(timezone.utc)
    items: list[LiveValueResponse] = []

    for mapping in mappings:
        if mapping.fixed_value is not None:
            items.append(
                LiveValueResponse(
                    mapping_id=mapping.id,
                    eos_field=mapping.eos_field,
                    channel_id=None,
                    channel_code=None,
                    channel_type=None,
                    input_key=None,
                    mqtt_topic=None,
                    unit=mapping.unit,
                    parsed_value=_apply_value_transform(
                        mapping.fixed_value,
                        value_multiplier=mapping.value_multiplier,
                        sign_convention=mapping.sign_convention,
                    ),
                    ts=_to_utc(mapping.updated_at),
                    last_seen_seconds=0,
                    status="healthy",
                )
            )
            continue

        latest = latest_events.get(mapping.id)
        if latest is None:
            items.append(
                LiveValueResponse(
                    mapping_id=mapping.id,
                    eos_field=mapping.eos_field,
                    channel_id=mapping.channel_id,
                    channel_code=mapping.channel_code,
                    channel_type=mapping.channel_type,
                    input_key=mapping.input_key,
                    mqtt_topic=mapping.mqtt_topic,
                    unit=mapping.unit,
                    parsed_value=None,
                    ts=None,
                    last_seen_seconds=None,
                    status="never",
                )
            )
            continue

        event_ts = _to_utc(latest.ts)
        last_seen_seconds = max(0, int((now - event_ts).total_seconds()))
        status = "healthy" if last_seen_seconds <= settings.live_stale_seconds else "stale"
        items.append(
                LiveValueResponse(
                    mapping_id=mapping.id,
                    eos_field=mapping.eos_field,
                    channel_id=mapping.channel_id,
                    channel_code=mapping.channel_code,
                    channel_type=mapping.channel_type,
                    input_key=mapping.input_key,
                    mqtt_topic=mapping.mqtt_topic,
                    unit=mapping.unit,
                    parsed_value=latest.parsed_value,
                ts=event_ts,
                last_seen_seconds=last_seen_seconds,
                status=status,
            )
        )

    return items
