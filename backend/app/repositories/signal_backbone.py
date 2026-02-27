from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings


@dataclass(frozen=True)
class SignalSeriesResult:
    signal_key: str
    resolution: str
    points: list[dict[str, Any]]


@dataclass(frozen=True)
class JobRunSnapshot:
    id: int
    job_name: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    affected_rows: int
    details_json: dict[str, Any] | list[Any] | None
    error_text: str | None


@dataclass(frozen=True)
class DataPipelineStatus:
    last_rollup_run: JobRunSnapshot | None
    last_retention_run: JobRunSnapshot | None
    raw_rows_24h: int
    rollup_rows_24h: int
    signal_catalog_count: int


def ingest_signal_measurement(
    db: Session,
    *,
    signal_key: str,
    label: str,
    value_type: str,
    canonical_unit: str | None,
    value: Any,
    ts: datetime,
    quality_status: str,
    source_type: str,
    run_id: int | None = None,
    source_ref_id: int | None = None,
    tags_json: dict[str, Any] | None = None,
    ingested_at: datetime | None = None,
) -> None:
    signal_id = ensure_signal_catalog_entry(
        db,
        signal_key=signal_key,
        label=label,
        value_type=value_type,
        canonical_unit=canonical_unit,
        tags_json=tags_json or {},
    )
    value_num, value_text, value_bool, value_json = _split_value_columns(value)
    ingest_ts = ingested_at or datetime.now(timezone.utc)
    ingest_lag_ms = _ingest_lag_ms(ingest_ts, ts)

    db.execute(
        text(
            """
            INSERT INTO signal_measurements_raw
                (signal_id, ts, value_num, value_text, value_bool, value_json,
                 quality_status, source_type, run_id, source_ref_id, ingested_at, ingest_lag_ms)
            VALUES
                (:signal_id, :ts, :value_num, :value_text, :value_bool, CAST(:value_json AS JSONB),
                 :quality_status, :source_type, :run_id, :source_ref_id, :ingested_at, :ingest_lag_ms)
            ON CONFLICT (
                signal_id, ts, source_type,
                COALESCE(run_id, 0),
                COALESCE(source_ref_id, 0)
            )
            DO UPDATE SET
                value_num = EXCLUDED.value_num,
                value_text = EXCLUDED.value_text,
                value_bool = EXCLUDED.value_bool,
                value_json = EXCLUDED.value_json,
                quality_status = EXCLUDED.quality_status,
                ingested_at = EXCLUDED.ingested_at,
                ingest_lag_ms = EXCLUDED.ingest_lag_ms
            """
        ),
        {
            "signal_id": signal_id,
            "ts": ts,
            "value_num": value_num,
            "value_text": value_text,
            "value_bool": value_bool,
            "value_json": _json_or_none(value_json),
            "quality_status": quality_status,
            "source_type": source_type,
            "run_id": run_id,
            "source_ref_id": source_ref_id,
            "ingested_at": ingest_ts,
            "ingest_lag_ms": ingest_lag_ms,
        },
    )

    db.execute(
        text(
            """
            INSERT INTO signal_state_latest
                (signal_id, last_ts, last_value_num, last_value_text, last_value_bool, last_value_json,
                 last_quality_status, last_source_type, last_run_id, updated_at)
            VALUES
                (:signal_id, :last_ts, :last_value_num, :last_value_text, :last_value_bool,
                 CAST(:last_value_json AS JSONB), :last_quality_status, :last_source_type, :last_run_id, :updated_at)
            ON CONFLICT (signal_id)
            DO UPDATE SET
                last_ts = EXCLUDED.last_ts,
                last_value_num = EXCLUDED.last_value_num,
                last_value_text = EXCLUDED.last_value_text,
                last_value_bool = EXCLUDED.last_value_bool,
                last_value_json = EXCLUDED.last_value_json,
                last_quality_status = EXCLUDED.last_quality_status,
                last_source_type = EXCLUDED.last_source_type,
                last_run_id = EXCLUDED.last_run_id,
                updated_at = EXCLUDED.updated_at
            WHERE signal_state_latest.last_ts IS NULL OR EXCLUDED.last_ts >= signal_state_latest.last_ts
            """
        ),
        {
            "signal_id": signal_id,
            "last_ts": ts,
            "last_value_num": value_num,
            "last_value_text": value_text,
            "last_value_bool": value_bool,
            "last_value_json": _json_or_none(value_json),
            "last_quality_status": quality_status,
            "last_source_type": source_type,
            "last_run_id": run_id,
            "updated_at": ingest_ts,
        },
    )
    db.commit()


def ensure_signal_catalog_entry(
    db: Session,
    *,
    signal_key: str,
    label: str,
    value_type: str,
    canonical_unit: str | None,
    tags_json: dict[str, Any],
) -> int:
    row = db.execute(
        text(
            """
            INSERT INTO signal_catalog
                (signal_key, label, value_type, canonical_unit, tags_json, created_at, updated_at)
            VALUES
                (:signal_key, :label, :value_type, :canonical_unit, CAST(:tags_json AS JSONB), now(), now())
            ON CONFLICT (signal_key)
            DO UPDATE SET
                label = EXCLUDED.label,
                value_type = EXCLUDED.value_type,
                canonical_unit = EXCLUDED.canonical_unit,
                tags_json = EXCLUDED.tags_json,
                updated_at = now()
            RETURNING id
            """
        ),
        {
            "signal_key": signal_key,
            "label": label,
            "value_type": value_type,
            "canonical_unit": canonical_unit,
            "tags_json": _json_or_empty(tags_json),
        },
    ).first()
    if row is None:
        raise RuntimeError(f"failed to upsert signal_catalog entry for {signal_key}")
    return int(row[0])


def list_signals_with_latest(db: Session, *, limit: int = 500) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT
                c.id,
                c.signal_key,
                c.label,
                c.value_type,
                c.canonical_unit,
                c.tags_json,
                s.last_ts,
                s.last_value_num,
                s.last_value_text,
                s.last_value_bool,
                s.last_value_json,
                s.last_quality_status,
                s.last_source_type,
                s.last_run_id,
                s.updated_at
            FROM signal_catalog c
            LEFT JOIN signal_state_latest s ON s.signal_id = c.id
            ORDER BY c.signal_key ASC
            LIMIT :limit
            """
        ),
        {"limit": max(1, limit)},
    ).mappings()
    return [dict(row) for row in rows]


def list_latest_by_signal_keys(
    db: Session,
    *,
    signal_keys: list[str] | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    if signal_keys:
        rows = db.execute(
            text(
                """
                SELECT
                    c.signal_key,
                    c.label,
                    c.value_type,
                    c.canonical_unit,
                    c.tags_json,
                    s.last_ts,
                    s.last_value_num,
                    s.last_value_text,
                    s.last_value_bool,
                    s.last_value_json,
                    s.last_quality_status,
                    s.last_source_type,
                    s.last_run_id,
                    s.updated_at
                FROM signal_catalog c
                LEFT JOIN signal_state_latest s ON s.signal_id = c.id
                WHERE c.signal_key = ANY(:signal_keys)
                ORDER BY c.signal_key ASC
                """
            ),
            {"signal_keys": signal_keys},
        ).mappings()
        return [dict(row) for row in rows]

    rows = db.execute(
        text(
            """
            SELECT
                c.signal_key,
                c.label,
                c.value_type,
                c.canonical_unit,
                c.tags_json,
                s.last_ts,
                s.last_value_num,
                s.last_value_text,
                s.last_value_bool,
                s.last_value_json,
                s.last_quality_status,
                s.last_source_type,
                s.last_run_id,
                s.updated_at
            FROM signal_catalog c
            LEFT JOIN signal_state_latest s ON s.signal_id = c.id
            ORDER BY s.last_ts DESC NULLS LAST, c.signal_key ASC
            LIMIT :limit
            """
        ),
        {"limit": max(1, limit)},
    ).mappings()
    return [dict(row) for row in rows]


def fetch_signal_series(
    db: Session,
    *,
    signal_key: str,
    from_ts: datetime,
    to_ts: datetime,
    resolution: str,
) -> SignalSeriesResult:
    if resolution == "raw":
        rows = db.execute(
            text(
                """
                SELECT
                    m.ts,
                    m.value_num,
                    m.value_text,
                    m.value_bool,
                    m.value_json,
                    m.quality_status,
                    m.source_type,
                    m.run_id
                FROM signal_measurements_raw m
                JOIN signal_catalog c ON c.id = m.signal_id
                WHERE c.signal_key = :signal_key
                  AND m.ts >= :from_ts
                  AND m.ts <= :to_ts
                ORDER BY m.ts ASC, m.id ASC
                """
            ),
            {
                "signal_key": signal_key,
                "from_ts": from_ts,
                "to_ts": to_ts,
            },
        ).mappings()
        return SignalSeriesResult(
            signal_key=signal_key,
            resolution=resolution,
            points=[dict(row) for row in rows],
        )

    table_name = _rollup_table_for_resolution(resolution)
    rows = db.execute(
        text(
            f"""
            SELECT
                r.bucket_start AS ts,
                r.min_num,
                r.max_num,
                r.avg_num,
                r.sum_num,
                r.count_num,
                r.last_num
            FROM {table_name} r
            JOIN signal_catalog c ON c.id = r.signal_id
            WHERE c.signal_key = :signal_key
              AND r.bucket_start >= :from_ts
              AND r.bucket_start <= :to_ts
            ORDER BY r.bucket_start ASC
            """
        ),
        {
            "signal_key": signal_key,
            "from_ts": from_ts,
            "to_ts": to_ts,
        },
    ).mappings()
    return SignalSeriesResult(
        signal_key=signal_key,
        resolution=resolution,
        points=[dict(row) for row in rows],
    )


def get_job_snapshot(db: Session, *, job_name: str) -> JobRunSnapshot | None:
    row = db.execute(
        text(
            """
            SELECT id, job_name, started_at, finished_at, status, affected_rows, details_json, error_text
            FROM retention_job_runs
            WHERE job_name = :job_name
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"job_name": job_name},
    ).mappings().first()
    if row is None:
        return None
    return JobRunSnapshot(
        id=int(row["id"]),
        job_name=str(row["job_name"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=str(row["status"]),
        affected_rows=int(row["affected_rows"]),
        details_json=row["details_json"],
        error_text=row["error_text"],
    )


def get_data_pipeline_status(db: Session) -> DataPipelineStatus:
    raw_rows_24h = int(
        db.scalar(
            text("SELECT COUNT(*) FROM signal_measurements_raw WHERE ts >= (now() - INTERVAL '24 hour')")
        )
        or 0
    )
    rollup_rows_24h = int(
        db.scalar(
            text(
                """
                SELECT
                    (SELECT COUNT(*) FROM signal_rollup_5m WHERE bucket_start >= (now() - INTERVAL '24 hour')) +
                    (SELECT COUNT(*) FROM signal_rollup_1h WHERE bucket_start >= (now() - INTERVAL '24 hour')) +
                    (SELECT COUNT(*) FROM signal_rollup_1d WHERE bucket_start >= (now() - INTERVAL '24 hour'))
                """
            )
        )
        or 0
    )
    signal_catalog_count = int(db.scalar(text("SELECT COUNT(*) FROM signal_catalog")) or 0)
    return DataPipelineStatus(
        last_rollup_run=get_job_snapshot(db, job_name="rollup"),
        last_retention_run=get_job_snapshot(db, job_name="retention"),
        raw_rows_24h=raw_rows_24h,
        rollup_rows_24h=rollup_rows_24h,
        signal_catalog_count=signal_catalog_count,
    )


def run_rollup_job(db: Session) -> tuple[int, dict[str, Any]]:
    started_at = datetime.now(timezone.utc)
    details: dict[str, Any] = {}
    affected_rows = 0

    try:
        for resolution in ("5m", "1h", "1d"):
            table_name = _rollup_table_for_resolution(resolution)
            from_ts = _rollup_from_ts(db, table_name=table_name, resolution=resolution)
            bucket_expr = _bucket_expr_for_resolution(resolution)
            result = db.execute(
                text(
                    f"""
                    INSERT INTO {table_name}
                        (signal_id, bucket_start, min_num, max_num, avg_num, sum_num, count_num, last_num)
                    SELECT
                        signal_id,
                        {bucket_expr} AS bucket_start,
                        MIN(value_num) AS min_num,
                        MAX(value_num) AS max_num,
                        AVG(value_num) AS avg_num,
                        SUM(value_num) AS sum_num,
                        COUNT(value_num) AS count_num,
                        (ARRAY_AGG(value_num ORDER BY ts DESC))[1] AS last_num
                    FROM signal_measurements_raw
                    WHERE value_num IS NOT NULL
                      AND ts >= :from_ts
                    GROUP BY signal_id, bucket_start
                    ON CONFLICT (signal_id, bucket_start)
                    DO UPDATE SET
                        min_num = EXCLUDED.min_num,
                        max_num = EXCLUDED.max_num,
                        avg_num = EXCLUDED.avg_num,
                        sum_num = EXCLUDED.sum_num,
                        count_num = EXCLUDED.count_num,
                        last_num = EXCLUDED.last_num
                    """
                ),
                {"from_ts": from_ts},
            )
            rowcount = max(0, result.rowcount or 0)
            details[f"{resolution}_rows_upserted"] = rowcount
            details[f"{resolution}_from_ts"] = from_ts.isoformat()
            affected_rows += rowcount

        _insert_job_run(
            db,
            job_name="rollup",
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            status="ok",
            affected_rows=affected_rows,
            details_json=details,
            error_text=None,
        )
        db.commit()
        return affected_rows, details
    except Exception as exc:
        _insert_job_run(
            db,
            job_name="rollup",
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            status="error",
            affected_rows=affected_rows,
            details_json=details,
            error_text=str(exc),
        )
        db.commit()
        raise


def run_retention_job(db: Session, *, settings: Settings) -> tuple[int, dict[str, Any]]:
    started_at = datetime.now(timezone.utc)
    details: dict[str, Any] = {}
    affected_rows = 0

    raw_cutoff = datetime.now(timezone.utc) - timedelta(days=settings.data_raw_retention_days)
    rollup_5m_cutoff = datetime.now(timezone.utc) - timedelta(days=settings.data_rollup_5m_retention_days)
    rollup_1h_cutoff = datetime.now(timezone.utc) - timedelta(days=settings.data_rollup_1h_retention_days)
    rollup_1d_cutoff = (
        None
        if settings.data_rollup_1d_retention_days <= 0
        else datetime.now(timezone.utc) - timedelta(days=settings.data_rollup_1d_retention_days)
    )
    artifact_cutoff = datetime.now(timezone.utc) - timedelta(days=settings.eos_artifact_raw_retention_days)

    try:
        raw_result = db.execute(
            text("DELETE FROM signal_measurements_raw WHERE ts < :cutoff"),
            {"cutoff": raw_cutoff},
        )
        raw_deleted = max(0, raw_result.rowcount or 0)
        details["raw_deleted"] = raw_deleted
        details["raw_cutoff"] = raw_cutoff.isoformat()
        affected_rows += raw_deleted

        power_samples_result = db.execute(
            text("DELETE FROM power_samples WHERE ts < :cutoff"),
            {"cutoff": raw_cutoff},
        )
        power_samples_deleted = max(0, power_samples_result.rowcount or 0)
        details["power_samples_deleted"] = power_samples_deleted
        details["power_samples_cutoff"] = raw_cutoff.isoformat()
        affected_rows += power_samples_deleted

        rollup_5m_result = db.execute(
            text("DELETE FROM signal_rollup_5m WHERE bucket_start < :cutoff"),
            {"cutoff": rollup_5m_cutoff},
        )
        rollup_5m_deleted = max(0, rollup_5m_result.rowcount or 0)
        details["rollup_5m_deleted"] = rollup_5m_deleted
        details["rollup_5m_cutoff"] = rollup_5m_cutoff.isoformat()
        affected_rows += rollup_5m_deleted

        rollup_1h_result = db.execute(
            text("DELETE FROM signal_rollup_1h WHERE bucket_start < :cutoff"),
            {"cutoff": rollup_1h_cutoff},
        )
        rollup_1h_deleted = max(0, rollup_1h_result.rowcount or 0)
        details["rollup_1h_deleted"] = rollup_1h_deleted
        details["rollup_1h_cutoff"] = rollup_1h_cutoff.isoformat()
        affected_rows += rollup_1h_deleted

        if rollup_1d_cutoff is not None:
            rollup_1d_result = db.execute(
                text("DELETE FROM signal_rollup_1d WHERE bucket_start < :cutoff"),
                {"cutoff": rollup_1d_cutoff},
            )
            rollup_1d_deleted = max(0, rollup_1d_result.rowcount or 0)
            details["rollup_1d_deleted"] = rollup_1d_deleted
            details["rollup_1d_cutoff"] = rollup_1d_cutoff.isoformat()
            affected_rows += rollup_1d_deleted
        else:
            details["rollup_1d_deleted"] = 0
            details["rollup_1d_cutoff"] = None

        artifact_result = db.execute(
            text("DELETE FROM eos_artifacts WHERE created_at < :cutoff"),
            {"cutoff": artifact_cutoff},
        )
        artifact_deleted = max(0, artifact_result.rowcount or 0)
        details["eos_artifacts_deleted"] = artifact_deleted
        details["eos_artifacts_cutoff"] = artifact_cutoff.isoformat()
        affected_rows += artifact_deleted

        _insert_job_run(
            db,
            job_name="retention",
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            status="ok",
            affected_rows=affected_rows,
            details_json=details,
            error_text=None,
        )
        db.commit()
        return affected_rows, details
    except Exception as exc:
        _insert_job_run(
            db,
            job_name="retention",
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            status="error",
            affected_rows=affected_rows,
            details_json=details,
            error_text=str(exc),
        )
        db.commit()
        raise


def _insert_job_run(
    db: Session,
    *,
    job_name: str,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    affected_rows: int,
    details_json: dict[str, Any],
    error_text: str | None,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO retention_job_runs
                (job_name, started_at, finished_at, status, affected_rows, details_json, error_text)
            VALUES
                (:job_name, :started_at, :finished_at, :status, :affected_rows, CAST(:details_json AS JSONB), :error_text)
            """
        ),
        {
            "job_name": job_name,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "affected_rows": affected_rows,
            "details_json": _json_or_empty(details_json),
            "error_text": error_text,
        },
    )


def _rollup_from_ts(db: Session, *, table_name: str, resolution: str) -> datetime:
    latest = db.scalar(text(f"SELECT MAX(bucket_start) FROM {table_name}"))
    now = datetime.now(timezone.utc)
    if latest is None:
        return now - timedelta(days=90)
    overlap = {
        "5m": timedelta(minutes=15),
        "1h": timedelta(hours=3),
        "1d": timedelta(days=2),
    }[resolution]
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return latest - overlap


def _bucket_expr_for_resolution(resolution: str) -> str:
    if resolution == "5m":
        return (
            "date_trunc('hour', ts) + (floor(date_part('minute', ts) / 5) * interval '5 minute')"
        )
    if resolution == "1h":
        return "date_trunc('hour', ts)"
    if resolution == "1d":
        return "date_trunc('day', ts)"
    raise ValueError(f"Unsupported rollup resolution: {resolution}")


def _rollup_table_for_resolution(resolution: str) -> str:
    tables = {
        "5m": "signal_rollup_5m",
        "1h": "signal_rollup_1h",
        "1d": "signal_rollup_1d",
    }
    table_name = tables.get(resolution)
    if table_name is None:
        raise ValueError("resolution must be one of raw|5m|1h|1d")
    return table_name


def _ingest_lag_ms(ingested_at: datetime, signal_ts: datetime) -> int | None:
    try:
        signal_ts_utc = signal_ts if signal_ts.tzinfo else signal_ts.replace(tzinfo=timezone.utc)
        delta = ingested_at - signal_ts_utc
        lag_ms = max(0, int(delta.total_seconds() * 1000))
        # DB column is INTEGER; clamp to avoid overflow for older backfilled points.
        return min(lag_ms, 2_147_483_647)
    except Exception:
        return None


def infer_value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"
    if isinstance(value, (dict, list)):
        return "json"
    return "string"


def _split_value_columns(value: Any) -> tuple[float | None, str | None, bool | None, dict | list | None]:
    if value is None:
        return None, None, None, None
    if isinstance(value, bool):
        return None, None, value, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), None, None, None
    if isinstance(value, (dict, list)):
        return None, None, None, value
    text_value = str(value)
    parsed_bool = _parse_bool(text_value)
    if parsed_bool is not None:
        return None, None, parsed_bool, None
    parsed_float = _parse_float(text_value)
    if parsed_float is not None:
        return parsed_float, text_value, None, None
    return None, text_value, None, None


def _parse_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "on", "yes"}:
        return True
    if normalized in {"false", "0", "off", "no"}:
        return False
    return None


def _parse_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_or_empty(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _json_or_none(value: dict | list | None) -> str | None:
    if value is None:
        return None
    import json

    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)
