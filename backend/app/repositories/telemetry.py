from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import TelemetryEvent


@dataclass(frozen=True)
class LatestTelemetryEvent:
    mapping_id: int
    parsed_value: str | None
    ts: datetime


def create_telemetry_event(
    db: Session,
    *,
    mapping_id: int,
    eos_field: str,
    raw_payload: str,
    parsed_value: str | None,
    event_ts: datetime | None = None,
) -> TelemetryEvent:
    event = TelemetryEvent(
        mapping_id=mapping_id,
        eos_field=eos_field,
        raw_payload=raw_payload,
        parsed_value=parsed_value,
        ts=event_ts or datetime.now(timezone.utc),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def get_latest_events_by_mapping(db: Session) -> dict[int, LatestTelemetryEvent]:
    ranked_events = (
        select(
            TelemetryEvent.mapping_id.label("mapping_id"),
            TelemetryEvent.parsed_value.label("parsed_value"),
            TelemetryEvent.ts.label("ts"),
            func.row_number()
            .over(
                partition_by=TelemetryEvent.mapping_id,
                order_by=(TelemetryEvent.ts.desc(), TelemetryEvent.id.desc()),
            )
            .label("rank_idx"),
        )
        .subquery()
    )

    rows = db.execute(
        select(
            ranked_events.c.mapping_id,
            ranked_events.c.parsed_value,
            ranked_events.c.ts,
        ).where(ranked_events.c.rank_idx == 1)
    ).all()

    latest_by_mapping: dict[int, LatestTelemetryEvent] = {}
    for row in rows:
        latest_by_mapping[row.mapping_id] = LatestTelemetryEvent(
            mapping_id=row.mapping_id,
            parsed_value=row.parsed_value,
            ts=row.ts,
        )
    return latest_by_mapping

