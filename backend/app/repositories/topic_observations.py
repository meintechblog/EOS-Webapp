from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models import InputChannel, InputObservation


@dataclass(frozen=True)
class TopicObservationSnapshot:
    mqtt_topic: str
    first_seen: datetime
    last_seen: datetime
    last_payload: str | None
    message_count: int
    last_retain: bool
    last_qos: int


@dataclass(frozen=True)
class InputObservationSnapshot:
    channel_id: int
    channel_code: str
    channel_type: str
    input_key: str
    normalized_key: str
    first_seen: datetime
    last_seen: datetime
    last_payload: str | None
    message_count: int
    last_meta_json: dict[str, Any]


@dataclass(frozen=True)
class DiscoveryStats:
    observed_topics_count: int
    last_discovery_ts: datetime | None


def normalize_input_key(raw_key: str) -> str:
    key = raw_key.strip()
    if key == "":
        return "eos/input/"
    if key.startswith("eos/input/"):
        return key
    if key.startswith("eos/"):
        return f"eos/input/{key[4:]}"
    return f"eos/input/{key.lstrip('/')}"


def normalize_param_key(raw_key: str) -> str:
    key = raw_key.strip()
    if key == "":
        return "eos/param/"
    if key.startswith("eos/param/"):
        return key
    if key.startswith("eos/input/"):
        return f"eos/param/{key[10:]}"
    if key.startswith("eos/"):
        return f"eos/param/{key[4:]}"
    return f"eos/param/{key.lstrip('/')}"


def infer_namespace_from_normalized_key(normalized_key: str) -> str:
    if normalized_key.startswith("eos/param/"):
        return "param"
    return "input"


def upsert_input_observation(
    db: Session,
    *,
    channel_id: int,
    input_key: str,
    normalized_key: str,
    payload: str,
    last_meta_json: dict[str, Any] | None = None,
    event_ts: datetime | None = None,
) -> None:
    ts = event_ts or datetime.now(timezone.utc)
    db.execute(
        text(
            """
            INSERT INTO input_observations
                (channel_id, input_key, normalized_key, first_seen, last_seen, last_payload, message_count, last_meta_json)
            VALUES
                (:channel_id, :input_key, :normalized_key, :first_seen, :last_seen, :last_payload, 1, CAST(:last_meta_json AS JSONB))
            ON CONFLICT (channel_id, input_key)
            DO UPDATE SET
                normalized_key = EXCLUDED.normalized_key,
                last_seen = EXCLUDED.last_seen,
                last_payload = EXCLUDED.last_payload,
                message_count = input_observations.message_count + 1,
                last_meta_json = EXCLUDED.last_meta_json
            """
        ),
        {
            "channel_id": channel_id,
            "input_key": input_key,
            "normalized_key": normalized_key,
            "first_seen": ts,
            "last_seen": ts,
            "last_payload": payload,
            "last_meta_json": _json_or_empty(last_meta_json),
        },
    )
    db.commit()


def list_input_observations(
    db: Session,
    limit: int = 500,
    *,
    channel_type: str | None = None,
    channel_id: int | None = None,
    required_prefix: str | None = None,
    seen_after: datetime | None = None,
) -> list[InputObservationSnapshot]:
    statement = (
        select(InputObservation, InputChannel)
        .join(InputChannel, InputChannel.id == InputObservation.channel_id)
    )
    if channel_type is not None:
        statement = statement.where(InputChannel.channel_type == channel_type)
    if channel_id is not None:
        statement = statement.where(InputObservation.channel_id == channel_id)
    if required_prefix is not None:
        statement = statement.where(InputObservation.normalized_key.startswith(required_prefix))
    if seen_after is not None:
        statement = statement.where(InputObservation.last_seen >= seen_after)

    rows = db.execute(
        statement.order_by(InputObservation.last_seen.desc()).limit(limit)
    ).all()
    return [
        InputObservationSnapshot(
            channel_id=observation.channel_id,
            channel_code=channel.code,
            channel_type=channel.channel_type,
            input_key=observation.input_key,
            normalized_key=observation.normalized_key,
            first_seen=observation.first_seen,
            last_seen=observation.last_seen,
            last_payload=observation.last_payload,
            message_count=observation.message_count,
            last_meta_json=_coerce_meta(observation.last_meta_json),
        )
        for observation, channel in rows
    ]


def upsert_topic_observation(
    db: Session,
    *,
    mqtt_topic: str,
    payload: str,
    retain: bool,
    qos: int,
    event_ts: datetime | None = None,
    channel_id: int,
) -> None:
    upsert_input_observation(
        db,
        channel_id=channel_id,
        input_key=mqtt_topic,
        normalized_key=normalize_input_key(mqtt_topic),
        payload=payload,
        event_ts=event_ts,
        last_meta_json={
            "retain": bool(retain),
            "qos": int(qos),
            "source": "mqtt",
        },
    )


def list_topic_observations(
    db: Session,
    limit: int = 500,
    *,
    required_prefix: str | None = None,
    seen_after: datetime | None = None,
) -> list[TopicObservationSnapshot]:
    observations = list_input_observations(
        db,
        limit=limit,
        channel_type="mqtt",
        required_prefix=required_prefix,
        seen_after=seen_after,
    )
    items: list[TopicObservationSnapshot] = []
    for observation in observations:
        meta = observation.last_meta_json
        items.append(
            TopicObservationSnapshot(
                mqtt_topic=observation.normalized_key,
                first_seen=observation.first_seen,
                last_seen=observation.last_seen,
                last_payload=observation.last_payload,
                message_count=observation.message_count,
                last_retain=bool(meta.get("retain", False)),
                last_qos=int(meta.get("qos", 0)),
            )
        )
    return items


def get_discovery_stats(
    db: Session,
    *,
    channel_type: str | None = None,
) -> DiscoveryStats:
    statement = select(func.count(InputObservation.id), func.max(InputObservation.last_seen)).select_from(
        InputObservation
    )
    if channel_type is not None:
        statement = statement.join(InputChannel, InputChannel.id == InputObservation.channel_id).where(
            InputChannel.channel_type == channel_type
        )

    row = db.execute(statement).first()
    count = int(row[0] or 0) if row else 0
    last_seen = row[1] if row else None
    return DiscoveryStats(observed_topics_count=count, last_discovery_ts=last_seen)


def _coerce_meta(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _json_or_empty(value: dict[str, Any] | None) -> str:
    import json

    payload = value if isinstance(value, dict) else {}
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
