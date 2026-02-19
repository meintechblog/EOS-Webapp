from datetime import datetime, timezone

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
        latest = latest_events.get(mapping.id)
        if latest is None:
            items.append(
                LiveValueResponse(
                    mapping_id=mapping.id,
                    eos_field=mapping.eos_field,
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
                mqtt_topic=mapping.mqtt_topic,
                unit=mapping.unit,
                parsed_value=latest.parsed_value,
                ts=event_ts,
                last_seen_seconds=last_seen_seconds,
                status=status,
            )
        )

    return items

