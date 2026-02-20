from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.repositories.emr_pipeline import (
    DEFAULT_MEASUREMENT_EMR_KEYS,
    create_measurement_sync_run,
    finish_measurement_sync_run,
    get_latest_emr_values,
    get_latest_measurement_sync_run,
    get_latest_power_samples,
)
from app.repositories.signal_backbone import list_latest_by_signal_keys
from app.services.eos_client import EosClient

SOC_SOURCE_KEYS: tuple[str, str] = ("battery_soc_percent", "battery_soc_pct")
SOC_ALIASES: dict[str, str] = {"battery_soc_pct": "battery_soc_percent"}


class EosMeasurementSyncService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker,
        eos_client: EosClient,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._eos_client = eos_client
        self._logger = logging.getLogger("app.eos_measurement_sync")
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="measurement-sync-force")

        self._lock = Lock()
        self._running = False
        self._next_due_ts: datetime | None = None
        self._last_error: str | None = None
        self._last_status: str | None = None
        self._last_run_id: int | None = None
        self._force_future: Future[None] | None = None

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._next_due_ts = datetime.now(timezone.utc)
        self._stop_event.clear()
        self._thread = Thread(target=self._loop, name="eos-measurement-sync", daemon=True)
        self._thread.start()
        self._logger.info(
            "started eos measurement sync enabled=%s interval_seconds=%s",
            self._settings.eos_measurement_sync_enabled,
            self._settings.eos_measurement_sync_seconds,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._executor.shutdown(wait=False, cancel_futures=False)
        with self._lock:
            self._running = False

    def request_force_sync(self) -> int:
        if not self._settings.eos_measurement_sync_enabled:
            raise RuntimeError("Measurement sync is disabled by configuration")

        with self._lock:
            force_future = self._force_future
            if force_future is not None and not force_future.done():
                raise RuntimeError("A measurement force sync is already in progress")

        with self._session_factory() as db:
            run_id = create_measurement_sync_run(db, trigger_source="force")
            db.commit()

        future = self._executor.submit(self._force_worker, run_id)
        with self._lock:
            self._force_future = future
            self._last_run_id = run_id
        return run_id

    def get_status_snapshot(self, db: Session) -> dict[str, Any]:
        latest = get_latest_measurement_sync_run(db)
        with self._lock:
            force_future = self._force_future
            return {
                "enabled": self._settings.eos_measurement_sync_enabled,
                "running": self._running and not self._stop_event.is_set(),
                "sync_seconds": self._settings.eos_measurement_sync_seconds,
                "next_due_ts": _to_iso(self._next_due_ts),
                "force_in_progress": bool(force_future and not force_future.done()),
                "last_run_id": self._last_run_id,
                "last_status": self._last_status,
                "last_error": self._last_error,
                "last_run": latest,
            }

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._settings.eos_measurement_sync_enabled:
                self._stop_event.wait(1.0)
                continue

            now = datetime.now(timezone.utc)
            with self._lock:
                next_due = self._next_due_ts
            if next_due is None or now >= next_due:
                run_id = self._create_periodic_run()
                try:
                    self._run_sync(run_id=run_id, trigger_source="periodic")
                except Exception:
                    self._logger.exception("periodic measurement sync failed run_id=%s", run_id)
                with self._lock:
                    self._next_due_ts = datetime.now(timezone.utc) + timedelta(
                        seconds=self._settings.eos_measurement_sync_seconds
                    )

            self._stop_event.wait(1.0)

    def _create_periodic_run(self) -> int:
        with self._session_factory() as db:
            run_id = create_measurement_sync_run(db, trigger_source="periodic")
            db.commit()
            return run_id

    def _force_worker(self, run_id: int) -> None:
        try:
            self._run_sync(run_id=run_id, trigger_source="force")
        except Exception:
            self._logger.exception("force measurement sync failed run_id=%s", run_id)

    def _run_sync(self, *, run_id: int, trigger_source: str) -> None:
        pushed_count = 0
        details: dict[str, Any] = {
            "trigger_source": trigger_source,
            "pushed": [],
            "failed": [],
            "skipped": [],
            "warnings": [],
            "blocked_reasons": [],
        }
        status = "ok"
        error_text: str | None = None

        try:
            with self._session_factory() as db:
                latest_power = get_latest_power_samples(db)
                latest_emr = get_latest_emr_values(db)
                latest_soc = list_latest_by_signal_keys(db, signal_keys=list(SOC_SOURCE_KEYS))

            payload_rows = _build_push_rows(
                latest_power=latest_power,
                latest_emr=latest_emr,
                latest_soc=latest_soc,
            )
            config = self._eos_client.get_config()
            available_measurement_keys: set[str] = set()
            try:
                available_measurement_keys = {
                    key.strip()
                    for key in self._eos_client.get_measurement_keys()
                    if isinstance(key, str) and key.strip() != ""
                }
            except Exception as exc:
                details["warnings"].append(f"measurement key registry lookup failed: {exc}")

            blocked_reasons, preflight_warnings, pushable_keys = _preflight_measurement_config(
                config=config,
                payload_rows=payload_rows,
                available_keys=available_measurement_keys,
            )
            details["warnings"] = sorted(set(details["warnings"] + preflight_warnings))
            if blocked_reasons:
                status = "blocked"
                details["blocked_reasons"] = blocked_reasons
                self._finish_run(
                    run_id=run_id,
                    status=status,
                    pushed_count=0,
                    details_json=details,
                    error_text=None,
                )
                self._set_runtime_status(run_id=run_id, status=status, error=None)
                return

            for row in payload_rows:
                key = str(row["key"])
                if key not in pushable_keys:
                    details["skipped"].append(
                        {"key": key, "reason": "key not available in EOS measurement registry"}
                    )
                    continue
                try:
                    self._eos_client.put_measurement_value(
                        key=key,
                        value=float(row["value"]),
                        datetime_utc=_to_utc(row["ts"]),
                    )
                    pushed_count += 1
                    details["pushed"].append(
                        {"key": row["key"], "ts": _to_iso(row["ts"]), "value": row["value"]}
                    )
                except Exception as exc:
                    status = "partial"
                    details["failed"].append(
                        {
                            "key": row["key"],
                            "ts": _to_iso(row["ts"]),
                            "value": row["value"],
                            "error": str(exc),
                        }
                    )

            self._finish_run(
                run_id=run_id,
                status=status,
                pushed_count=pushed_count,
                details_json=details,
                error_text=None,
            )
            self._set_runtime_status(run_id=run_id, status=status, error=None)
        except Exception as exc:
            error_text = str(exc)
            self._finish_run(
                run_id=run_id,
                status="error",
                pushed_count=pushed_count,
                details_json=details,
                error_text=error_text,
            )
            self._set_runtime_status(run_id=run_id, status="error", error=error_text)

    def _finish_run(
        self,
        *,
        run_id: int,
        status: str,
        pushed_count: int,
        details_json: dict[str, Any],
        error_text: str | None,
    ) -> None:
        with self._session_factory() as db:
            finish_measurement_sync_run(
                db,
                run_id=run_id,
                status=status,
                pushed_count=pushed_count,
                details_json=details_json,
                error_text=error_text,
            )
            db.commit()

    def _set_runtime_status(self, *, run_id: int, status: str, error: str | None) -> None:
        with self._lock:
            self._last_run_id = run_id
            self._last_status = status
            self._last_error = error


def _build_push_rows(
    *,
    latest_power: list[dict[str, Any]],
    latest_emr: list[dict[str, Any]],
    latest_soc: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in latest_power:
        ts = row.get("ts")
        value = row.get("value_w")
        key = row.get("key")
        if not isinstance(key, str) or ts is None or value is None:
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        rows.append({"key": key, "value": numeric_value, "ts": ts})

    for row in latest_emr:
        ts = row.get("ts")
        value = row.get("emr_kwh")
        key = row.get("emr_key")
        if not isinstance(key, str) or ts is None or value is None:
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        rows.append({"key": key, "value": numeric_value, "ts": ts})

    rows.extend(_build_soc_rows(latest_soc))
    return _dedupe_by_key(rows)


def _build_soc_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    present_keys: set[str] = set()

    for row in rows:
        key = row.get("signal_key")
        ts = row.get("last_ts")
        if not isinstance(key, str) or ts is None:
            continue
        value = _coerce_numeric_signal_value(row)
        if value is None:
            continue
        result.append({"key": key, "value": value, "ts": ts})
        present_keys.add(key)

    for row in list(result):
        source_key = row["key"]
        alias_key = SOC_ALIASES.get(source_key)
        if alias_key is None or alias_key in present_keys:
            continue
        result.append(
            {
                "key": alias_key,
                "value": row["value"],
                "ts": row["ts"],
            }
        )
        present_keys.add(alias_key)

    return result


def _coerce_numeric_signal_value(row: dict[str, Any]) -> float | None:
    value_num = row.get("last_value_num")
    if value_num is not None:
        try:
            return float(value_num)
        except (TypeError, ValueError):
            return None

    value_text = row.get("last_value_text")
    if value_text is not None:
        try:
            return float(value_text)
        except (TypeError, ValueError):
            return None
    return None


def _dedupe_by_key(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("key")
        ts = row.get("ts")
        if not isinstance(key, str) or ts is None:
            continue
        existing = best_by_key.get(key)
        if existing is None:
            best_by_key[key] = row
            continue
        existing_ts = existing.get("ts")
        if isinstance(existing_ts, datetime) and isinstance(ts, datetime):
            if ts >= existing_ts:
                best_by_key[key] = row
            continue
        best_by_key[key] = row
    return list(best_by_key.values())


def _preflight_measurement_config(
    *,
    config: dict[str, Any],
    payload_rows: list[dict[str, Any]],
    available_keys: set[str] | None,
) -> tuple[list[str], list[str], set[str]]:
    reasons: list[str] = []
    warnings: list[str] = []
    measurement = config.get("measurement")
    if not isinstance(measurement, dict):
        return ["EOS config has no 'measurement' object"], warnings, set()

    configured_emr_keys: set[str] = set()

    for field_name, required_defaults in DEFAULT_MEASUREMENT_EMR_KEYS.items():
        configured = measurement.get(field_name)
        if not isinstance(configured, list) or len(configured) == 0:
            reasons.append(f"measurement.{field_name} is missing or empty")
            continue
        configured_keys = [item.strip() for item in configured if isinstance(item, str) and item.strip() != ""]
        if len(configured_keys) == 0:
            reasons.append(f"measurement.{field_name} has no valid keys")
            continue
        configured_emr_keys.update(configured_keys)
        for required_key in required_defaults:
            if required_key not in configured_keys:
                warnings.append(
                    f"measurement.{field_name} does not include default key '{required_key}'"
                )

    normalized_available = {
        key.strip()
        for key in (available_keys or set())
        if isinstance(key, str) and key.strip() != ""
    }
    if normalized_available:
        pushable_keys = normalized_available
        for configured_key in sorted(configured_emr_keys):
            if configured_key not in pushable_keys:
                warnings.append(
                    f"configured EMR key '{configured_key}' is not available in EOS measurement registry"
                )
    else:
        pushable_keys = set(configured_emr_keys)
        warnings.append(
            "EOS measurement registry unavailable; fallback to configured EMR keys only."
        )

    for row in payload_rows:
        key = row.get("key")
        if isinstance(key, str) and key not in pushable_keys:
            warnings.append(f"EOS measurement registry does not contain '{key}'")

    return sorted(set(reasons)), sorted(set(warnings)), pushable_keys


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
