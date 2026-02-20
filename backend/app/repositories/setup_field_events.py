from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def create_setup_field_event(
    db: Session,
    *,
    field_id: str,
    source: str,
    raw_value_text: str | None,
    normalized_value: Any,
    event_ts: datetime,
    apply_status: str,
    error_text: str | None,
) -> int:
    row = db.execute(
        text(
            """
            INSERT INTO setup_field_events
                (field_id, source, raw_value_text, normalized_value_json, event_ts, apply_status, error_text)
            VALUES
                (:field_id, :source, :raw_value_text, CAST(:normalized_value_json AS JSONB), :event_ts, :apply_status, :error_text)
            RETURNING id
            """
        ),
        {
            "field_id": field_id,
            "source": source,
            "raw_value_text": raw_value_text,
            "normalized_value_json": _to_json(normalized_value),
            "event_ts": event_ts,
            "apply_status": apply_status,
            "error_text": error_text,
        },
    ).first()
    if row is None:
        raise RuntimeError("failed to insert setup_field_event")
    return int(row[0])


def update_setup_field_events_status(
    db: Session,
    *,
    event_ids: list[int],
    apply_status: str,
    error_text: str | None = None,
) -> None:
    if not event_ids:
        return
    db.execute(
        text(
            """
            UPDATE setup_field_events
            SET apply_status = :apply_status, error_text = :error_text
            WHERE id = ANY(:event_ids)
            """
        ),
        {
            "event_ids": event_ids,
            "apply_status": apply_status,
            "error_text": error_text,
        },
    )


def list_latest_setup_field_events(
    db: Session,
    *,
    field_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    params: dict[str, Any] = {}
    where_sql = ""
    if field_ids:
        where_sql = "WHERE field_id = ANY(:field_ids)"
        params["field_ids"] = field_ids
    rows = db.execute(
        text(
            f"""
            SELECT DISTINCT ON (field_id)
                field_id,
                source,
                event_ts,
                apply_status,
                error_text,
                created_at
            FROM setup_field_events
            {where_sql}
            ORDER BY field_id, created_at DESC, id DESC
            """
        ),
        params,
    ).mappings()
    return {str(row["field_id"]): dict(row) for row in rows}


def _to_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))

