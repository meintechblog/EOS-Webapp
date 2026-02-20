import json
import logging
from datetime import datetime, timezone


def _stringify(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _parse_scalar_payload(raw_payload: str) -> str | None:
    stripped = raw_payload.strip()
    if stripped == "":
        return None

    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped

    return _stringify(decoded)


def parse_payload(raw_payload: str, payload_path: str | None, logger: logging.Logger) -> str | None:
    if not payload_path:
        return _parse_scalar_payload(raw_payload)

    try:
        decoded_json = json.loads(raw_payload)
    except json.JSONDecodeError:
        logger.warning("payload_path is set but payload is not valid JSON path=%s", payload_path)
        return None

    current: object = decoded_json
    for part in payload_path.split("."):
        if not isinstance(current, dict) or part not in current:
            logger.warning("payload_path not found path=%s payload=%s", payload_path, raw_payload)
            return None
        current = current[part]

    return _stringify(current)


def parse_event_timestamp(
    raw_payload: str,
    timestamp_path: str | None,
    *,
    fallback_ts: datetime,
    logger: logging.Logger,
) -> datetime:
    if not timestamp_path:
        return _to_utc(fallback_ts)

    try:
        decoded_json = json.loads(raw_payload)
    except json.JSONDecodeError:
        logger.warning(
            "timestamp_path is set but payload is not valid JSON path=%s",
            timestamp_path,
        )
        return _to_utc(fallback_ts)

    current: object = decoded_json
    for part in timestamp_path.split("."):
        if not isinstance(current, dict) or part not in current:
            logger.warning(
                "timestamp_path not found path=%s payload=%s",
                timestamp_path,
                raw_payload,
            )
            return _to_utc(fallback_ts)
        current = current[part]

    parsed = _coerce_datetime(current)
    if parsed is None:
        logger.warning(
            "timestamp_path value is not a valid datetime path=%s value=%s",
            timestamp_path,
            current,
        )
        return _to_utc(fallback_ts)
    return parsed


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _to_utc(value)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _epoch_to_datetime(float(value))

    if isinstance(value, str):
        raw = value.strip()
        if raw == "":
            return None
        try:
            numeric = float(raw)
        except ValueError:
            numeric = None
        if numeric is not None:
            return _epoch_to_datetime(numeric)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return _to_utc(parsed)

    return None


def _epoch_to_datetime(value: float) -> datetime | None:
    try:
        seconds = value / 1000.0 if abs(value) > 1_000_000_000_000 else value
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        return _to_utc(parsed)
    except (OverflowError, OSError, ValueError):
        return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
