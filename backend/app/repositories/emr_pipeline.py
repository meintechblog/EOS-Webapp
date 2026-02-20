from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


POWER_KEYS: tuple[str, ...] = (
    "house_load_w",
    "pv_power_w",
    "grid_import_w",
    "grid_export_w",
    "battery_power_w",
)

EMR_KEY_BY_POWER_KEY: dict[str, str] = {
    "house_load_w": "house_load_emr_kwh",
    "pv_power_w": "pv_production_emr_kwh",
    "grid_import_w": "grid_import_emr_kwh",
    "grid_export_w": "grid_export_emr_kwh",
}

DEFAULT_MEASUREMENT_EMR_KEYS: dict[str, list[str]] = {
    "load_emr_keys": ["house_load_emr_kwh"],
    "grid_import_emr_keys": ["grid_import_emr_kwh"],
    "grid_export_emr_keys": ["grid_export_emr_kwh"],
    "pv_production_emr_keys": ["pv_production_emr_kwh"],
}


@dataclass(frozen=True)
class EmrState:
    ts: datetime
    emr_key: str
    emr_kwh: float
    last_power_w: float | None
    last_ts: datetime | None
    method: str
    notes: str | None


def upsert_power_sample(
    db: Session,
    *,
    ts: datetime,
    key: str,
    value_w: float,
    source: str,
    quality: str = "ok",
    mapping_id: int | None = None,
    raw_payload: str | None = None,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO power_samples
                (ts, key, value_w, source, quality, mapping_id, raw_payload, ingested_at)
            VALUES
                (:ts, :key, :value_w, :source, :quality, :mapping_id, :raw_payload, now())
            ON CONFLICT (key, ts, source, COALESCE(mapping_id, 0))
            DO UPDATE SET
                value_w = EXCLUDED.value_w,
                quality = EXCLUDED.quality,
                raw_payload = EXCLUDED.raw_payload,
                ingested_at = now()
            """
        ),
        {
            "ts": ts,
            "key": key,
            "value_w": value_w,
            "source": source,
            "quality": quality,
            "mapping_id": mapping_id,
            "raw_payload": raw_payload,
        },
    )


def get_power_sample_value_by_key_ts(db: Session, *, key: str, ts: datetime) -> float | None:
    row = db.execute(
        text(
            """
            SELECT value_w
            FROM power_samples
            WHERE key = :key AND ts = :ts
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"key": key, "ts": ts},
    ).first()
    if row is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def update_power_sample_value_by_key_ts(
    db: Session,
    *,
    key: str,
    ts: datetime,
    value_w: float,
    quality: str = "ok",
) -> int:
    result = db.execute(
        text(
            """
            UPDATE power_samples
            SET value_w = :value_w, quality = :quality, ingested_at = now()
            WHERE key = :key AND ts = :ts
            """
        ),
        {
            "key": key,
            "ts": ts,
            "value_w": value_w,
            "quality": quality,
        },
    )
    return max(0, result.rowcount or 0)


def get_latest_power_samples(
    db: Session,
    *,
    keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    if keys:
        rows = db.execute(
            text(
                """
                SELECT DISTINCT ON (key)
                    key, ts, value_w, source, quality, mapping_id, raw_payload, ingested_at
                FROM power_samples
                WHERE key = ANY(:keys)
                ORDER BY key ASC, ts DESC, id DESC
                """
            ),
            {"keys": keys},
        ).mappings()
    else:
        rows = db.execute(
            text(
                """
                SELECT DISTINCT ON (key)
                    key, ts, value_w, source, quality, mapping_id, raw_payload, ingested_at
                FROM power_samples
                ORDER BY key ASC, ts DESC, id DESC
                """
            )
        ).mappings()
    return [dict(row) for row in rows]


def get_power_series(
    db: Session,
    *,
    key: str,
    from_ts: datetime,
    to_ts: datetime,
) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT key, ts, value_w, source, quality, mapping_id, raw_payload, ingested_at
            FROM power_samples
            WHERE key = :key
              AND ts >= :from_ts
              AND ts <= :to_ts
            ORDER BY ts ASC, id ASC
            """
        ),
        {
            "key": key,
            "from_ts": from_ts,
            "to_ts": to_ts,
        },
    ).mappings()
    return [dict(row) for row in rows]


def upsert_energy_emr(
    db: Session,
    *,
    ts: datetime,
    emr_key: str,
    emr_kwh: float,
    last_power_w: float | None,
    last_ts: datetime | None,
    method: str,
    notes: str | None,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO energy_emr
                (ts, emr_key, emr_kwh, last_power_w, last_ts, method, notes, created_at)
            VALUES
                (:ts, :emr_key, :emr_kwh, :last_power_w, :last_ts, :method, :notes, now())
            ON CONFLICT (emr_key, ts)
            DO UPDATE SET
                emr_kwh = EXCLUDED.emr_kwh,
                last_power_w = EXCLUDED.last_power_w,
                last_ts = EXCLUDED.last_ts,
                method = EXCLUDED.method,
                notes = EXCLUDED.notes
            """
        ),
        {
            "ts": ts,
            "emr_key": emr_key,
            "emr_kwh": emr_kwh,
            "last_power_w": last_power_w,
            "last_ts": last_ts,
            "method": method,
            "notes": notes,
        },
    )


def get_latest_emr_state(db: Session, *, emr_key: str) -> EmrState | None:
    row = db.execute(
        text(
            """
            SELECT ts, emr_key, emr_kwh, last_power_w, last_ts, method, notes
            FROM energy_emr
            WHERE emr_key = :emr_key
            ORDER BY ts DESC, id DESC
            LIMIT 1
            """
        ),
        {"emr_key": emr_key},
    ).mappings().first()
    if row is None:
        return None
    return EmrState(
        ts=row["ts"],
        emr_key=str(row["emr_key"]),
        emr_kwh=float(row["emr_kwh"]),
        last_power_w=float(row["last_power_w"]) if row["last_power_w"] is not None else None,
        last_ts=row["last_ts"],
        method=str(row["method"]),
        notes=row["notes"],
    )


def get_latest_emr_values(
    db: Session,
    *,
    emr_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    if emr_keys:
        rows = db.execute(
            text(
                """
                SELECT DISTINCT ON (emr_key)
                    emr_key, ts, emr_kwh, last_power_w, last_ts, method, notes, created_at
                FROM energy_emr
                WHERE emr_key = ANY(:emr_keys)
                ORDER BY emr_key ASC, ts DESC, id DESC
                """
            ),
            {"emr_keys": emr_keys},
        ).mappings()
    else:
        rows = db.execute(
            text(
                """
                SELECT DISTINCT ON (emr_key)
                    emr_key, ts, emr_kwh, last_power_w, last_ts, method, notes, created_at
                FROM energy_emr
                ORDER BY emr_key ASC, ts DESC, id DESC
                """
            )
        ).mappings()
    return [dict(row) for row in rows]


def get_emr_series(
    db: Session,
    *,
    emr_key: str,
    from_ts: datetime,
    to_ts: datetime,
) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT emr_key, ts, emr_kwh, last_power_w, last_ts, method, notes, created_at
            FROM energy_emr
            WHERE emr_key = :emr_key
              AND ts >= :from_ts
              AND ts <= :to_ts
            ORDER BY ts ASC, id ASC
            """
        ),
        {
            "emr_key": emr_key,
            "from_ts": from_ts,
            "to_ts": to_ts,
        },
    ).mappings()
    return [dict(row) for row in rows]


def create_measurement_sync_run(db: Session, *, trigger_source: str) -> int:
    row = db.execute(
        text(
            """
            INSERT INTO eos_measurement_sync_runs
                (trigger_source, started_at, status, pushed_count, details_json)
            VALUES
                (:trigger_source, :started_at, 'running', 0, CAST(:details_json AS JSONB))
            RETURNING id
            """
        ),
        {
            "trigger_source": trigger_source,
            "started_at": datetime.now(timezone.utc),
            "details_json": "{}",
        },
    ).first()
    if row is None:
        raise RuntimeError("failed to create eos_measurement_sync_run")
    return int(row[0])


def finish_measurement_sync_run(
    db: Session,
    *,
    run_id: int,
    status: str,
    pushed_count: int,
    details_json: dict[str, Any] | list[Any] | None,
    error_text: str | None,
) -> None:
    db.execute(
        text(
            """
            UPDATE eos_measurement_sync_runs
            SET
                finished_at = :finished_at,
                status = :status,
                pushed_count = :pushed_count,
                details_json = CAST(:details_json AS JSONB),
                error_text = :error_text
            WHERE id = :run_id
            """
        ),
        {
            "run_id": run_id,
            "finished_at": datetime.now(timezone.utc),
            "status": status,
            "pushed_count": pushed_count,
            "details_json": _json_or_none(details_json),
            "error_text": error_text,
        },
    )


def get_latest_measurement_sync_run(db: Session) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT id, trigger_source, started_at, finished_at, status, pushed_count, details_json, error_text
            FROM eos_measurement_sync_runs
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """
        )
    ).mappings().first()
    return dict(row) if row is not None else None


def get_power_samples_rows_24h(db: Session) -> int:
    return int(
        db.scalar(
            text("SELECT COUNT(*) FROM power_samples WHERE ts >= (now() - INTERVAL '24 hour')")
        )
        or 0
    )


def get_energy_emr_rows_24h(db: Session) -> int:
    return int(
        db.scalar(
            text("SELECT COUNT(*) FROM energy_emr WHERE ts >= (now() - INTERVAL '24 hour')")
        )
        or 0
    )


def _json_or_none(value: dict[str, Any] | list[Any] | None) -> str | None:
    if value is None:
        return None
    import json

    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)
