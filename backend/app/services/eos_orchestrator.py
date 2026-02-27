from __future__ import annotations

import json
import logging
import math
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable

from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.repositories.eos_runtime import (
    add_artifact,
    create_run,
    get_latest_artifact,
    get_runtime_preference,
    get_latest_successful_run_with_plan,
    get_run_by_eos_last_run_datetime,
    get_run_by_id,
    list_running_runs,
    replace_plan_instructions,
    upsert_runtime_preference,
    upsert_run_input_snapshot,
    update_run_status,
)
from app.repositories.emr_pipeline import get_latest_emr_values, get_latest_power_samples
from app.repositories.parameter_profiles import get_active_parameter_profile, get_current_draft_revision
from app.repositories.signal_backbone import infer_value_type, ingest_signal_measurement, list_latest_by_signal_keys
from app.services.eos_client import EosApiError, EosClient, EosHealthSnapshot

FORCE_MEASUREMENT_SOC_KEYS: tuple[str, str] = ("battery_soc_percent", "battery_soc_pct")
FORCE_MEASUREMENT_SOC_ALIASES: dict[str, str] = {"battery_soc_pct": "battery_soc_percent"}
_PREDICTION_SIGNAL_ALLOWLIST: set[str] = {
    "elecprice_marketprice_wh",
    "elecprice_marketprice_kwh",
    "pvforecast_ac_power",
    "pvforecastakkudoktor_ac_power_any",
    "loadforecast_power_w",
    "load_mean_adjusted",
    "load_mean",
    "loadakkudoktor_mean_power_w",
}
_PRICE_HISTORY_SIGNAL_KEY = "elecprice_marketprice_wh"
_EOS_RETRYABLE_STATUS_CODES: set[int] = {429, 502, 503, 504}
_EOS_RETRYABLE_TEXT_FRAGMENTS: tuple[str, ...] = (
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "temporarily unavailable",
    "service unavailable",
    "remote end closed connection",
    "name or service not known",
)
_AUTO_RUN_PREF_KEY = "auto_run_preset"
_AUTO_RUN_PRESET_SLOTS: dict[str, list[int]] = {
    "off": [],
    "15m": [0, 15, 30, 45],
    "30m": [0, 30],
    "60m": [0],
}
_AUTO_RUN_PRESET_ORDER: tuple[str, ...] = ("off", "15m", "30m", "60m")


class EosOrchestratorService:
    _PLAN_TYPE = "plan"
    _SOLUTION_TYPE = "solution"
    _HEALTH_TYPE = "health"
    _PREDICTION_KEYS_TYPE = "prediction_keys"
    _PREDICTION_SERIES_TYPE = "prediction_series"
    _PREDICTION_REFRESH_TYPE = "prediction_refresh"
    _RUN_NOTE_TYPE = "run_note"
    _PRICE_HISTORY_BACKFILL_TYPE = "price_history_backfill"
    _MEASUREMENT_PUSH_TYPE = "measurement_push"
    _LEGACY_REQUEST_TYPE = "legacy_optimize_request"
    _LEGACY_RESPONSE_TYPE = "legacy_optimize_response"

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
        self._logger = logging.getLogger("app.eos_orchestrator")

        self._stop_event = Event()
        self._collector_thread: Thread | None = None
        self._aligned_scheduler_thread: Thread | None = None
        self._force_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eos-force-run")

        self._lock = Lock()
        self._running = False
        self._last_poll_ts: datetime | None = None
        self._last_successful_sync_ts: datetime | None = None
        self._last_error: str | None = None
        self._last_observed_eos_run_datetime: datetime | None = None
        self._last_force_request_ts: datetime | None = None
        self._force_run_future: Future[None] | None = None
        self._aligned_scheduler_next_due_ts: datetime | None = None
        self._aligned_scheduler_last_trigger_ts: datetime | None = None
        self._aligned_scheduler_last_skip_reason: str | None = None
        self._price_backfill_last_check_ts: datetime | None = None
        self._price_backfill_last_attempt_ts: datetime | None = None
        self._price_backfill_last_success_ts: datetime | None = None
        self._price_backfill_last_status: str | None = None
        self._price_backfill_last_history_hours: float | None = None
        self._price_backfill_cooldown_until_ts: datetime | None = None
        self._artifact_warmup_first_seen_ts: datetime | None = None
        self._last_artifact_warmup_log_ts: datetime | None = None
        (
            self._auto_run_preset,
            self._auto_run_enabled,
            self._auto_run_minutes,
        ) = _default_auto_run_state_from_settings(settings)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True

        try:
            self._reconcile_interrupted_runs()
        except Exception:
            self._logger.exception("failed to reconcile interrupted runs at startup")

        self._stop_event.clear()
        try:
            self._load_auto_run_state_from_preferences()
        except Exception:
            self._logger.exception("failed to load runtime auto-run preferences at startup")

        if self._settings.eos_autoconfig_enable:
            try:
                self._set_runtime_mode_and_interval(
                    mode=self._settings.eos_autoconfig_mode,
                    interval_seconds=self._settings.eos_autoconfig_interval_seconds,
                )
            except Exception:
                self._logger.exception("failed to apply EOS autoconfig at startup")
        if self._is_auto_run_enabled():
            try:
                self._set_runtime_mode_and_interval(
                    mode=self._settings.eos_autoconfig_mode,
                    interval_seconds=self._settings.eos_aligned_scheduler_base_interval_seconds,
                )
            except Exception:
                self._logger.exception("failed to apply aligned scheduler base interval at startup")
        try:
            self._apply_safe_horizon_cap()
        except Exception:
            self._logger.exception("failed to apply EOS safe horizon cap at startup")

        self._collector_thread = Thread(
            target=self._collector_loop,
            name="eos-collector",
            daemon=True,
        )
        self._collector_thread.start()
        self._aligned_scheduler_thread = Thread(
            target=self._aligned_scheduler_loop,
            name="eos-aligned-scheduler",
            daemon=True,
        )
        self._aligned_scheduler_thread.start()
        self._logger.info(
            "started eos orchestrator poll_seconds=%s auto_run_preset=%s auto_run_enabled=%s auto_run_minutes=%s",
            self._settings.eos_sync_poll_seconds,
            self._auto_run_preset,
            self._auto_run_enabled,
            self._auto_run_minutes,
        )

    def _reconcile_interrupted_runs(self) -> None:
        now = datetime.now(timezone.utc)
        with self._session_factory() as db:
            interrupted = list_running_runs(db)
            if not interrupted:
                return
            for run in interrupted:
                interruption_note = "run interrupted by backend restart before completion"
                existing_error = _safe_str(run.error_text)
                merged_error = (
                    f"{existing_error}; {interruption_note}" if existing_error else interruption_note
                )
                update_run_status(
                    db,
                    run,
                    status="failed",
                    error_text=merged_error,
                    finished_at=now,
                )
        self._logger.warning(
            "reconciled interrupted runs at startup count=%s",
            len(interrupted),
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._collector_thread and self._collector_thread.is_alive():
            self._collector_thread.join(timeout=5.0)
        if self._aligned_scheduler_thread and self._aligned_scheduler_thread.is_alive():
            self._aligned_scheduler_thread.join(timeout=5.0)

        self._force_executor.shutdown(wait=False, cancel_futures=False)
        with self._lock:
            self._running = False

    def get_collector_status(self) -> dict[str, Any]:
        with self._lock:
            force_future = self._force_run_future
            return {
                "running": self._running and not self._stop_event.is_set(),
                "poll_seconds": self._settings.eos_sync_poll_seconds,
                "last_poll_ts": _to_iso(self._last_poll_ts),
                "last_successful_sync_ts": _to_iso(self._last_successful_sync_ts),
                "last_observed_eos_run_datetime": _to_iso(self._last_observed_eos_run_datetime),
                "force_run_in_progress": bool(force_future and not force_future.done()),
                "last_force_request_ts": _to_iso(self._last_force_request_ts),
                "last_error": self._last_error,
                "auto_run_preset": self._auto_run_preset,
                "auto_run_enabled": self._auto_run_enabled,
                "auto_run_interval_minutes": _auto_run_interval_minutes_for_preset(self._auto_run_preset),
                "aligned_scheduler_enabled": self._auto_run_enabled,
                "aligned_scheduler_minutes": _format_aligned_minute_slots(self._auto_run_minutes),
                "aligned_scheduler_delay_seconds": self._settings.eos_aligned_scheduler_delay_seconds,
                "aligned_scheduler_next_due_ts": _to_iso(self._aligned_scheduler_next_due_ts),
                "aligned_scheduler_last_trigger_ts": _to_iso(self._aligned_scheduler_last_trigger_ts),
                "aligned_scheduler_last_skip_reason": self._aligned_scheduler_last_skip_reason,
                "price_backfill_last_check_ts": _to_iso(self._price_backfill_last_check_ts),
                "price_backfill_last_attempt_ts": _to_iso(self._price_backfill_last_attempt_ts),
                "price_backfill_last_success_ts": _to_iso(self._price_backfill_last_success_ts),
                "price_backfill_last_status": self._price_backfill_last_status,
                "price_backfill_last_history_hours": self._price_backfill_last_history_hours,
                "price_backfill_cooldown_until_ts": _to_iso(self._price_backfill_cooldown_until_ts),
            }

    def get_runtime_snapshot(self) -> dict[str, Any]:
        health_ok = False
        health_payload: dict[str, Any] | None = None
        config_payload: dict[str, Any] | None = None

        try:
            health = self._eos_client.get_health()
            health_payload = health.payload
            health_ok = True
        except Exception as exc:
            health_ok = False
            self._logger.warning("failed to fetch eos health snapshot: %s", exc)

        try:
            config = self._eos_client.get_config()
            if isinstance(config, dict):
                config_payload = config
        except Exception as exc:
            self._logger.warning("failed to fetch eos config snapshot: %s", exc)

        collector_status = self.get_collector_status()
        return {
            "eos_base_url": self._settings.eos_base_url,
            "health_ok": health_ok,
            "health_payload": health_payload,
            "config_payload": config_payload,
            "collector": {
                "running": bool(collector_status.get("running", False)),
                "poll_seconds": int(collector_status.get("poll_seconds", self._settings.eos_sync_poll_seconds)),
                "last_poll_ts": _parse_datetime(collector_status.get("last_poll_ts")),
                "last_successful_sync_ts": _parse_datetime(
                    collector_status.get("last_successful_sync_ts")
                ),
                "last_observed_eos_run_datetime": _parse_datetime(
                    collector_status.get("last_observed_eos_run_datetime")
                ),
                "force_run_in_progress": bool(collector_status.get("force_run_in_progress", False)),
                "last_force_request_ts": _parse_datetime(
                    collector_status.get("last_force_request_ts")
                ),
                "last_error": collector_status.get("last_error"),
                "auto_run_preset": str(
                    collector_status.get("auto_run_preset", "off")
                ),
                "auto_run_enabled": bool(
                    collector_status.get("auto_run_enabled", False)
                ),
                "auto_run_interval_minutes": _coerce_optional_int(
                    collector_status.get("auto_run_interval_minutes")
                ),
                "aligned_scheduler_enabled": bool(
                    collector_status.get("aligned_scheduler_enabled", False)
                ),
                "aligned_scheduler_minutes": str(
                    collector_status.get("aligned_scheduler_minutes", "")
                ),
                "aligned_scheduler_delay_seconds": int(
                    collector_status.get("aligned_scheduler_delay_seconds", 0)
                ),
                "aligned_scheduler_next_due_ts": _parse_datetime(
                    collector_status.get("aligned_scheduler_next_due_ts")
                ),
                "aligned_scheduler_last_trigger_ts": _parse_datetime(
                    collector_status.get("aligned_scheduler_last_trigger_ts")
                ),
                "aligned_scheduler_last_skip_reason": (
                    str(collector_status.get("aligned_scheduler_last_skip_reason"))
                    if collector_status.get("aligned_scheduler_last_skip_reason") is not None
                    else None
                ),
                "price_backfill_last_check_ts": _parse_datetime(
                    collector_status.get("price_backfill_last_check_ts")
                ),
                "price_backfill_last_attempt_ts": _parse_datetime(
                    collector_status.get("price_backfill_last_attempt_ts")
                ),
                "price_backfill_last_success_ts": _parse_datetime(
                    collector_status.get("price_backfill_last_success_ts")
                ),
                "price_backfill_last_status": (
                    str(collector_status.get("price_backfill_last_status"))
                    if collector_status.get("price_backfill_last_status") is not None
                    else None
                ),
                "price_backfill_last_history_hours": _coerce_float(
                    collector_status.get("price_backfill_last_history_hours")
                ),
                "price_backfill_cooldown_until_ts": _parse_datetime(
                    collector_status.get("price_backfill_cooldown_until_ts")
                ),
            },
        }

    def update_runtime_config(self, *, mode: str, interval_seconds: int) -> dict[str, str]:
        return self._set_runtime_mode_and_interval(mode=mode, interval_seconds=interval_seconds)

    def update_auto_run_preset(self, *, preset: str) -> dict[str, Any]:
        normalized = _normalize_auto_run_preset(preset)
        if normalized is None:
            raise ValueError("preset must be one of: off, 15m, 30m, 60m")

        enabled, minute_slots = _auto_run_preset_to_state(normalized)
        with self._session_factory() as db:
            upsert_runtime_preference(
                db,
                key=_AUTO_RUN_PREF_KEY,
                value_json={"preset": normalized},
            )

        with self._lock:
            previous_preset = self._auto_run_preset
            previous_minutes = list(self._auto_run_minutes)
            self._auto_run_preset = normalized
            self._auto_run_enabled = enabled
            self._auto_run_minutes = minute_slots
            if not enabled:
                self._aligned_scheduler_next_due_ts = None

        self._logger.info(
            "runtime auto-run preset updated old=%s new=%s old_slots=%s new_slots=%s source=api",
            previous_preset,
            normalized,
            previous_minutes,
            minute_slots,
        )
        return {
            "preset": normalized,
            "applied_slots": minute_slots,
            "enabled": enabled,
        }

    def _is_auto_run_enabled(self) -> bool:
        with self._lock:
            return self._auto_run_enabled

    def _load_auto_run_state_from_preferences(self) -> None:
        fallback_preset, fallback_enabled, fallback_minutes = _default_auto_run_state_from_settings(
            self._settings
        )
        chosen_preset = fallback_preset
        chosen_enabled = fallback_enabled
        chosen_minutes = list(fallback_minutes)

        preference_payload: Any | None = None
        try:
            with self._session_factory() as db:
                preference = get_runtime_preference(db, key=_AUTO_RUN_PREF_KEY)
                if preference is not None:
                    preference_payload = preference.value_json
        except Exception as exc:
            self._logger.warning("failed to read runtime auto-run preference, using fallback: %s", exc)

        persisted_preset = _extract_auto_run_preset_from_preference_value(preference_payload)
        if persisted_preset is not None:
            chosen_preset = persisted_preset
            chosen_enabled, chosen_minutes = _auto_run_preset_to_state(persisted_preset)

        with self._lock:
            self._auto_run_preset = chosen_preset
            self._auto_run_enabled = chosen_enabled
            self._auto_run_minutes = chosen_minutes
            if not chosen_enabled:
                self._aligned_scheduler_next_due_ts = None

    def request_force_run(self) -> int:
        return self._queue_force_like_run(
            trigger_source="force_run",
            run_mode="pulse_then_legacy",
            conflict_message="A manual run is already in progress",
        )

    def _queue_force_like_run(
        self,
        *,
        trigger_source: str,
        run_mode: str,
        conflict_message: str,
    ) -> int:
        with self._lock:
            existing_future = self._force_run_future
            if existing_future and not existing_future.done():
                raise RuntimeError(conflict_message)

        warmup_reason = self._artifact_warmup_block_reason()
        if warmup_reason is not None:
            raise RuntimeError(warmup_reason)

        with self._session_factory() as db:
            running_runs = list_running_runs(db)
            if running_runs:
                running_ids = ", ".join(str(run.id) for run in running_runs)
                raise RuntimeError(f"{conflict_message} (active run ids: {running_ids})")
            run = create_run(
                db,
                trigger_source=trigger_source,
                run_mode=run_mode,
                eos_last_run_datetime=None,
                status="running",
            )
            run_id = run.id

        force_future = self._force_executor.submit(
            self._force_run_worker,
            run_id,
            trigger_source,
            run_mode,
        )
        with self._lock:
            self._force_run_future = force_future
            self._last_force_request_ts = datetime.now(timezone.utc)
        return run_id

    def request_prediction_refresh(self, *, scope: str) -> int:
        normalized_scope = scope.strip().lower()
        if normalized_scope not in {"all", "pv", "prices", "load"}:
            raise ValueError("scope must be one of: all, pv, prices, load")

        with self._lock:
            existing_future = self._force_run_future
            if existing_future and not existing_future.done():
                raise RuntimeError("A manual run is already in progress")

        with self._session_factory() as db:
            run = create_run(
                db,
                trigger_source="prediction_refresh",
                run_mode=f"prediction_refresh_{normalized_scope}",
                eos_last_run_datetime=None,
                status="running",
            )
            run_id = run.id

        refresh_future = self._force_executor.submit(
            self._prediction_refresh_worker,
            run_id,
            normalized_scope,
        )
        with self._lock:
            self._force_run_future = refresh_future
            self._last_force_request_ts = datetime.now(timezone.utc)
        return run_id

    def _aligned_scheduler_loop(self) -> None:
        delay_seconds = max(0, int(self._settings.eos_aligned_scheduler_delay_seconds))
        self._logger.info("aligned scheduler thread started delay_seconds=%s", delay_seconds)
        while not self._stop_event.is_set():
            with self._lock:
                enabled = self._auto_run_enabled
                minute_slots = list(self._auto_run_minutes)

            if not enabled:
                with self._lock:
                    self._aligned_scheduler_next_due_ts = None
                    self._aligned_scheduler_last_skip_reason = None
                if self._stop_event.wait(1.0):
                    return
                continue

            if not minute_slots:
                with self._lock:
                    self._aligned_scheduler_next_due_ts = None
                    self._aligned_scheduler_last_skip_reason = "invalid_minute_slots"
                self._logger.warning("aligned scheduler has no valid minute slots; waiting for preset update")
                if self._stop_event.wait(1.0):
                    return
                continue

            try:
                self._ensure_aligned_base_runtime()
            except Exception as exc:
                self._logger.warning("aligned scheduler base runtime ensure failed: %s", exc)

            now = datetime.now(timezone.utc)
            due = _next_aligned_due_utc(
                now=now,
                minute_slots=minute_slots,
                delay_seconds=delay_seconds,
            )
            with self._lock:
                self._aligned_scheduler_next_due_ts = due

            while not self._stop_event.is_set():
                now = datetime.now(timezone.utc)
                remaining_seconds = (due - now).total_seconds()
                if remaining_seconds <= 0:
                    break

                with self._lock:
                    current_enabled = self._auto_run_enabled
                    current_slots = list(self._auto_run_minutes)
                if not current_enabled or current_slots != minute_slots:
                    break
                if self._stop_event.wait(min(1.0, max(0.1, remaining_seconds))):
                    return

            if self._stop_event.is_set():
                return

            with self._lock:
                current_enabled = self._auto_run_enabled
                current_slots = list(self._auto_run_minutes)
            if not current_enabled or current_slots != minute_slots:
                continue

            with self._lock:
                self._aligned_scheduler_last_trigger_ts = datetime.now(timezone.utc)

            try:
                run_id = self._queue_force_like_run(
                    trigger_source="automatic",
                    run_mode="aligned_schedule",
                    conflict_message="A run is already in progress; aligned slot skipped",
                )
            except Exception as exc:
                message = _safe_str(exc) or "aligned_scheduler_skip"
                with self._lock:
                    self._aligned_scheduler_last_skip_reason = message
                self._logger.warning("aligned scheduler skipped slot: %s", message)
                continue

            with self._lock:
                self._aligned_scheduler_last_skip_reason = None
            self._logger.info("aligned scheduler triggered run_id=%s", run_id)

    def _ensure_aligned_base_runtime(self) -> None:
        if not self._is_auto_run_enabled():
            return
        config_payload = self._eos_client.get_config()
        if not isinstance(config_payload, dict):
            return

        desired_mode = self._settings.eos_autoconfig_mode
        desired_interval = int(self._settings.eos_aligned_scheduler_base_interval_seconds)
        current_mode = _extract_ems_mode(config_payload)
        current_interval = _extract_ems_interval(config_payload)

        if current_mode == desired_mode and current_interval == desired_interval:
            return

        self._set_runtime_mode_and_interval(mode=desired_mode, interval_seconds=desired_interval)
        self._logger.info(
            "aligned scheduler set EOS base runtime mode=%s interval=%s (previous mode=%s interval=%s)",
            desired_mode,
            desired_interval,
            current_mode,
            current_interval,
        )

    def _collector_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                self._last_poll_ts = datetime.now(timezone.utc)

            if self._is_auto_run_enabled():
                try:
                    self._ensure_aligned_base_runtime()
                except Exception as exc:
                    self._logger.warning("aligned scheduler base runtime ensure failed: %s", exc)

            try:
                self._poll_once()
                with self._lock:
                    self._last_successful_sync_ts = datetime.now(timezone.utc)
                    self._last_error = None
            except Exception as exc:
                self._logger.exception("collector poll failed")
                with self._lock:
                    self._last_error = str(exc)

            self._stop_event.wait(self._settings.eos_sync_poll_seconds)

    def _poll_once(self) -> None:
        health = self._eos_client.get_health()
        last_run_datetime = health.eos_last_run_datetime
        with self._lock:
            self._last_observed_eos_run_datetime = last_run_datetime

        if last_run_datetime is None:
            return
        if not self._is_auto_run_enabled():
            return

        with self._lock:
            force_future = self._force_run_future
            force_in_progress = bool(force_future and not force_future.done())
        if force_in_progress:
            return

        warmup_reason = self._artifact_warmup_block_reason(health_snapshot=health)
        if warmup_reason is not None:
            self._log_artifact_warmup_defer(
                source="automatic_poll",
                last_run_datetime=last_run_datetime,
                reason=warmup_reason,
            )
            return

        with self._session_factory() as db:
            if list_running_runs(db):
                return
            existing = get_run_by_eos_last_run_datetime(db, last_run_datetime)
            if existing is not None:
                return

        self._collect_run_for_last_datetime(
            last_run_datetime,
            trigger_source="automatic",
            run_mode=self._settings.eos_autoconfig_mode,
            existing_run_id=None,
        )

    def _collect_run_for_last_datetime(
        self,
        last_run_datetime: datetime,
        *,
        trigger_source: str,
        run_mode: str,
        existing_run_id: int | None,
        artifact_wait_seconds_override: int | None = None,
    ) -> int:
        run_id: int
        with self._session_factory() as db:
            if existing_run_id is not None:
                run = get_run_by_id(db, existing_run_id)
                if run is None:
                    run = create_run(
                        db,
                        trigger_source=trigger_source,
                        run_mode=run_mode,
                        eos_last_run_datetime=last_run_datetime,
                        status="running",
                    )
                else:
                    run.trigger_source = trigger_source
                    run.run_mode = run_mode
                    run.eos_last_run_datetime = last_run_datetime
                    run.status = "running"
                    run.error_text = None
                    run.finished_at = None
                    db.add(run)
                    db.commit()
                    db.refresh(run)
            else:
                existing = get_run_by_eos_last_run_datetime(db, last_run_datetime)
                if existing is not None:
                    return existing.id
                run = create_run(
                    db,
                    trigger_source=trigger_source,
                    run_mode=run_mode,
                    eos_last_run_datetime=last_run_datetime,
                    status="running",
                )
            run_id = run.id

        try:
            self._capture_run_input_snapshot(run_id=run_id)
        except Exception as exc:
            partial_reasons = [f"run input snapshot capture failed: {exc}"]
        else:
            partial_reasons = []
        effective_artifact_wait_seconds = artifact_wait_seconds_override
        if effective_artifact_wait_seconds is None and trigger_source == "force_run":
            effective_artifact_wait_seconds = min(
                15,
                max(0, int(self._settings.eos_run_artifact_wait_seconds)),
            )
        plan_payload: dict[str, Any] | list[Any] | None = None
        solution_payload: dict[str, Any] | list[Any] | None = None
        plan_missing_reason: str | None = None
        solution_missing_reason: str | None = None
        plan_missing_soft = False
        solution_missing_soft = False
        plan_valid_from: datetime | None = None
        plan_valid_until: datetime | None = None
        plan_instructions_payloads: list[dict[str, Any]] = []

        try:
            health_snapshot = self._eos_client.get_health()
            with self._session_factory() as db:
                add_artifact(
                    db,
                    run_id=run_id,
                    artifact_type=self._HEALTH_TYPE,
                    artifact_key="latest",
                    payload_json=_to_json_payload(health_snapshot.payload),
                    valid_from=None,
                    valid_until=None,
                )
        except Exception as exc:
            partial_reasons.append(f"health capture failed: {exc}")
            self._logger.warning("health artifact capture failed run_id=%s error=%s", run_id, exc)

        self._capture_prediction_artifacts(
            run_id=run_id,
            partial_reasons=partial_reasons,
            include_extended_series=False,
        )

        try:
            plan_payload, plan_missing_reason = self._fetch_optional_run_artifact_json(
                artifact_name="plan",
                fetcher=self._eos_client.get_plan,
                wait_seconds_override=effective_artifact_wait_seconds,
            )
            if plan_payload is not None:
                plan_id, plan_instructions_payloads = _extract_plan_instructions(plan_payload)
                plan_valid_from, plan_valid_until = _derive_instruction_window(plan_instructions_payloads)
                with self._session_factory() as db:
                    add_artifact(
                        db,
                        run_id=run_id,
                        artifact_type=self._PLAN_TYPE,
                        artifact_key="latest",
                        payload_json=plan_payload,
                        valid_from=plan_valid_from,
                        valid_until=plan_valid_until,
                    )
                    replace_plan_instructions(
                        db,
                        run_id=run_id,
                        instructions=plan_instructions_payloads,
                        plan_id=plan_id,
                    )
                    ingest_signal_measurement(
                        db,
                        signal_key="eos_plan.latest",
                        label="eos_plan.latest",
                        value_type="json",
                        canonical_unit=None,
                        value=plan_payload,
                        ts=datetime.now(timezone.utc),
                        quality_status="derived",
                        source_type="eos_plan",
                        run_id=run_id,
                        source_ref_id=None,
                        tags_json={"source": "eos", "artifact": "plan"},
                    )
            elif plan_missing_reason:
                plan_missing_soft = (
                    trigger_source == "automatic"
                    and _is_artifact_not_configured_reason(
                        artifact_name="plan",
                        reason=plan_missing_reason,
                    )
                )
                if not plan_missing_soft:
                    partial_reasons.append(f"plan unavailable: {plan_missing_reason}")
        except Exception as exc:
            partial_reasons.append(f"plan capture failed: {exc}")

        try:
            solution_payload, solution_missing_reason = self._fetch_optional_run_artifact_json(
                artifact_name="solution",
                fetcher=self._eos_client.get_solution,
                wait_seconds_override=effective_artifact_wait_seconds,
            )
            if solution_payload is not None:
                with self._session_factory() as db:
                    add_artifact(
                        db,
                        run_id=run_id,
                        artifact_type=self._SOLUTION_TYPE,
                        artifact_key="latest",
                        payload_json=solution_payload,
                        valid_from=plan_valid_from,
                        valid_until=plan_valid_until,
                    )
                    ingest_signal_measurement(
                        db,
                        signal_key="eos_solution.latest",
                        label="eos_solution.latest",
                        value_type="json",
                        canonical_unit=None,
                        value=solution_payload,
                        ts=datetime.now(timezone.utc),
                        quality_status="derived",
                        source_type="eos_solution",
                        run_id=run_id,
                        source_ref_id=None,
                        tags_json={"source": "eos", "artifact": "solution"},
                    )
            elif solution_missing_reason:
                solution_missing_soft = (
                    trigger_source == "automatic"
                    and _is_artifact_not_configured_reason(
                        artifact_name="solution",
                        reason=solution_missing_reason,
                    )
                )
                if not solution_missing_soft:
                    partial_reasons.append(f"solution unavailable: {solution_missing_reason}")
        except Exception as exc:
            partial_reasons.append(f"solution capture failed: {exc}")

        if plan_missing_soft or solution_missing_soft:
            # For aligned automatic slots, EOS may report that plan/solution are not configured
            # even though the runtime itself is healthy. Keep the run successful and persist
            # this as warning context instead of downgrading run status.
            if plan_missing_soft and solution_missing_soft:
                notes: list[str] = []
                if plan_missing_reason:
                    notes.append(f"plan unavailable: {plan_missing_reason}")
                if solution_missing_reason:
                    notes.append(f"solution unavailable: {solution_missing_reason}")
                if notes:
                    self._persist_run_notes(
                        run_id=run_id,
                        note_key="automatic_artifact_unavailable",
                        severity="warning",
                        notes=notes,
                    )
            else:
                if plan_missing_soft and plan_missing_reason:
                    partial_reasons.append(f"plan unavailable: {plan_missing_reason}")
                if solution_missing_soft and solution_missing_reason:
                    partial_reasons.append(f"solution unavailable: {solution_missing_reason}")

        if (
            trigger_source == "force_run"
            and self._settings.eos_force_run_allow_legacy
            and plan_payload is None
            and solution_payload is None
        ):
            try:
                legacy_response = self._run_legacy_optimize(run_id=run_id)
                legacy_solution = _to_json_payload(legacy_response)
                with self._session_factory() as db:
                    add_artifact(
                        db,
                        run_id=run_id,
                        artifact_type=self._SOLUTION_TYPE,
                        artifact_key="legacy_fallback",
                        payload_json=legacy_solution,
                        valid_from=plan_valid_from,
                        valid_until=plan_valid_until,
                    )
                    ingest_signal_measurement(
                        db,
                        signal_key="eos_solution.latest",
                        label="eos_solution.latest",
                        value_type="json",
                        canonical_unit=None,
                        value=legacy_solution,
                        ts=datetime.now(timezone.utc),
                        quality_status="derived",
                        source_type="eos_solution",
                        run_id=run_id,
                        source_ref_id=None,
                        tags_json={"source": "eos", "artifact": "legacy_optimize_response"},
                    )
                solution_payload = legacy_solution
                partial_reasons = [
                    reason
                    for reason in partial_reasons
                    if not reason.startswith("solution unavailable:")
                ]
                partial_reasons.append("legacy optimize fallback used (no automatic plan available)")
            except Exception as exc:
                partial_reasons.append(f"legacy optimize fallback failed: {exc}")
                self._logger.warning(
                    "legacy optimize fallback failed run_id=%s error=%s",
                    run_id,
                    exc,
                )

        status = "success" if not partial_reasons else "partial"
        error_text = None if not partial_reasons else "; ".join(partial_reasons)
        with self._session_factory() as db:
            run = get_run_by_id(db, run_id)
            if run is not None:
                update_run_status(
                    db,
                    run,
                    status=status,
                    error_text=error_text,
                    finished_at=datetime.now(timezone.utc),
                )
        return run_id

    def _capture_run_input_snapshot(self, *, run_id: int) -> None:
        runtime_config_snapshot: dict[str, Any]
        try:
            config_payload = self._eos_client.get_config()
            runtime_config_snapshot = config_payload if isinstance(config_payload, dict) else {}
        except Exception:
            runtime_config_snapshot = {}

        with self._session_factory() as db:
            active_profile = get_active_parameter_profile(db)
            draft_revision = (
                get_current_draft_revision(db, profile_id=active_profile.id) if active_profile else None
            )
            parameter_payload = draft_revision.payload_json if draft_revision else {}
            previous_successful_run = get_latest_successful_run_with_plan(db)
            previous_successful_summary: dict[str, Any] | None = None
            if previous_successful_run is not None and previous_successful_run.id != run_id:
                previous_successful_summary = {
                    "run_id": previous_successful_run.id,
                    "trigger_source": previous_successful_run.trigger_source,
                    "run_mode": previous_successful_run.run_mode,
                    "started_at": _to_iso(previous_successful_run.started_at),
                    "finished_at": _to_iso(previous_successful_run.finished_at),
                }

            now = datetime.now(timezone.utc)
            assembled = {
                "captured_at": now.isoformat(),
                "run_id": run_id,
                "parameters_payload": parameter_payload,
                "mapped_inputs": {},
                "runtime_config": runtime_config_snapshot,
                "previous_successful_run": previous_successful_summary,
            }

            upsert_run_input_snapshot(
                db,
                run_id=run_id,
                parameter_profile_id=active_profile.id if active_profile else None,
                parameter_revision_id=draft_revision.id if draft_revision else None,
                parameter_payload_json=_to_json_payload(parameter_payload),
                mappings_snapshot_json=_to_json_payload({"items": []}),
                live_state_snapshot_json=_to_json_payload({}),
                runtime_config_snapshot_json=_to_json_payload(runtime_config_snapshot),
                assembled_eos_input_json=_to_json_payload(assembled),
            )

    def _prediction_refresh_worker(self, run_id: int, scope: str) -> None:
        partial_reasons: list[str] = []
        try:
            try:
                self._capture_run_input_snapshot(run_id=run_id)
            except Exception as exc:
                partial_reasons.append(f"run input snapshot capture failed: {exc}")

            try:
                refresh_details = self._trigger_prediction_refresh(scope=scope)
            except Exception as exc:
                raise RuntimeError(f"prediction refresh failed: {exc}") from exc
            global_error = _safe_str(refresh_details.get("global_error"))
            if global_error:
                partial_reasons.append(f"global prediction update failed: {global_error}")
            failed_updates = refresh_details.get("failed")
            if isinstance(failed_updates, list) and failed_updates:
                failed_notes: list[str] = []
                for item in failed_updates:
                    if not isinstance(item, dict):
                        continue
                    provider_id = _safe_str(item.get("provider_id"))
                    error_text = _safe_str(item.get("error"))
                    if provider_id and error_text:
                        failed_notes.append(f"{provider_id}: {error_text}")
                if failed_notes:
                    partial_reasons.append(
                        "prediction refresh failures: " + " | ".join(failed_notes)
                    )
            backfill_details_raw = refresh_details.get("price_history_backfill")
            backfill_details = (
                backfill_details_raw if isinstance(backfill_details_raw, dict) else None
            )
            if backfill_details is not None:
                backfill_error = _safe_str(backfill_details.get("error"))
                backfill_applied = bool(backfill_details.get("applied"))
                backfill_success = bool(backfill_details.get("success"))
                if backfill_error:
                    partial_reasons.append(f"price history backfill: {backfill_error}")
                elif backfill_applied and not backfill_success:
                    partial_reasons.append(
                        "price history backfill did not reach target history coverage"
                    )

            with self._session_factory() as db:
                add_artifact(
                    db,
                    run_id=run_id,
                    artifact_type=self._PREDICTION_REFRESH_TYPE,
                    artifact_key=scope,
                    payload_json=_to_json_payload(refresh_details),
                    valid_from=None,
                    valid_until=None,
                )
                if backfill_details is not None:
                    add_artifact(
                        db,
                        run_id=run_id,
                        artifact_type=self._PRICE_HISTORY_BACKFILL_TYPE,
                        artifact_key=scope,
                        payload_json=_to_json_payload(backfill_details),
                        valid_from=None,
                        valid_until=None,
                    )

            self._capture_prediction_artifacts(run_id=run_id, partial_reasons=partial_reasons)

            with self._session_factory() as db:
                run = get_run_by_id(db, run_id)
                if run is not None:
                    update_run_status(
                        db,
                        run,
                        status="success" if not partial_reasons else "partial",
                        error_text=None if not partial_reasons else "; ".join(partial_reasons),
                        finished_at=datetime.now(timezone.utc),
                    )
        except Exception as exc:
            self._logger.exception("prediction refresh run failed run_id=%s scope=%s", run_id, scope)
            with self._session_factory() as db:
                run = get_run_by_id(db, run_id)
                if run is not None:
                    update_run_status(
                        db,
                        run,
                        status="failed",
                        error_text=str(exc),
                        finished_at=datetime.now(timezone.utc),
                    )

    def _trigger_prediction_refresh(self, *, scope: str) -> dict[str, Any]:
        normalized_scope = scope.strip().lower()
        provider_ids = self._resolve_prediction_scope_providers(scope=normalized_scope)
        refreshed: list[str] = []
        failed: list[dict[str, str]] = []
        fallback_applied: list[dict[str, Any]] = []
        fallback_restore: list[dict[str, Any]] = []
        feedin_spot_sync: dict[str, Any] | None = None
        price_history_backfill: dict[str, Any] | None = None
        global_error: str | None = None

        if normalized_scope == "all":
            try:
                self._call_eos_with_retry(
                    action="prediction.update_all",
                    call=lambda: self._eos_client.trigger_prediction_update(
                        force_update=True,
                        force_enable=False,
                    ),
                )
                refreshed = provider_ids
            except Exception as exc:
                global_error = _summarize_exception(exc)
                fallback_result = self._attempt_pv_import_fallback(
                    error_text=global_error,
                    scope=normalized_scope,
                    failed_provider_id="*",
                )
                if fallback_result is not None:
                    fallback_applied.append(fallback_result)
                    if bool(fallback_result.get("applied")):
                        try:
                            self._call_eos_with_retry(
                                action="prediction.update_all.retry_after_fallback",
                                call=lambda: self._eos_client.trigger_prediction_update(
                                    force_update=True,
                                    force_enable=False,
                                ),
                            )
                            refreshed = provider_ids
                            global_error = None
                        except Exception as retry_exc:
                            retry_error = _summarize_exception(retry_exc)
                            failed.append(
                                {
                                    "provider_id": "*",
                                    "error": (
                                        f"{global_error}; retry_after_fallback_failed: {retry_error}"
                                    ),
                                }
                            )
                    else:
                        note = _safe_str(fallback_result.get("note"))
                        failed.append(
                            {
                                "provider_id": "*",
                                "error": (
                                    f"{global_error}; fallback_not_applied: {note}"
                                    if note
                                    else global_error
                                ),
                            }
                        )
                else:
                    failed.append({"provider_id": "*", "error": global_error})

            if global_error is None and not failed:
                try:
                    feedin_spot_sync = self._sync_feedin_spot_import_from_elecprice(
                        scope=normalized_scope
                    )
                except Exception as exc:
                    feedin_spot_sync = {
                        "applied": False,
                        "note": f"feed-in spot sync failed: {_summarize_exception(exc)}",
                    }
                try:
                    price_history_backfill = self._maybe_backfill_price_history(
                        scope=normalized_scope
                    )
                except Exception as exc:
                    price_history_backfill = {
                        "applied": False,
                        "success": False,
                        "error": _summarize_exception(exc),
                        "status": "error",
                    }

            fallback_restore = self._restore_pv_provider_after_fallback(
                fallback_applied=fallback_applied
            )

            return {
                "scope": normalized_scope,
                "force_update": True,
                "force_enable": False,
                "providers": refreshed,
                "failed": failed,
                "global_error": global_error,
                "fallback_applied": fallback_applied,
                "fallback_restore": fallback_restore,
                "feedin_spot_sync": feedin_spot_sync,
                "price_history_backfill": price_history_backfill,
            }

        for provider_id in provider_ids:
            try:
                self._call_eos_with_retry(
                    action=f"prediction.update_provider.{provider_id}",
                    call=lambda provider_id=provider_id: self._eos_client.trigger_prediction_update_provider(
                        provider_id=provider_id,
                        force_update=True,
                        force_enable=False,
                    ),
                )
                refreshed.append(provider_id)
            except Exception as exc:
                error_text = _summarize_exception(exc)
                fallback_result = self._attempt_pv_import_fallback(
                    error_text=error_text,
                    scope=normalized_scope,
                    failed_provider_id=provider_id,
                )
                if fallback_result is not None:
                    fallback_applied.append(fallback_result)
                    if bool(fallback_result.get("applied")):
                        try:
                            self._call_eos_with_retry(
                                action=f"prediction.update_provider.{provider_id}.retry_after_fallback",
                                call=lambda provider_id=provider_id: self._eos_client.trigger_prediction_update_provider(
                                    provider_id=provider_id,
                                    force_update=True,
                                    force_enable=False,
                                ),
                            )
                            refreshed.append(provider_id)
                            continue
                        except Exception as retry_exc:
                            retry_error = _summarize_exception(retry_exc)
                            failed.append(
                                {
                                    "provider_id": provider_id,
                                    "error": (
                                        f"{error_text}; retry_after_fallback_failed: {retry_error}"
                                    ),
                                }
                            )
                            continue
                    note = _safe_str(fallback_result.get("note"))
                    failed.append(
                        {
                            "provider_id": provider_id,
                            "error": (
                                f"{error_text}; fallback_not_applied: {note}"
                                if note
                                else error_text
                            ),
                        }
                    )
                    continue
                failed.append({"provider_id": provider_id, "error": error_text})

        if not failed:
            try:
                feedin_spot_sync = self._sync_feedin_spot_import_from_elecprice(
                    scope=normalized_scope
                )
            except Exception as exc:
                feedin_spot_sync = {
                    "applied": False,
                    "note": f"feed-in spot sync failed: {_summarize_exception(exc)}",
                }
            try:
                price_history_backfill = self._maybe_backfill_price_history(
                    scope=normalized_scope
                )
            except Exception as exc:
                price_history_backfill = {
                    "applied": False,
                    "success": False,
                    "error": _summarize_exception(exc),
                    "status": "error",
                }

        fallback_restore = self._restore_pv_provider_after_fallback(
            fallback_applied=fallback_applied
        )

        return {
            "scope": normalized_scope,
            "force_update": True,
            "force_enable": False,
            "providers": refreshed,
            "failed": failed,
            "global_error": global_error,
            "fallback_applied": fallback_applied,
            "fallback_restore": fallback_restore,
            "feedin_spot_sync": feedin_spot_sync,
            "price_history_backfill": price_history_backfill,
        }

    def _maybe_backfill_price_history(self, *, scope: str) -> dict[str, Any] | None:
        if scope not in {"all", "prices"}:
            return None

        now = datetime.now(timezone.utc)
        target_hours = max(1, int(self._settings.eos_price_backfill_target_hours))
        min_history_hours = max(
            1,
            min(int(self._settings.eos_price_backfill_min_history_hours), target_hours),
        )

        before = self._collect_price_history_raw_metrics(
            target_hours=target_hours,
            now=now,
        )
        before_history_hours = _coerce_float(before.get("raw_history_hours")) or 0.0

        if not self._settings.eos_price_backfill_enabled:
            self._update_price_backfill_status(
                check_ts=now,
                status="disabled",
                history_hours=before_history_hours,
            )
            return {
                "applied": False,
                "success": False,
                "status": "disabled",
                "error": None,
                "cooldown_active": False,
                "target_hours": target_hours,
                "min_history_hours": min_history_hours,
                "before": before,
                "after": None,
            }

        if before_history_hours >= float(min_history_hours):
            self._update_price_backfill_status(
                check_ts=now,
                status="sufficient_history",
                history_hours=before_history_hours,
            )
            return {
                "applied": False,
                "success": True,
                "status": "sufficient_history",
                "error": None,
                "cooldown_active": False,
                "target_hours": target_hours,
                "min_history_hours": min_history_hours,
                "before": before,
                "after": before,
            }

        cooldown_until: datetime | None
        with self._lock:
            cooldown_until = self._price_backfill_cooldown_until_ts
        if cooldown_until is not None and cooldown_until > now:
            self._update_price_backfill_status(
                check_ts=now,
                status="cooldown_active",
                history_hours=before_history_hours,
                cooldown_until_ts=cooldown_until,
            )
            return {
                "applied": False,
                "success": False,
                "status": "cooldown_active",
                "error": None,
                "cooldown_active": True,
                "cooldown_until_ts": _to_iso(cooldown_until),
                "target_hours": target_hours,
                "min_history_hours": min_history_hours,
                "before": before,
                "after": None,
            }

        attempt_ts = now
        cooldown_until = attempt_ts + timedelta(
            seconds=int(self._settings.eos_price_backfill_cooldown_seconds)
        )
        self._update_price_backfill_status(
            check_ts=attempt_ts,
            attempt_ts=attempt_ts,
            status="running",
            history_hours=before_history_hours,
            cooldown_until_ts=cooldown_until,
        )

        restart_payload: dict[str, Any] | None = None
        feedin_spot_sync_after_backfill: dict[str, Any] | None = None
        try:
            restart_payload = self._call_eos_with_retry(
                action="price_backfill.restart_server",
                call=self._eos_client.restart_server,
                max_attempts=2,
                initial_delay_seconds=1.0,
            )
            recovered = self._wait_for_eos_recovery(
                timeout_seconds=int(self._settings.eos_price_backfill_restart_timeout_seconds)
            )
            if not recovered:
                raise RuntimeError(
                    "EOS recovery timeout after restart "
                    f"({int(self._settings.eos_price_backfill_restart_timeout_seconds)}s)"
                )

            refreshed_provider = self._resolve_price_provider_id()
            self._call_eos_with_retry(
                action=f"price_backfill.update_provider.{refreshed_provider}",
                call=lambda: self._eos_client.trigger_prediction_update_provider(
                    provider_id=refreshed_provider,
                    force_update=True,
                    force_enable=False,
                ),
            )
            try:
                feedin_spot_sync_after_backfill = self._sync_feedin_spot_import_from_elecprice(
                    scope="prices"
                )
            except Exception as exc:
                feedin_spot_sync_after_backfill = {
                    "applied": False,
                    "note": f"feed-in spot sync after backfill failed: {_summarize_exception(exc)}",
                }

            after = self._collect_price_history_after_backfill(
                target_hours=target_hours,
                min_history_hours=min_history_hours,
            )
            after_history_hours = _coerce_float(after.get("raw_history_hours")) or 0.0
            success = after_history_hours >= float(min_history_hours)
            status = "backfill_succeeded" if success else "backfill_insufficient"
            error_text = None if success else (
                f"history coverage still below target: {after_history_hours:.2f}h < {min_history_hours}h"
            )
            self._update_price_backfill_status(
                check_ts=datetime.now(timezone.utc),
                success_ts=datetime.now(timezone.utc) if success else None,
                status=status,
                history_hours=after_history_hours,
                cooldown_until_ts=cooldown_until,
            )
            return {
                "applied": True,
                "success": success,
                "status": status,
                "error": error_text,
                "cooldown_active": False,
                "cooldown_until_ts": _to_iso(cooldown_until),
                "target_hours": target_hours,
                "min_history_hours": min_history_hours,
                "before": before,
                "after": after,
                "restart": restart_payload,
                "provider_refreshed": refreshed_provider,
                "feedin_spot_sync_after_backfill": feedin_spot_sync_after_backfill,
            }
        except Exception as exc:
            error_text = _summarize_exception(exc)
            self._update_price_backfill_status(
                check_ts=datetime.now(timezone.utc),
                status="error",
                history_hours=before_history_hours,
                cooldown_until_ts=cooldown_until,
            )
            return {
                "applied": True,
                "success": False,
                "status": "error",
                "error": error_text,
                "cooldown_active": False,
                "cooldown_until_ts": _to_iso(cooldown_until),
                "target_hours": target_hours,
                "min_history_hours": min_history_hours,
                "before": before,
                "after": None,
                "restart": restart_payload,
                "feedin_spot_sync_after_backfill": feedin_spot_sync_after_backfill,
            }

    def _resolve_price_provider_id(self) -> str:
        providers = self._resolve_prediction_scope_providers(scope="prices")
        if not providers:
            raise RuntimeError("No price provider configured")
        return providers[0]

    def _collect_price_history_raw_metrics(
        self,
        *,
        target_hours: int,
        now: datetime,
    ) -> dict[str, Any]:
        window_start = now - timedelta(hours=target_hours)
        series_payload = self._call_eos_with_retry(
            action="price_backfill.collect_history_series",
            call=lambda: self._eos_client.get_prediction_series(
                key=_PRICE_HISTORY_SIGNAL_KEY,
                start_datetime=window_start,
                end_datetime=now,
            ),
        )
        points = _extract_prediction_points(series_payload)
        timestamps: list[datetime] = []
        for ts, value in points:
            if value is None:
                continue
            ts_utc = _to_utc(ts)
            if ts_utc < window_start or ts_utc > now:
                continue
            timestamps.append(ts_utc)
        timestamps.sort()

        oldest_ts = timestamps[0] if timestamps else None
        newest_ts = timestamps[-1] if timestamps else None
        history_hours = 0.0
        if oldest_ts is not None:
            history_hours = max(0.0, (now - oldest_ts).total_seconds() / 3600.0)

        return {
            "signal_key": _PRICE_HISTORY_SIGNAL_KEY,
            "target_hours": target_hours,
            "window_start": _to_iso(window_start),
            "window_end": _to_iso(now),
            "raw_point_count": len(timestamps),
            "oldest_ts": _to_iso(oldest_ts),
            "newest_ts": _to_iso(newest_ts),
            "raw_history_hours": history_hours,
        }

    def _collect_price_history_after_backfill(
        self,
        *,
        target_hours: int,
        min_history_hours: int,
    ) -> dict[str, Any]:
        best_metrics = self._collect_price_history_raw_metrics(
            target_hours=target_hours,
            now=datetime.now(timezone.utc),
        )
        best_history_hours = _coerce_float(best_metrics.get("raw_history_hours")) or 0.0
        if best_history_hours >= float(min_history_hours):
            return best_metrics

        settle_seconds = max(0, int(self._settings.eos_price_backfill_settle_seconds))
        if settle_seconds <= 0:
            return best_metrics

        deadline = datetime.now(timezone.utc) + timedelta(seconds=settle_seconds)
        while datetime.now(timezone.utc) < deadline:
            remaining = max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
            wait_seconds = min(5.0, remaining)
            if wait_seconds > 0.0 and self._stop_event.wait(wait_seconds):
                break

            candidate_metrics = self._collect_price_history_raw_metrics(
                target_hours=target_hours,
                now=datetime.now(timezone.utc),
            )
            candidate_history_hours = (
                _coerce_float(candidate_metrics.get("raw_history_hours")) or 0.0
            )
            best_point_count = int(best_metrics.get("raw_point_count") or 0)
            candidate_point_count = int(candidate_metrics.get("raw_point_count") or 0)
            if (
                candidate_history_hours > best_history_hours
                or (
                    math.isclose(candidate_history_hours, best_history_hours)
                    and candidate_point_count > best_point_count
                )
            ):
                best_metrics = candidate_metrics
                best_history_hours = candidate_history_hours
            if candidate_history_hours >= float(min_history_hours):
                break

        return best_metrics

    def _wait_for_eos_recovery(self, *, timeout_seconds: int) -> bool:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=max(1, timeout_seconds))
        while datetime.now(timezone.utc) < deadline:
            try:
                self._eos_client.get_health()
                return True
            except Exception:
                pass
            if self._stop_event.wait(2.0):
                break
        return False

    def _artifact_warmup_block_reason(
        self,
        *,
        health_snapshot: EosHealthSnapshot | None = None,
    ) -> str | None:
        grace_seconds = max(0, int(self._settings.eos_artifact_warmup_grace_seconds))
        if grace_seconds <= 0:
            return None

        warmup_active = self._is_plan_solution_warmup_state()
        now = datetime.now(timezone.utc)
        if not warmup_active:
            with self._lock:
                self._artifact_warmup_first_seen_ts = None
            return None

        with self._lock:
            if self._artifact_warmup_first_seen_ts is None:
                self._artifact_warmup_first_seen_ts = now
            first_seen = self._artifact_warmup_first_seen_ts

        if first_seen is None:
            return None

        snapshot = health_snapshot
        if snapshot is None:
            try:
                snapshot = self._eos_client.get_health()
            except Exception:
                snapshot = None
        energy_start = _extract_energy_management_start_datetime(
            snapshot.payload if snapshot is not None else None
        )

        # Guard against backend restarts causing repeated warm-up windows:
        # if EOS has already been running longer than the grace period,
        # do not block artifact collection.
        if energy_start is not None:
            startup_age_seconds = (now - energy_start).total_seconds()
            if startup_age_seconds > float(grace_seconds):
                with self._lock:
                    self._artifact_warmup_first_seen_ts = None
                return None

        elapsed_seconds = int(max(0.0, (now - first_seen).total_seconds()))
        if elapsed_seconds > grace_seconds:
            return None

        remaining_seconds = max(0, grace_seconds - elapsed_seconds)
        return (
            "EOS warm-up active after restart: plan/solution endpoints not ready yet "
            f"(first_seen={_to_iso(first_seen)}, energy-management start={_to_iso(energy_start)}, "
            f"grace_remaining_seconds={remaining_seconds})"
        )

    def _is_plan_solution_warmup_state(self) -> bool:
        try:
            self._eos_client.get_plan()
            return False
        except EosApiError as exc:
            if not _is_artifact_warmup_404(exc):
                return False
        except Exception:
            return False

        try:
            self._eos_client.get_solution()
            return False
        except EosApiError as exc:
            return _is_artifact_warmup_404(exc)
        except Exception:
            return False

    def _log_artifact_warmup_defer(
        self,
        *,
        source: str,
        last_run_datetime: datetime | None,
        reason: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            previous = self._last_artifact_warmup_log_ts
            if previous is not None and (now - previous).total_seconds() < 30.0:
                return
            self._last_artifact_warmup_log_ts = now
        self._logger.info(
            "deferred run capture source=%s eos_last_run_datetime=%s reason=%s",
            source,
            _to_iso(last_run_datetime),
            reason,
        )

    def _fetch_optional_run_artifact_json(
        self,
        *,
        artifact_name: str,
        fetcher: Callable[[], Any],
        wait_seconds_override: int | None = None,
    ) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
        wait_seconds = (
            max(0, int(wait_seconds_override))
            if wait_seconds_override is not None
            else max(0, int(self._settings.eos_run_artifact_wait_seconds))
        )
        poll_seconds = max(1, int(self._settings.eos_run_artifact_poll_seconds))
        deadline = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
        last_missing_reason: str | None = None

        while True:
            try:
                payload = self._call_eos_with_retry(
                    action=f"run_artifact.{artifact_name}",
                    call=fetcher,
                )
                return _to_json_payload(payload), None
            except EosApiError as exc:
                if exc.status_code != 404 and not _is_retryable_eos_exception(exc):
                    raise
                last_missing_reason = _summarize_eos_error(exc)
            except Exception as exc:
                if not _is_retryable_eos_exception(exc):
                    raise
                last_missing_reason = _summarize_exception(exc)

            now = datetime.now(timezone.utc)
            if now >= deadline:
                return None, last_missing_reason or "not available"

            remaining = max(0.0, (deadline - now).total_seconds())
            if self._stop_event.wait(min(float(poll_seconds), remaining)):
                return None, "collector stopping"

    def _call_eos_with_retry(
        self,
        *,
        action: str,
        call: Callable[[], Any],
        max_attempts: int = 3,
        initial_delay_seconds: float = 1.5,
    ) -> Any:
        attempts = max(1, int(max_attempts))
        delay_seconds = max(0.0, float(initial_delay_seconds))
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return call()
            except Exception as exc:
                last_exc = exc
                if attempt >= attempts or not _is_retryable_eos_exception(exc):
                    raise
                self._logger.warning(
                    "transient eos call failed action=%s attempt=%s/%s error=%s",
                    action,
                    attempt,
                    attempts,
                    _summarize_exception(exc),
                )
                if delay_seconds > 0.0 and self._stop_event.wait(delay_seconds):
                    break
                delay_seconds = min(8.0, delay_seconds * 2.0 if delay_seconds > 0.0 else 0.5)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"EOS retry aborted before first attempt for action={action}")

    def _update_price_backfill_status(
        self,
        *,
        check_ts: datetime | None = None,
        attempt_ts: datetime | None = None,
        success_ts: datetime | None = None,
        status: str | None = None,
        history_hours: float | None = None,
        cooldown_until_ts: datetime | None = None,
    ) -> None:
        with self._lock:
            if check_ts is not None:
                self._price_backfill_last_check_ts = check_ts
            if attempt_ts is not None:
                self._price_backfill_last_attempt_ts = attempt_ts
            if success_ts is not None:
                self._price_backfill_last_success_ts = success_ts
            if status is not None:
                self._price_backfill_last_status = status
            if history_hours is not None:
                self._price_backfill_last_history_hours = history_hours
            if cooldown_until_ts is not None:
                self._price_backfill_cooldown_until_ts = cooldown_until_ts

    def _apply_safe_horizon_cap(self) -> bool:
        safe_horizon_hours = max(0, int(self._settings.eos_visualize_safe_horizon_hours))

        config_payload = self._eos_client.get_config()
        if not isinstance(config_payload, dict):
            return False

        prediction_hours = _extract_prediction_hours(config_payload)
        optimization_horizon_hours = _extract_optimization_horizon_hours(config_payload)
        (
            profile_prediction_hours,
            profile_optimization_horizon_hours,
        ) = self._read_active_profile_horizon_targets()

        desired_prediction_hours = (
            profile_prediction_hours if profile_prediction_hours is not None else prediction_hours
        )
        desired_optimization_horizon_hours = (
            profile_optimization_horizon_hours
            if profile_optimization_horizon_hours is not None
            else optimization_horizon_hours
        )
        if safe_horizon_hours > 0:
            if desired_prediction_hours is not None:
                desired_prediction_hours = min(desired_prediction_hours, safe_horizon_hours)
            if desired_optimization_horizon_hours is not None:
                desired_optimization_horizon_hours = min(
                    desired_optimization_horizon_hours,
                    safe_horizon_hours,
                )

        updates: list[dict[str, Any]] = []

        if (
            desired_prediction_hours is not None
            and prediction_hours != desired_prediction_hours
        ):
            path = self._set_config_value(
                path_candidates=_prediction_hours_path_candidates(config_payload),
                value=desired_prediction_hours,
            )
            updates.append(
                {
                    "field": "prediction.hours",
                    "previous": prediction_hours,
                    "desired": desired_prediction_hours,
                    "profile": profile_prediction_hours,
                    "safe_cap": safe_horizon_hours if safe_horizon_hours > 0 else None,
                    "path": path,
                }
            )

        if (
            desired_optimization_horizon_hours is not None
            and optimization_horizon_hours != desired_optimization_horizon_hours
        ):
            path = self._set_config_value(
                path_candidates=_optimization_horizon_hours_path_candidates(config_payload),
                value=desired_optimization_horizon_hours,
            )
            updates.append(
                {
                    "field": "optimization.horizon_hours",
                    "previous": optimization_horizon_hours,
                    "desired": desired_optimization_horizon_hours,
                    "profile": profile_optimization_horizon_hours,
                    "safe_cap": safe_horizon_hours if safe_horizon_hours > 0 else None,
                    "path": path,
                }
            )

        if not updates:
            return False

        save_error: str | None = None
        try:
            self._eos_client.save_config_file()
        except Exception as exc:
            save_error = _summarize_exception(exc)

        if save_error:
            self._logger.warning(
                "synchronized EOS horizon settings but save_config_file failed cap=%s updates=%s error=%s",
                safe_horizon_hours,
                updates,
                save_error,
            )
        else:
            self._logger.warning(
                "synchronized EOS horizon settings cap=%s updates=%s",
                safe_horizon_hours,
                updates,
            )
        return True

    def _read_active_profile_horizon_targets(self) -> tuple[int | None, int | None]:
        try:
            with self._session_factory() as db:
                if db is None:
                    return None, None
                active_profile = get_active_parameter_profile(db)
                if active_profile is None:
                    return None, None
                draft_revision = get_current_draft_revision(db, profile_id=active_profile.id)
                payload_json = draft_revision.payload_json if draft_revision else None
        except Exception:
            return None, None
        if not isinstance(payload_json, dict):
            return None, None
        return (
            _extract_prediction_hours(payload_json),
            _extract_optimization_horizon_hours(payload_json),
        )

    def _sync_feedin_spot_import_from_elecprice(self, *, scope: str) -> dict[str, Any]:
        if not self._settings.eos_feedin_spot_mirror_enabled:
            return {"applied": False, "note": "disabled by EOS_FEEDIN_SPOT_MIRROR_ENABLED"}
        if scope not in {"all", "prices"}:
            return {"applied": False, "note": f"scope '{scope}' does not include price refresh"}

        config_payload = self._eos_client.get_config()
        if not isinstance(config_payload, dict):
            return {"applied": False, "note": "EOS config payload unavailable"}

        feed_provider = _extract_nested_string(config_payload, ("feedintariff", "provider"))
        if feed_provider != "FeedInTariffImport":
            return {
                "applied": False,
                "note": "feedintariff.provider is not FeedInTariffImport",
                "current_provider": feed_provider,
            }

        source_context: dict[str, Any]
        try:
            elec_series, source_context = self._load_marketprice_series_for_feedin(
                config_payload=config_payload
            )
        except Exception as exc:
            return {
                "applied": False,
                "note": f"unable to load market price series: {_summarize_exception(exc)}",
            }
        if not elec_series:
            return {
                "applied": False,
                "note": "elecprice_marketprice_wh prediction series unavailable",
            }

        generated_at = datetime.now(timezone.utc).isoformat()
        import_payload = {
            "feed_in_tariff_wh": elec_series,
            "source": "eos-webapp.elecprice_marketprice_wh_mirror",
            "generated_at": generated_at,
            "note": "Feed-in spot mirrored from ElecPrice market series for direct marketing mode.",
        }

        config_path = self._set_config_value(
            path_candidates=_feedintariff_import_json_path_candidates(config_payload),
            value=json.dumps(import_payload, separators=(",", ":")),
        )

        provider_refresh_error: str | None = None
        provider_refresh_applied = False
        try:
            self._eos_client.trigger_prediction_update_provider(
                provider_id="FeedInTariffImport",
                force_update=True,
                force_enable=False,
            )
            provider_refresh_applied = True
        except Exception as exc:
            provider_refresh_error = _summarize_exception(exc)

        save_error: str | None = None
        try:
            self._eos_client.save_config_file()
        except Exception as exc:
            save_error = _summarize_exception(exc)

        return {
            "applied": True,
            "source_key": "elecprice_marketprice_wh",
            "points": len(elec_series),
            "unique_values": len(set(elec_series)),
            "config_path": config_path,
            "provider_refresh_applied": provider_refresh_applied,
            "provider_refresh_error": provider_refresh_error,
            "save_error": save_error,
            "source_context": source_context,
        }

    def _load_marketprice_series_for_feedin(
        self,
        *,
        config_payload: dict[str, Any],
    ) -> tuple[list[float], dict[str, Any]]:
        current_provider = _extract_nested_string(config_payload, ("elecprice", "provider"))
        if current_provider != "ElecPriceImport":
            series_raw = self._eos_client.get_prediction_list(key="elecprice_marketprice_wh")
            return _extract_numeric_series(series_raw), {
                "current_provider": current_provider,
                "source_provider": current_provider,
                "temporary_provider_switch": False,
            }

        provider_path = self._set_config_value(
            path_candidates=_elecprice_provider_path_candidates(config_payload),
            value="ElecPriceEnergyCharts",
        )
        try:
            self._eos_client.trigger_prediction_update_provider(
                provider_id="ElecPriceEnergyCharts",
                force_update=True,
                force_enable=False,
            )
            series_raw = self._eos_client.get_prediction_list(key="elecprice_marketprice_wh")
            series = _extract_numeric_series(series_raw)
        finally:
            restore_errors: list[str] = []
            try:
                self._set_config_value(
                    path_candidates=_elecprice_provider_path_candidates(config_payload),
                    value="ElecPriceImport",
                )
            except Exception as exc:
                restore_errors.append(f"restore provider failed: {_summarize_exception(exc)}")
            if restore_errors:
                raise RuntimeError("; ".join(restore_errors))

        return series, {
            "current_provider": current_provider,
            "source_provider": "ElecPriceEnergyCharts",
            "temporary_provider_switch": True,
            "provider_path": provider_path,
        }

    def _attempt_pv_import_fallback(
        self,
        *,
        error_text: str,
        scope: str,
        failed_provider_id: str | None,
    ) -> dict[str, Any] | None:
        if not self._settings.eos_prediction_pv_import_fallback_enabled:
            return None
        if not _is_pv_akkudoktor_refresh_error(error_text, provider_id=failed_provider_id):
            return None

        fallback_provider = _safe_str(self._settings.eos_prediction_pv_import_provider) or "PVForecastImport"
        config_payload = self._eos_client.get_config()
        if not isinstance(config_payload, dict):
            return {
                "scope": scope,
                "failed_provider_id": failed_provider_id,
                "from_provider": None,
                "to_provider": fallback_provider,
                "applied": False,
                "note": "EOS config payload unavailable for fallback decision",
            }

        current_provider = _extract_nested_string(config_payload, ("pvforecast", "provider"))
        if current_provider == fallback_provider:
            return {
                "scope": scope,
                "failed_provider_id": failed_provider_id,
                "from_provider": current_provider,
                "to_provider": fallback_provider,
                "applied": False,
                "note": "pvforecast provider already set to fallback provider",
            }
        if current_provider != "PVForecastAkkudoktor":
            return {
                "scope": scope,
                "failed_provider_id": failed_provider_id,
                "from_provider": current_provider,
                "to_provider": fallback_provider,
                "applied": False,
                "note": "current pvforecast provider is not PVForecastAkkudoktor",
            }
        fallback_valid, fallback_note = _is_valid_pv_fallback_provider(
            config_payload, fallback_provider
        )
        if not fallback_valid:
            note = "fallback provider not configured with usable import data"
            if fallback_note:
                note = f"{note}: {fallback_note}"
            return {
                "scope": scope,
                "failed_provider_id": failed_provider_id,
                "from_provider": current_provider,
                "to_provider": fallback_provider,
                "applied": False,
                "note": note,
            }

        try:
            path = self._set_config_value(
                path_candidates=_pvforecast_provider_path_candidates(config_payload),
                value=fallback_provider,
            )
            self._logger.warning(
                "prediction refresh fallback applied scope=%s from=%s to=%s path=%s trigger=%s",
                scope,
                current_provider,
                fallback_provider,
                path,
                failed_provider_id,
            )
            return {
                "scope": scope,
                "failed_provider_id": failed_provider_id,
                "from_provider": current_provider,
                "to_provider": fallback_provider,
                "applied": True,
                "config_path": path,
                "note": "switched pvforecast provider to fallback",
            }
        except Exception as exc:
            self._logger.warning("failed to apply prediction refresh fallback: %s", exc)
            return {
                "scope": scope,
                "failed_provider_id": failed_provider_id,
                "from_provider": current_provider,
                "to_provider": fallback_provider,
                "applied": False,
                "note": str(exc),
            }

    def _restore_pv_provider_after_fallback(
        self,
        *,
        fallback_applied: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for entry in fallback_applied:
            if not isinstance(entry, dict):
                continue
            if not bool(entry.get("applied")):
                continue

            config_path = _safe_str(entry.get("config_path"))
            from_provider = _safe_str(entry.get("from_provider"))
            to_provider = _safe_str(entry.get("to_provider"))
            if not config_path or not from_provider:
                continue
            if from_provider == to_provider:
                continue

            pair = (config_path, from_provider)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            try:
                self._set_config_value(path_candidates=[config_path], value=from_provider)
                self._logger.info(
                    "prediction refresh fallback restored provider path=%s to=%s",
                    config_path,
                    from_provider,
                )
                results.append(
                    {
                        "config_path": config_path,
                        "from_provider": from_provider,
                        "to_provider": to_provider,
                        "status": "restored",
                    }
                )
            except Exception as exc:
                error_text = _summarize_exception(exc)
                self._logger.warning(
                    "prediction refresh fallback restore failed path=%s to=%s error=%s",
                    config_path,
                    from_provider,
                    error_text,
                )
                results.append(
                    {
                        "config_path": config_path,
                        "from_provider": from_provider,
                        "to_provider": to_provider,
                        "status": "restore_failed",
                        "error": error_text,
                    }
                )

        return results

    def _resolve_prediction_scope_providers(self, *, scope: str) -> list[str]:
        config_payload = self._call_eos_with_retry(
            action=f"prediction.resolve_providers.{scope}",
            call=self._eos_client.get_config,
        )
        if not isinstance(config_payload, dict):
            raise RuntimeError("EOS config payload unavailable")

        provider_candidates: list[str] = []
        if scope == "pv":
            provider_candidates.append(
                _extract_nested_string(config_payload, ("pvforecast", "provider"))
            )
        elif scope == "prices":
            provider_candidates.append(
                _extract_nested_string(config_payload, ("elecprice", "provider"))
            )
            provider_candidates.append(
                _extract_nested_string(config_payload, ("feedintariff", "provider"))
            )
        elif scope == "load":
            provider_candidates.append(_extract_nested_string(config_payload, ("load", "provider")))
        elif scope == "all":
            provider_candidates.append(_extract_nested_string(config_payload, ("pvforecast", "provider")))
            provider_candidates.append(_extract_nested_string(config_payload, ("elecprice", "provider")))
            provider_candidates.append(_extract_nested_string(config_payload, ("feedintariff", "provider")))
            provider_candidates.append(_extract_nested_string(config_payload, ("load", "provider")))
            provider_candidates.append(_extract_nested_string(config_payload, ("weather", "provider")))
        else:
            raise RuntimeError(f"Unsupported prediction refresh scope: {scope}")

        provider_ids: list[str] = []
        seen: set[str] = set()
        for raw_provider in provider_candidates:
            provider = (raw_provider or "").strip()
            if provider == "" or provider in seen:
                continue
            seen.add(provider)
            provider_ids.append(provider)

        if not provider_ids:
            raise RuntimeError(f"No provider configured for prediction refresh scope '{scope}'")
        return provider_ids

    def _capture_prediction_artifacts(
        self,
        *,
        run_id: int,
        partial_reasons: list[str],
        include_extended_series: bool = True,
    ) -> None:
        try:
            prediction_keys = self._call_eos_with_retry(
                action="prediction.capture.keys",
                call=self._eos_client.get_prediction_keys,
            )
            if not include_extended_series:
                fast_capture_keys = set(_PREDICTION_SIGNAL_ALLOWLIST) | {"date_time"}
                prediction_keys = [
                    key for key in prediction_keys if isinstance(key, str) and key in fast_capture_keys
                ]
            prediction_datetime_index: list[datetime] = []
            try:
                datetime_payload = self._call_eos_with_retry(
                    action="prediction.capture.date_time.series",
                    call=lambda: self._eos_client.get_prediction_series(key="date_time"),
                )
                prediction_datetime_index = _extract_prediction_datetime_index(datetime_payload)
                if not prediction_datetime_index:
                    datetime_fallback = self._call_eos_with_retry(
                        action="prediction.capture.date_time.list",
                        call=lambda: self._eos_client.get_prediction_list(key="date_time"),
                    )
                    prediction_datetime_index = _extract_prediction_datetime_index(datetime_fallback)
            except EosApiError as exc:
                if exc.status_code != 404:
                    self._logger.warning(
                        "failed to load prediction date_time index run_id=%s error=%s",
                        run_id,
                        _summarize_eos_error(exc),
                    )
            except Exception as exc:
                self._logger.warning(
                    "failed to parse prediction date_time index run_id=%s error=%s",
                    run_id,
                    exc,
                )

            with self._session_factory() as db:
                add_artifact(
                    db,
                    run_id=run_id,
                    artifact_type=self._PREDICTION_KEYS_TYPE,
                    artifact_key="latest",
                    payload_json=_to_json_payload({"keys": prediction_keys}),
                    valid_from=None,
                    valid_until=None,
                )

            for key in prediction_keys:
                try:
                    series_payload = self._call_eos_with_retry(
                        action=f"prediction.capture.series.{key}",
                        call=lambda key=key: self._eos_client.get_prediction_series(key=key),
                    )
                    points = _extract_prediction_points(series_payload)
                    if not points:
                        fallback_list = self._call_eos_with_retry(
                            action=f"prediction.capture.list.{key}",
                            call=lambda key=key: self._eos_client.get_prediction_list(key=key),
                        )
                        points = _extract_prediction_points(fallback_list)
                        if fallback_list:
                            series_payload = {"values": fallback_list}
                    if key == _PRICE_HISTORY_SIGNAL_KEY:
                        price_history_points = self._load_price_history_points_for_capture()
                        if price_history_points:
                            points = _merge_prediction_points(points, price_history_points)
                    aligned_points = points
                    if key in _PREDICTION_SIGNAL_ALLOWLIST and prediction_datetime_index:
                        # Keep explicit historical timestamps for price history.
                        # Aligning to date_time index would otherwise drop backfilled past points.
                        if key == _PRICE_HISTORY_SIGNAL_KEY:
                            indexed_points = []
                        else:
                            values = _extract_prediction_values(series_payload)
                            indexed_points = _align_prediction_values_to_datetimes(
                                values=values,
                                datetime_index=prediction_datetime_index,
                            )
                        if indexed_points:
                            aligned_points = indexed_points

                    with self._session_factory() as db:
                        add_artifact(
                            db,
                            run_id=run_id,
                            artifact_type=self._PREDICTION_SERIES_TYPE,
                            artifact_key=key,
                            payload_json=_to_json_payload(series_payload),
                            valid_from=points[0][0] if points else None,
                            valid_until=points[-1][0] if points else None,
                        )
                        if key in _PREDICTION_SIGNAL_ALLOWLIST:
                            for point_ts, point_value in aligned_points:
                                if point_value is None:
                                    continue
                                ingest_signal_measurement(
                                    db,
                                    signal_key=f"prediction.{key}",
                                    label=f"prediction.{key}",
                                    value_type=infer_value_type(point_value),
                                    canonical_unit=None,
                                    value=point_value,
                                    ts=point_ts,
                                    quality_status="derived",
                                    source_type="eos_prediction",
                                    run_id=run_id,
                                    source_ref_id=None,
                                    tags_json={"prediction_key": key, "source": "eos"},
                                )
                except EosApiError as exc:
                    if exc.status_code == 404:
                        partial_reasons.append(f"prediction series missing for key={key}")
                        continue
                    raise
        except Exception as exc:
            partial_reasons.append(f"prediction capture failed: {exc}")
            self._logger.warning("prediction capture failed run_id=%s error=%s", run_id, exc)

    def _load_price_history_points_for_capture(self) -> list[tuple[datetime, float | None]]:
        now = datetime.now(timezone.utc)
        target_hours = max(1, int(self._settings.eos_price_backfill_target_hours))
        window_start = now - timedelta(hours=target_hours)
        try:
            history_payload = self._call_eos_with_retry(
                action="prediction.capture.series.elecprice_marketprice_wh.history_window",
                call=lambda: self._eos_client.get_prediction_series(
                    key=_PRICE_HISTORY_SIGNAL_KEY,
                    start_datetime=window_start,
                    end_datetime=now,
                ),
            )
        except EosApiError as exc:
            if exc.status_code == 404:
                return []
            raise
        points = _extract_prediction_points(history_payload)
        return [
            (ts, value)
            for ts, value in points
            if window_start <= ts <= now
        ]

    def _persist_run_notes(
        self,
        *,
        run_id: int,
        note_key: str,
        severity: str,
        notes: list[str],
    ) -> None:
        if not notes:
            return
        payload = {
            "severity": severity,
            "notes": list(notes),
            "captured_at": _to_iso(datetime.now(timezone.utc)),
        }
        try:
            with self._session_factory() as db:
                add_artifact(
                    db,
                    run_id=run_id,
                    artifact_type=self._RUN_NOTE_TYPE,
                    artifact_key=note_key,
                    payload_json=_to_json_payload(payload),
                    valid_from=None,
                    valid_until=None,
                )
        except Exception as exc:
            self._logger.warning(
                "failed to persist run note artifact run_id=%s key=%s severity=%s error=%s",
                run_id,
                note_key,
                severity,
                exc,
            )

    def _force_run_worker(
        self,
        run_id: int,
        trigger_source: str = "force_run",
        run_mode: str = "pulse_then_legacy",
    ) -> None:
        original_interval: int | None = None
        original_mode: str | None = None
        prior_last_run: datetime | None = None
        pre_force_soft_notes: list[str] = []
        pre_force_hard_notes: list[str] = []
        legacy_no_solution = False
        artifact_wait_seconds_override: int | None = None
        try:
            try:
                self._apply_safe_horizon_cap()
            except Exception as exc:
                pre_force_soft_notes.append(f"pre-force horizon cap update failed: {exc}")

            if self._settings.eos_force_run_pre_refresh_enabled:
                refresh_scope = _normalize_prediction_refresh_scope(
                    self._settings.eos_force_run_pre_refresh_scope
                )
                try:
                    refresh_details = self._trigger_prediction_refresh(scope=refresh_scope)
                    with self._session_factory() as db:
                        add_artifact(
                            db,
                            run_id=run_id,
                            artifact_type=self._PREDICTION_REFRESH_TYPE,
                            artifact_key=f"pre_force_{refresh_scope}",
                            payload_json=_to_json_payload(refresh_details),
                            valid_from=None,
                            valid_until=None,
                        )
                    global_error = _safe_str(refresh_details.get("global_error"))
                    failed_updates = refresh_details.get("failed")
                    if global_error:
                        pre_force_soft_notes.append(
                            f"pre-force prediction refresh global error: {global_error}"
                        )
                    elif isinstance(failed_updates, list) and failed_updates:
                        pre_force_soft_notes.append(
                            f"pre-force prediction refresh had {len(failed_updates)} provider failure(s)"
                        )
                except Exception as exc:
                    pre_force_soft_notes.append(f"pre-force prediction refresh failed: {exc}")

            try:
                measurement_push_details = self._push_latest_measurements_to_eos()
                with self._session_factory() as db:
                    add_artifact(
                        db,
                        run_id=run_id,
                        artifact_type=self._MEASUREMENT_PUSH_TYPE,
                        artifact_key="pre_force_latest",
                        payload_json=_to_json_payload(measurement_push_details),
                        valid_from=None,
                        valid_until=None,
                    )
                failed_count = int(measurement_push_details.get("failed_count", 0))
                if failed_count > 0:
                    pre_force_soft_notes.append(
                        f"pre-force measurement push had {failed_count} failure(s)"
                    )
            except Exception as exc:
                pre_force_soft_notes.append(f"pre-force measurement push failed: {exc}")

            health_before = self._eos_client.get_health()
            prior_last_run = health_before.eos_last_run_datetime

            config_payload = self._eos_client.get_config()
            if isinstance(config_payload, dict):
                original_interval = _extract_ems_interval(config_payload)
                original_mode = _extract_ems_mode(config_payload)

            if original_interval is None:
                original_interval = self._settings.eos_autoconfig_interval_seconds
            if not original_mode:
                original_mode = self._settings.eos_autoconfig_mode

            self._set_runtime_mode_and_interval(mode=original_mode, interval_seconds=1)
            pulse_wait_seconds = max(1, min(60, int(self._settings.eos_force_run_timeout_seconds)))
            observed = self._wait_for_next_last_run_datetime(
                previous=prior_last_run,
                timeout_seconds=pulse_wait_seconds,
            )

            if observed is None and self._settings.eos_force_run_allow_legacy:
                self._logger.warning("force run pulse timeout reached; invoking legacy /optimize fallback")
                try:
                    self._run_legacy_optimize(run_id=run_id)
                except EosApiError as exc:
                    if _is_legacy_no_solution_error(exc):
                        legacy_no_solution = True
                        pre_force_soft_notes.append(
                            "legacy optimize returned no solution; falling back to best-effort artifact capture"
                        )
                        self._logger.warning(
                            "legacy optimize returned no solution run_id=%s; continuing with degraded run capture",
                            run_id,
                        )
                    else:
                        raise
                post_legacy_wait_seconds = 5 if legacy_no_solution else min(
                    20,
                    int(self._settings.eos_force_run_timeout_seconds),
                )
                observed = self._wait_for_next_last_run_datetime(
                    previous=prior_last_run,
                    timeout_seconds=max(1, post_legacy_wait_seconds),
                )
                if observed is None:
                    # EOS may successfully process legacy optimize but keep health timestamp unchanged.
                    # In that case, still collect artifacts for this run instead of failing with timeout.
                    health_after_legacy = self._eos_client.get_health()
                    fallback_last_run = health_after_legacy.eos_last_run_datetime or prior_last_run
                    if fallback_last_run is not None:
                        artifact_wait_seconds_override = 5
                        if legacy_no_solution:
                            pre_force_soft_notes.append(
                                "legacy optimize returned no solution and eos_last_run_datetime did not advance"
                            )
                        else:
                            pre_force_soft_notes.append(
                                "legacy optimize completed but eos_last_run_datetime did not advance"
                            )
                        observed = fallback_last_run

            if observed is None and legacy_no_solution:
                pre_force_hard_notes.append(
                    "no EOS run timestamp available after legacy optimize no-solution"
                )
                self._persist_run_notes(
                    run_id=run_id,
                    note_key="pre_force",
                    severity="warning",
                    notes=pre_force_soft_notes,
                )
                with self._session_factory() as db:
                    run = get_run_by_id(db, run_id)
                    if run is not None:
                        merged_error_notes: list[str] = []
                        existing_error = _safe_str(run.error_text)
                        if existing_error:
                            merged_error_notes.append(existing_error)
                        merged_error_notes.extend(pre_force_hard_notes)
                        merged_error_notes.extend(pre_force_soft_notes)
                        update_run_status(
                            db,
                            run,
                            status="partial",
                            error_text="; ".join(merged_error_notes) if merged_error_notes else None,
                            finished_at=datetime.now(timezone.utc),
                        )
                return

            if observed is None:
                raise RuntimeError(
                    f"Force run did not produce a new EOS run timestamp (pulse_wait={pulse_wait_seconds}s)"
                )

            self._collect_run_for_last_datetime(
                observed,
                trigger_source=trigger_source,
                run_mode=run_mode,
                existing_run_id=run_id,
                artifact_wait_seconds_override=artifact_wait_seconds_override,
            )

            if pre_force_soft_notes:
                self._persist_run_notes(
                    run_id=run_id,
                    note_key="pre_force",
                    severity="warning",
                    notes=pre_force_soft_notes,
                )

            if pre_force_soft_notes or pre_force_hard_notes:
                with self._session_factory() as db:
                    run = get_run_by_id(db, run_id)
                    if run is not None:
                        merged_notes: list[str] = []
                        existing_error = _safe_str(run.error_text)
                        if existing_error:
                            merged_notes.append(existing_error)

                        if run.status != "success":
                            merged_notes.extend(pre_force_hard_notes)
                            merged_notes.extend(pre_force_soft_notes)

                        next_error_text = "; ".join(merged_notes) if merged_notes else None
                        has_changes = False
                        if run.error_text != next_error_text:
                            run.error_text = next_error_text
                            has_changes = True

                        if run.status == "success" and pre_force_hard_notes:
                            run.status = "partial"
                            has_changes = True

                        if has_changes:
                            db.add(run)
                            db.commit()
        except Exception as exc:
            self._logger.exception("force run failed run_id=%s", run_id)
            with self._session_factory() as db:
                run = get_run_by_id(db, run_id)
                if run is not None:
                    update_run_status(
                        db,
                        run,
                        status="failed",
                        error_text=str(exc),
                        finished_at=datetime.now(timezone.utc),
                    )
        finally:
            if original_mode is not None and original_interval is not None:
                try:
                    self._set_runtime_mode_and_interval(
                        mode=original_mode,
                        interval_seconds=original_interval,
                    )
                except Exception:
                    self._logger.exception("failed to restore EOS runtime config after force run")

    def _push_latest_measurements_to_eos(self) -> dict[str, Any]:
        config_payload_raw = self._eos_client.get_config()
        config_payload = config_payload_raw if isinstance(config_payload_raw, dict) else {}
        soc_signal_keys = _build_soc_signal_key_hints(config_payload)

        with self._session_factory() as db:
            latest_power = get_latest_power_samples(db)
            latest_emr = get_latest_emr_values(db)
            latest_soc = list_latest_by_signal_keys(db, signal_keys=soc_signal_keys)

        available_keys = {
            key.strip()
            for key in self._eos_client.get_measurement_keys()
            if isinstance(key, str) and key.strip() != ""
        }

        payload_rows = _build_measurement_push_rows(
            latest_power=latest_power,
            latest_emr=latest_emr,
            latest_soc=latest_soc,
        )
        payload_rows.extend(
            _build_device_soc_measurement_rows(
                latest_soc=latest_soc,
                config_payload=config_payload,
                available_keys=available_keys,
            )
        )
        payload_rows = _dedupe_measurement_rows(payload_rows)

        details: dict[str, Any] = {
            "attempted_count": len(payload_rows),
            "available_keys_count": len(available_keys),
            "pushed": [],
            "skipped": [],
            "failed": [],
        }
        pushed_count = 0
        failed_count = 0

        for row in payload_rows:
            key = str(row["key"])
            if key not in available_keys:
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
                    {"key": key, "ts": _to_iso(row["ts"]), "value": row["value"]}
                )
            except Exception as exc:
                failed_count += 1
                details["failed"].append(
                    {
                        "key": key,
                        "ts": _to_iso(row["ts"]),
                        "value": row["value"],
                        "error": str(exc),
                    }
                )
        details["pushed_count"] = pushed_count
        details["failed_count"] = failed_count
        details["skipped_count"] = len(details["skipped"])
        return details

    def _wait_for_next_last_run_datetime(
        self,
        *,
        previous: datetime | None,
        timeout_seconds: int,
    ) -> datetime | None:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        while datetime.now(timezone.utc) < deadline:
            health = self._eos_client.get_health()
            current = health.eos_last_run_datetime
            if current is not None:
                if previous is None or current > previous:
                    return current
            if self._stop_event.wait(2.0):
                break
        return None

    def _run_legacy_optimize(self, *, run_id: int) -> dict[str, Any]:
        payload = self._build_legacy_optimize_payload()
        with self._session_factory() as db:
            add_artifact(
                db,
                run_id=run_id,
                artifact_type=self._LEGACY_REQUEST_TYPE,
                artifact_key="optimize_payload",
                payload_json=_to_json_payload(payload),
            )

        try:
            response = self._call_eos_with_retry(
                action="legacy_optimize.run",
                call=lambda: self._eos_client.run_optimize(payload=payload),
                max_attempts=2,
                initial_delay_seconds=1.0,
            )
        except EosApiError as exc:
            if (
                _is_legacy_no_solution_error(exc)
                and isinstance(payload.get("start_solution"), list)
                and payload.get("start_solution")
            ):
                retry_payload = dict(payload)
                retry_payload["start_solution"] = None
                with self._session_factory() as db:
                    add_artifact(
                        db,
                        run_id=run_id,
                        artifact_type=self._LEGACY_REQUEST_TYPE,
                        artifact_key="optimize_payload_without_warm_start",
                        payload_json=_to_json_payload(retry_payload),
                    )
                response = self._call_eos_with_retry(
                    action="legacy_optimize.run_without_warm_start",
                    call=lambda: self._eos_client.run_optimize(payload=retry_payload),
                    max_attempts=2,
                    initial_delay_seconds=1.0,
                )
            else:
                raise
        with self._session_factory() as db:
            add_artifact(
                db,
                run_id=run_id,
                artifact_type=self._LEGACY_RESPONSE_TYPE,
                artifact_key="optimize_response",
                payload_json=_to_json_payload(response),
            )
        return response

    def _build_legacy_optimize_payload(self) -> dict[str, Any]:
        prediction_map: dict[str, tuple[str, bool]] = {
            "pvforecast_ac_power": ("pv_prognose_wh", True),
            "elecprice_marketprice_wh": ("strompreis_euro_pro_wh", False),
            "loadforecast_power_w": ("gesamtlast", False),
            "feed_in_tariff_wh": ("einspeiseverguetung_euro_pro_wh", False),
        }
        ems_payload: dict[str, Any] = {}
        missing_keys: list[str] = []
        for prediction_key, (optimize_key, zero_fill_null) in prediction_map.items():
            values = self._eos_client.get_prediction_list(key=prediction_key)
            numeric_values = _normalize_legacy_series(
                values,
                zero_fill_null=zero_fill_null,
            )
            if not numeric_values:
                missing_keys.append(prediction_key)
                continue
            ems_payload[optimize_key] = numeric_values

        if missing_keys:
            raise RuntimeError(
                "Legacy optimize fallback missing prediction data for keys: "
                + ", ".join(sorted(missing_keys))
            )

        config_payload_raw = self._eos_client.get_config()
        config_payload = config_payload_raw if isinstance(config_payload_raw, dict) else {}
        _trim_legacy_series_to_common_length(ems_payload)
        safe_horizon_hours = max(0, int(self._settings.eos_visualize_safe_horizon_hours))
        if safe_horizon_hours > 0:
            _trim_legacy_series_to_max_length(
                ems_payload,
                max_length=safe_horizon_hours,
            )
        ems_payload["preis_euro_pro_wh_akku"] = _extract_battery_storage_cost_per_wh(config_payload)
        legacy_eauto = _extract_legacy_eauto(config_payload)
        start_solution = self._load_latest_legacy_start_solution()
        if (
            safe_horizon_hours > 0
            and isinstance(start_solution, list)
            and len(start_solution) > safe_horizon_hours
        ):
            start_solution = start_solution[:safe_horizon_hours]
        if start_solution is not None:
            self._logger.info("legacy optimize warm-start reused start_solution_len=%s", len(start_solution))

        return {
            "ems": ems_payload,
            "pv_akku": None,
            "inverter": None,
            "eauto": legacy_eauto,
            "start_solution": start_solution,
        }

    def _load_latest_legacy_start_solution(self) -> list[float] | None:
        try:
            with self._session_factory() as db:
                artifact = get_latest_artifact(
                    db,
                    artifact_type=self._LEGACY_RESPONSE_TYPE,
                    artifact_key="optimize_response",
                )
        except Exception as exc:
            self._logger.warning("failed to load legacy start_solution for warm-start: %s", exc)
            return None
        if artifact is None:
            return None

        payload = artifact.payload_json
        if not isinstance(payload, dict):
            return None

        return _extract_legacy_start_solution(payload)

    def _set_runtime_mode_and_interval(self, *, mode: str, interval_seconds: int) -> dict[str, str]:
        config_payload = self._eos_client.get_config()
        mode_candidates = _ems_mode_path_candidates(config_payload if isinstance(config_payload, dict) else None)
        interval_candidates = _ems_interval_path_candidates(
            config_payload if isinstance(config_payload, dict) else None
        )

        mode_path = self._set_config_value(path_candidates=mode_candidates, value=mode)
        interval_path = self._set_config_value(
            path_candidates=interval_candidates,
            value=int(interval_seconds),
        )
        return {"mode_path": mode_path, "interval_path": interval_path}

    def _set_config_value(self, *, path_candidates: list[str], value: Any) -> str:
        attempted: list[str] = []
        last_error: Exception | None = None
        for path in path_candidates:
            attempted.append(path)
            try:
                self._eos_client.put_config_path(path, value)
                return path
            except EosApiError as exc:
                last_error = exc
                continue
            except Exception as exc:
                last_error = exc
                continue

        if last_error is None:
            raise RuntimeError("No config path candidates available")
        raise RuntimeError(
            f"Failed to update EOS config path. attempted={attempted} error={last_error}"
        )

def _to_json_payload(value: Any) -> dict[str, Any] | list[Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return value
    return {"value": value}


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _extract_nested_string(payload: dict[str, Any], path: tuple[str, ...]) -> str | None:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _safe_str(current)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, (int, float)):
        raw = float(value)
        if not math.isfinite(raw):
            return None
        magnitude = abs(raw)
        if magnitude > 1e17:
            # nanoseconds
            timestamp_seconds = raw / 1e9
        elif magnitude > 1e14:
            # microseconds
            timestamp_seconds = raw / 1e6
        elif magnitude > 1e11:
            # milliseconds
            timestamp_seconds = raw / 1e3
        else:
            # seconds
            timestamp_seconds = raw
        try:
            return datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _extract_prediction_points(payload: Any) -> list[tuple[datetime, float | None]]:
    now = datetime.now(timezone.utc)

    if isinstance(payload, dict):
        data_candidate = payload.get("data")
        if isinstance(data_candidate, dict):
            points: list[tuple[datetime, float | None]] = []
            for raw_key, raw_value in data_candidate.items():
                key_ts = _parse_datetime(raw_key)
                if isinstance(raw_value, dict):
                    ts = (
                        _parse_datetime(raw_value.get("datetime"))
                        or _parse_datetime(raw_value.get("date_time"))
                        or _parse_datetime(raw_value.get("ts"))
                        or _parse_datetime(raw_value.get("timestamp"))
                        or _parse_datetime(raw_value.get("start_datetime"))
                        or key_ts
                    )
                    numeric_value = _extract_numeric_from_prediction_row(raw_value)
                else:
                    ts = key_ts
                    numeric_value = _coerce_float(raw_value)
                if ts is None:
                    continue
                points.append((ts, numeric_value))
            points.sort(key=lambda item: item[0])
            return points
        if isinstance(data_candidate, list):
            return _extract_prediction_points(data_candidate)
        series_candidate = payload.get("series")
        if isinstance(series_candidate, list):
            return _extract_prediction_points(series_candidate)
        values_candidate = payload.get("values")
        if isinstance(values_candidate, list):
            return _extract_prediction_points(values_candidate)

    if isinstance(payload, list):
        points: list[tuple[datetime, float | None]] = []
        for index, raw_item in enumerate(payload):
            if isinstance(raw_item, dict):
                ts = (
                    _parse_datetime(raw_item.get("datetime"))
                    or _parse_datetime(raw_item.get("date_time"))
                    or _parse_datetime(raw_item.get("ts"))
                    or _parse_datetime(raw_item.get("timestamp"))
                    or _parse_datetime(raw_item.get("start_datetime"))
                    or now + timedelta(minutes=15 * index)
                )
                numeric_value = _extract_numeric_from_prediction_row(raw_item)
                points.append((ts, numeric_value))
                continue

            ts = now + timedelta(minutes=15 * index)
            points.append((ts, _coerce_float(raw_item)))
        return points

    return []


def _extract_prediction_datetime_index(payload: Any) -> list[datetime]:
    if isinstance(payload, dict):
        data_candidate = payload.get("data")
        if isinstance(data_candidate, dict):
            datetimes: list[datetime] = []
            for raw_key, raw_value in data_candidate.items():
                key_ts = _parse_datetime(raw_key)
                if key_ts is not None:
                    datetimes.append(key_ts)
                    continue
                if isinstance(raw_value, dict):
                    ts = (
                        _parse_datetime(raw_value.get("datetime"))
                        or _parse_datetime(raw_value.get("date_time"))
                        or _parse_datetime(raw_value.get("ts"))
                        or _parse_datetime(raw_value.get("timestamp"))
                        or _parse_datetime(raw_value.get("start_datetime"))
                    )
                    if ts is not None:
                        datetimes.append(ts)
            datetimes.sort()
            return datetimes
        if isinstance(data_candidate, list):
            return _extract_prediction_datetime_index(data_candidate)
        series_candidate = payload.get("series")
        if isinstance(series_candidate, list):
            return _extract_prediction_datetime_index(series_candidate)
        values_candidate = payload.get("values")
        if isinstance(values_candidate, list):
            return _extract_prediction_datetime_index(values_candidate)

    if not isinstance(payload, list):
        return []

    datetimes: list[datetime] = []
    for raw_item in payload:
        if isinstance(raw_item, dict):
            ts = (
                _parse_datetime(raw_item.get("datetime"))
                or _parse_datetime(raw_item.get("date_time"))
                or _parse_datetime(raw_item.get("ts"))
                or _parse_datetime(raw_item.get("timestamp"))
                or _parse_datetime(raw_item.get("start_datetime"))
            )
        else:
            ts = _parse_datetime(raw_item)
        if ts is not None:
            datetimes.append(ts)
    return datetimes


def _extract_prediction_values(payload: Any) -> list[float | None]:
    if isinstance(payload, dict):
        data_candidate = payload.get("data")
        if isinstance(data_candidate, dict):
            keyed_values: list[tuple[datetime, float | None]] = []
            fallback_values: list[float | None] = []
            for raw_key, raw_value in data_candidate.items():
                ts = _parse_datetime(raw_key)
                if isinstance(raw_value, dict):
                    value = _extract_numeric_from_prediction_row(raw_value)
                else:
                    value = _coerce_float(raw_value)
                if ts is None:
                    fallback_values.append(value)
                    continue
                keyed_values.append((ts, value))
            if keyed_values:
                keyed_values.sort(key=lambda item: item[0])
                return [value for _, value in keyed_values]
            return fallback_values
        if isinstance(data_candidate, list):
            return _extract_prediction_values(data_candidate)
        series_candidate = payload.get("series")
        if isinstance(series_candidate, list):
            return _extract_prediction_values(series_candidate)
        values_candidate = payload.get("values")
        if isinstance(values_candidate, list):
            return _extract_prediction_values(values_candidate)

    if not isinstance(payload, list):
        return []

    values: list[float | None] = []
    for raw_item in payload:
        if isinstance(raw_item, dict):
            value = _extract_numeric_from_prediction_row(raw_item)
            values.append(_coerce_float(value))
            continue
        values.append(_coerce_float(raw_item))
    return values


def _merge_prediction_points(
    primary: list[tuple[datetime, float | None]],
    secondary: list[tuple[datetime, float | None]],
) -> list[tuple[datetime, float | None]]:
    merged: dict[datetime, float | None] = {}
    for ts, value in secondary:
        merged[_to_utc(ts)] = value
    for ts, value in primary:
        merged[_to_utc(ts)] = value
    return sorted(merged.items(), key=lambda item: item[0])


def _extract_numeric_from_prediction_row(raw_item: dict[str, Any]) -> float | None:
    if "value" in raw_item:
        return _coerce_float(raw_item.get("value"))
    if "y" in raw_item:
        return _coerce_float(raw_item.get("y"))
    if "v" in raw_item:
        return _coerce_float(raw_item.get("v"))

    preferred_keys = (
        _PRICE_HISTORY_SIGNAL_KEY,
        "pvforecast_ac_power",
        "pvforecastakkudoktor_ac_power_any",
        "loadforecast_power_w",
        "load_mean_adjusted",
        "load_mean",
        "loadakkudoktor_mean_power_w",
    )
    for key in preferred_keys:
        if key in raw_item:
            numeric = _coerce_float(raw_item.get(key))
            if numeric is not None:
                return numeric

    for value in raw_item.values():
        numeric = _coerce_float(value)
        if numeric is not None:
            return numeric
    return None


def _align_prediction_values_to_datetimes(
    *,
    values: list[float | None],
    datetime_index: list[datetime],
) -> list[tuple[datetime, float | None]]:
    if not values or not datetime_index:
        return []
    limit = min(len(values), len(datetime_index))
    return [(datetime_index[i], values[i]) for i in range(limit)]


def _extract_numeric_series(values: list[Any]) -> list[float]:
    numeric_values: list[float] = []
    for raw_value in values:
        number = _coerce_float(raw_value)
        if number is None:
            continue
        numeric_values.append(number)
    return numeric_values


def _normalize_legacy_series(values: list[Any], *, zero_fill_null: bool) -> list[float]:
    normalized: list[float] = []
    for raw_value in values:
        number = _coerce_float(raw_value)
        if number is None:
            if zero_fill_null:
                normalized.append(0.0)
            continue
        normalized.append(number)
    return normalized


def _trim_legacy_series_to_common_length(ems_payload: dict[str, Any]) -> None:
    series_keys = [
        "pv_prognose_wh",
        "strompreis_euro_pro_wh",
        "gesamtlast",
        "einspeiseverguetung_euro_pro_wh",
    ]
    lengths: list[int] = []
    for key in series_keys:
        series = ems_payload.get(key)
        if not isinstance(series, list):
            continue
        lengths.append(len(series))

    if not lengths:
        return

    target_length = min(lengths)
    if target_length <= 0:
        raise RuntimeError("Legacy optimize fallback produced empty series")

    for key in series_keys:
        series = ems_payload.get(key)
        if not isinstance(series, list):
            continue
        if len(series) > target_length:
            ems_payload[key] = series[:target_length]


def _trim_legacy_series_to_max_length(ems_payload: dict[str, Any], *, max_length: int) -> None:
    if max_length <= 0:
        return
    for key, value in list(ems_payload.items()):
        if not isinstance(value, list):
            continue
        if len(value) <= max_length:
            continue
        ems_payload[key] = value[:max_length]


def _extract_battery_storage_cost_per_wh(config_payload: dict[str, Any]) -> float:
    devices = config_payload.get("devices")
    if not isinstance(devices, dict):
        return 0.0

    batteries = devices.get("batteries")
    if not isinstance(batteries, list) or not batteries:
        return 0.0

    first_battery = batteries[0]
    if not isinstance(first_battery, dict):
        return 0.0

    levelized_cost_kwh = _coerce_float(first_battery.get("levelized_cost_of_storage_kwh"))
    if levelized_cost_kwh is None:
        return 0.0
    if levelized_cost_kwh < 0:
        return 0.0
    return levelized_cost_kwh / 1000.0


def _extract_legacy_eauto(config_payload: dict[str, Any]) -> dict[str, Any] | None:
    devices = config_payload.get("devices")
    if not isinstance(devices, dict):
        return None

    max_vehicles = _coerce_float(devices.get("max_electric_vehicles"))
    if max_vehicles is not None and max_vehicles <= 0:
        return None

    vehicles = devices.get("electric_vehicles")
    if not isinstance(vehicles, list) or not vehicles:
        return None

    for candidate in vehicles:
        payload = _normalize_legacy_eauto_candidate(candidate)
        if payload is not None:
            return payload
    return None


def _normalize_legacy_eauto_candidate(candidate: Any) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None

    device_id = _safe_str(candidate.get("device_id"))
    capacity_wh = _coerce_float(candidate.get("capacity_wh"))
    if not device_id or capacity_wh is None or capacity_wh <= 0:
        return None

    payload: dict[str, Any] = {
        "device_id": device_id,
        "capacity_wh": int(round(capacity_wh)),
    }

    hours = _coerce_float(candidate.get("hours"))
    if hours is not None and hours > 0:
        payload["hours"] = int(round(hours))

    max_charge_power_w = _coerce_float(candidate.get("max_charge_power_w"))
    if max_charge_power_w is not None and max_charge_power_w > 0:
        payload["max_charge_power_w"] = float(max_charge_power_w)

    min_soc = _coerce_float(candidate.get("min_soc_percentage"))
    if min_soc is not None:
        payload["min_soc_percentage"] = max(0, min(100, int(round(min_soc))))

    max_soc = _coerce_float(candidate.get("max_soc_percentage"))
    if max_soc is not None:
        payload["max_soc_percentage"] = max(0, min(100, int(round(max_soc))))

    initial_soc = _coerce_float(candidate.get("initial_soc_percentage"))
    if initial_soc is not None:
        payload["initial_soc_percentage"] = max(0, min(100, int(round(initial_soc))))

    charging_efficiency = _coerce_float(candidate.get("charging_efficiency"))
    if charging_efficiency is not None and 0 < charging_efficiency <= 1.0:
        payload["charging_efficiency"] = float(charging_efficiency)

    discharging_efficiency = _coerce_float(candidate.get("discharging_efficiency"))
    if discharging_efficiency is not None and 0 < discharging_efficiency <= 1.0:
        payload["discharging_efficiency"] = float(discharging_efficiency)

    charge_rates = candidate.get("charge_rates")
    if isinstance(charge_rates, list):
        normalized_rates: list[float] = []
        for raw_rate in charge_rates:
            rate = _coerce_float(raw_rate)
            if rate is None or rate < 0 or rate > 1.0:
                continue
            normalized_rates.append(float(rate))
        if normalized_rates:
            payload["charge_rates"] = normalized_rates

    return payload


def _extract_legacy_start_solution(payload: dict[str, Any]) -> list[float] | None:
    raw_solution = payload.get("start_solution")
    if not isinstance(raw_solution, list):
        return None
    if len(raw_solution) < 2:
        return None

    normalized: list[float] = []
    for raw_value in raw_solution:
        value = _coerce_float(raw_value)
        if value is None:
            return None
        normalized.append(value)
    return normalized


def _summarize_eos_error(exc: EosApiError) -> str:
    detail_text = str(exc.detail or "").strip()
    if detail_text == "":
        return f"http {exc.status_code}"

    try:
        parsed = json.loads(detail_text)
    except Exception:
        parsed = None

    if isinstance(parsed, dict):
        detail_value = parsed.get("detail")
        if isinstance(detail_value, str) and detail_value.strip():
            first_line = detail_value.strip().splitlines()[0]
            if " for url:" in first_line:
                first_line = first_line.split(" for url:")[0].strip()
            return first_line

    first_line = detail_text.splitlines()[0].strip()
    if " for url:" in first_line:
        first_line = first_line.split(" for url:")[0].strip()
    return first_line if first_line else f"http {exc.status_code}"


def _summarize_exception(exc: Exception) -> str:
    if isinstance(exc, EosApiError):
        return _summarize_eos_error(exc)
    text = str(exc).strip()
    if text == "":
        return exc.__class__.__name__
    first_line = text.splitlines()[0].strip()
    return first_line if first_line else exc.__class__.__name__


def _is_retryable_eos_exception(exc: Exception) -> bool:
    if isinstance(exc, EosApiError):
        if exc.status_code in _EOS_RETRYABLE_STATUS_CODES:
            return True
        detail_text = str(exc.detail or "").strip().lower()
        return any(fragment in detail_text for fragment in _EOS_RETRYABLE_TEXT_FRAGMENTS)

    text = str(exc).strip().lower()
    if text == "":
        return False
    return any(fragment in text for fragment in _EOS_RETRYABLE_TEXT_FRAGMENTS)


def _is_legacy_no_solution_error(exc: EosApiError) -> bool:
    if exc.status_code != 400:
        return False
    detail_text = str(exc.detail or "").lower()
    return "no solution stored by run" in detail_text


def _is_artifact_warmup_404(exc: EosApiError) -> bool:
    if exc.status_code != 404:
        return False
    detail_text = str(exc.detail or "").lower()
    return "did you configure automatic optimization" in detail_text


def _is_artifact_not_configured_reason(*, artifact_name: str, reason: str | None) -> bool:
    text = (reason or "").strip().lower()
    if text == "":
        return False
    if artifact_name == "plan":
        return "can not get the energy management plan" in text
    if artifact_name == "solution":
        return "can not get the optimization solution" in text
    return False


def _extract_energy_management_start_datetime(payload: dict[str, Any] | None) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    energy_management = payload.get("energy-management")
    if not isinstance(energy_management, dict):
        return None
    return _parse_datetime(energy_management.get("start_datetime"))


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_measurement_push_rows(
    *,
    latest_power: list[dict[str, Any]],
    latest_emr: list[dict[str, Any]],
    latest_soc: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in latest_power:
        key = row.get("key")
        ts = row.get("ts")
        value = _coerce_float(row.get("value_w"))
        if not isinstance(key, str) or ts is None or value is None:
            continue
        rows.append({"key": key, "value": value, "ts": ts})

    for row in latest_emr:
        key = row.get("emr_key")
        ts = row.get("ts")
        value = _coerce_float(row.get("emr_kwh"))
        if not isinstance(key, str) or ts is None or value is None:
            continue
        rows.append({"key": key, "value": value, "ts": ts})

    rows.extend(_build_soc_push_rows(latest_soc))
    return _dedupe_measurement_rows(rows)


def _build_soc_push_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    present: set[str] = set()

    for row in rows:
        key = row.get("signal_key")
        ts = row.get("last_ts")
        value = _coerce_numeric_signal_latest(row)
        if not isinstance(key, str) or ts is None or value is None:
            continue
        result.append({"key": key, "value": value, "ts": ts})
        present.add(key)

    for row in list(result):
        source_key = row["key"]
        alias_key = FORCE_MEASUREMENT_SOC_ALIASES.get(source_key)
        if alias_key is None or alias_key in present:
            continue
        result.append({"key": alias_key, "value": row["value"], "ts": row["ts"]})
        present.add(alias_key)

    return result


def _build_soc_signal_key_hints(config_payload: dict[str, Any]) -> list[str]:
    keys: set[str] = set(FORCE_MEASUREMENT_SOC_KEYS)

    for target_key, source_keys in _extract_device_soc_targets(config_payload):
        keys.add(target_key)
        keys.update(source_keys)

    return sorted(keys)


def _build_device_soc_measurement_rows(
    *,
    latest_soc: list[dict[str, Any]],
    config_payload: dict[str, Any],
    available_keys: set[str],
) -> list[dict[str, Any]]:
    by_key = _latest_signal_rows_by_key(latest_soc)
    fallback_row = _pick_latest_generic_soc_row(by_key)
    rows: list[dict[str, Any]] = []
    seen_targets: set[str] = set()

    for target_key, source_keys in _extract_device_soc_targets(config_payload):
        if target_key in seen_targets:
            continue
        seen_targets.add(target_key)
        if target_key not in available_keys:
            continue

        source_row: dict[str, Any] | None = None
        for source_key in source_keys:
            candidate = by_key.get(source_key)
            if candidate is not None:
                source_row = candidate
                break
        if source_row is None:
            source_row = fallback_row
        if source_row is None:
            continue

        value_raw = _coerce_numeric_signal_latest(source_row)
        value = _normalize_soc_factor(value_raw)
        ts = source_row.get("last_ts")
        if value is None or ts is None:
            continue
        rows.append({"key": target_key, "value": value, "ts": ts})

    return rows


def _extract_device_soc_targets(config_payload: dict[str, Any]) -> list[tuple[str, list[str]]]:
    devices = config_payload.get("devices")
    if not isinstance(devices, dict):
        return []

    targets: list[tuple[str, list[str]]] = []

    batteries = devices.get("batteries")
    if isinstance(batteries, list):
        for index, row in enumerate(batteries):
            if not isinstance(row, dict):
                continue
            target_key = _safe_str(row.get("measurement_key_soc_factor"))
            if not target_key:
                continue
            source_keys = [
                target_key,
                f"battery{index + 1}-soc-factor",
            ]
            device_id = _safe_str(row.get("device_id"))
            if device_id:
                source_keys.append(f"{device_id}-soc-factor")
            source_keys.extend(FORCE_MEASUREMENT_SOC_KEYS)
            targets.append((target_key, _unique_preserve_order(source_keys)))

    electric_vehicles = devices.get("electric_vehicles")
    if isinstance(electric_vehicles, list):
        for index, row in enumerate(electric_vehicles):
            if not isinstance(row, dict):
                continue
            target_key = _safe_str(row.get("measurement_key_soc_factor"))
            if not target_key:
                continue
            source_keys = [
                target_key,
                f"ev{index + 1}-soc-factor",
                f"electric_vehicle{index + 1}-soc-factor",
            ]
            device_id = _safe_str(row.get("device_id"))
            if device_id:
                source_keys.append(f"{device_id}-soc-factor")
            source_keys.extend(FORCE_MEASUREMENT_SOC_KEYS)
            targets.append((target_key, _unique_preserve_order(source_keys)))

    return targets


def _latest_signal_rows_by_key(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _safe_str(row.get("signal_key"))
        ts = row.get("last_ts")
        if not key or ts is None:
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = row
            continue
        existing_ts = existing.get("last_ts")
        if isinstance(existing_ts, datetime) and isinstance(ts, datetime):
            if ts >= existing_ts:
                by_key[key] = row
        else:
            by_key[key] = row
    return by_key


def _pick_latest_generic_soc_row(by_key: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for key in FORCE_MEASUREMENT_SOC_KEYS:
        row = by_key.get(key)
        if row is None:
            continue
        if latest is None:
            latest = row
            continue
        latest_ts = latest.get("last_ts")
        row_ts = row.get("last_ts")
        if isinstance(latest_ts, datetime) and isinstance(row_ts, datetime):
            if row_ts >= latest_ts:
                latest = row
            continue
        latest = row
    return latest


def _normalize_soc_factor(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    numeric = float(value)
    if numeric < 0.0:
        return None
    if numeric > 1.0:
        if numeric <= 100.0:
            numeric = numeric / 100.0
        else:
            return None
    if numeric > 1.0:
        numeric = 1.0
    return numeric


def _coerce_numeric_signal_latest(row: dict[str, Any]) -> float | None:
    value_num = _coerce_float(row.get("last_value_num"))
    if value_num is not None:
        return value_num
    return _coerce_float(row.get("last_value_text"))


def _dedupe_measurement_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        else:
            best_by_key[key] = row
    return list(best_by_key.values())


def _normalize_prediction_refresh_scope(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in {"all", "pv", "prices", "load"}:
        return raw
    return "all"


def _normalize_auto_run_preset(value: str | None) -> str | None:
    raw = (value or "").strip().lower()
    if raw in _AUTO_RUN_PRESET_ORDER:
        return raw
    return None


def _auto_run_preset_to_state(preset: str) -> tuple[bool, list[int]]:
    normalized = _normalize_auto_run_preset(preset)
    if normalized is None:
        raise ValueError("invalid auto-run preset")
    slots = list(_AUTO_RUN_PRESET_SLOTS[normalized])
    return (normalized != "off"), slots


def _auto_run_interval_minutes_for_preset(preset: str) -> int | None:
    normalized = _normalize_auto_run_preset(preset)
    if normalized is None or normalized == "off":
        return None
    if normalized == "15m":
        return 15
    if normalized == "30m":
        return 30
    if normalized == "60m":
        return 60
    return None


def _format_aligned_minute_slots(slots: list[int]) -> str:
    if not slots:
        return ""
    return ",".join(str(slot) for slot in sorted(set(slots)))


def _default_auto_run_state_from_settings(settings: Settings) -> tuple[str, bool, list[int]]:
    if not settings.eos_aligned_scheduler_enabled:
        return "off", False, []

    minute_slots = _parse_aligned_minute_slots(settings.eos_aligned_scheduler_minutes)
    if minute_slots == _AUTO_RUN_PRESET_SLOTS["15m"]:
        return "15m", True, list(minute_slots)
    if minute_slots == _AUTO_RUN_PRESET_SLOTS["30m"]:
        return "30m", True, list(minute_slots)
    if minute_slots == _AUTO_RUN_PRESET_SLOTS["60m"]:
        return "60m", True, list(minute_slots)
    if minute_slots:
        return "15m", True, list(minute_slots)
    return "off", False, []


def _extract_auto_run_preset_from_preference_value(value: Any) -> str | None:
    if isinstance(value, str):
        return _normalize_auto_run_preset(value)
    if isinstance(value, dict):
        raw = value.get("preset")
        if isinstance(raw, str):
            return _normalize_auto_run_preset(raw)
    return None


def _parse_aligned_minute_slots(value: str | None) -> list[int]:
    raw = (value or "").strip()
    if raw == "":
        return []

    slots: list[int] = []
    seen: set[int] = set()
    for token in raw.split(","):
        part = token.strip()
        if part == "":
            continue
        if not part.isdigit():
            return []
        minute = int(part)
        if minute < 0 or minute > 59:
            return []
        if minute in seen:
            continue
        seen.add(minute)
        slots.append(minute)
    slots.sort()
    return slots


def _next_aligned_due_utc(
    *,
    now: datetime,
    minute_slots: list[int],
    delay_seconds: int,
) -> datetime:
    if not minute_slots:
        raise ValueError("minute_slots must not be empty")
    base = _to_utc(now).replace(second=0, microsecond=0)
    delay = max(0, min(int(delay_seconds), 59))

    for hour_offset in range(0, 49):
        bucket_hour = base + timedelta(hours=hour_offset)
        for minute in minute_slots:
            candidate = bucket_hour.replace(minute=minute, second=delay, microsecond=0)
            if candidate > now:
                return candidate
    return (base + timedelta(hours=1)).replace(
        minute=minute_slots[0],
        second=delay,
        microsecond=0,
    )


def _extract_plan_instructions(payload: dict[str, Any] | list[Any]) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(payload, list):
        instructions = [item for item in payload if isinstance(item, dict)]
        return "plan-list", instructions

    if not isinstance(payload, dict):
        return "plan-unknown", []

    plan_id = _safe_str(payload.get("id")) or _safe_str(payload.get("plan_id")) or "plan-latest"
    candidate_sets: list[Any] = [
        payload.get("instructions"),
        payload.get("items"),
        payload.get("plan"),
    ]

    for candidate in candidate_sets:
        if isinstance(candidate, list):
            instructions = [item for item in candidate if isinstance(item, dict)]
            return plan_id, instructions
        if isinstance(candidate, dict):
            nested = candidate.get("instructions")
            if isinstance(nested, list):
                instructions = [item for item in nested if isinstance(item, dict)]
                nested_plan_id = _safe_str(candidate.get("id")) or _safe_str(candidate.get("plan_id"))
                return nested_plan_id or plan_id, instructions

    if all(isinstance(key, str) for key in payload.keys()):
        return plan_id, []
    return plan_id, []


def _derive_instruction_window(
    instructions: list[dict[str, Any]],
) -> tuple[datetime | None, datetime | None]:
    starts: list[datetime] = []
    ends: list[datetime] = []
    for instruction in instructions:
        starts_at = _parse_datetime(
            instruction.get("execution_time")
            or instruction.get("effective_at")
            or instruction.get("start_datetime")
            or instruction.get("starts_at")
        )
        ends_at = _parse_datetime(
            instruction.get("end_datetime") if "end_datetime" in instruction else instruction.get("ends_at")
        )
        if starts_at is not None:
            starts.append(starts_at)
        if ends_at is not None:
            ends.append(ends_at)

    valid_from = min(starts) if starts else None
    valid_until = max(ends) if ends else None
    return valid_from, valid_until


def _path_exists(payload: dict[str, Any], path: str) -> bool:
    current: Any = payload
    for part in path.split("/"):
        if not isinstance(current, dict):
            return False
        if part not in current:
            return False
        current = current[part]
    return True


def _ems_mode_path_candidates(config_payload: dict[str, Any] | None) -> list[str]:
    candidates = [
        "ems/mode",
        "energy_management/mode",
        "energy-management/mode",
        "energy/management/mode",
    ]
    if config_payload:
        for candidate in list(candidates):
            if _path_exists(config_payload, candidate):
                candidates.insert(0, candidate)
    return _unique_preserve_order(candidates)


def _ems_interval_path_candidates(config_payload: dict[str, Any] | None) -> list[str]:
    candidates = [
        "ems/interval",
        "energy_management/interval",
        "energy-management/interval",
        "energy/management/interval",
    ]
    if config_payload:
        for candidate in list(candidates):
            if _path_exists(config_payload, candidate):
                candidates.insert(0, candidate)
    return _unique_preserve_order(candidates)


def _prediction_hours_path_candidates(config_payload: dict[str, Any] | None) -> list[str]:
    candidates = [
        "prediction/hours",
        "predictions/hours",
    ]
    if config_payload:
        for candidate in list(candidates):
            if _path_exists(config_payload, candidate):
                candidates.insert(0, candidate)
    return _unique_preserve_order(candidates)


def _optimization_horizon_hours_path_candidates(config_payload: dict[str, Any] | None) -> list[str]:
    candidates = [
        "optimization/horizon_hours",
        "optimization/horizon",
    ]
    if config_payload:
        for candidate in list(candidates):
            if _path_exists(config_payload, candidate):
                candidates.insert(0, candidate)
    return _unique_preserve_order(candidates)


def _pvforecast_provider_path_candidates(config_payload: dict[str, Any] | None) -> list[str]:
    candidates = [
        "pvforecast/provider",
        "pv_forecast/provider",
    ]
    if config_payload:
        for candidate in list(candidates):
            if _path_exists(config_payload, candidate):
                candidates.insert(0, candidate)
    return _unique_preserve_order(candidates)


def _elecprice_provider_path_candidates(config_payload: dict[str, Any] | None) -> list[str]:
    candidates = [
        "elecprice/provider",
        "elec_price/provider",
        "electricity_price/provider",
    ]
    if config_payload:
        for candidate in list(candidates):
            if _path_exists(config_payload, candidate):
                candidates.insert(0, candidate)
    return _unique_preserve_order(candidates)


def _feedintariff_import_json_path_candidates(config_payload: dict[str, Any] | None) -> list[str]:
    candidates = [
        "feedintariff/provider_settings/FeedInTariffImport/import_json",
        "feed_in_tariff/provider_settings/FeedInTariffImport/import_json",
    ]
    if config_payload:
        for candidate in list(candidates):
            if _path_exists(config_payload, candidate):
                candidates.insert(0, candidate)
    return _unique_preserve_order(candidates)


def _is_valid_pv_fallback_provider(
    config_payload: dict[str, Any], provider_id: str
) -> tuple[bool, str | None]:
    pvforecast = config_payload.get("pvforecast")
    if not isinstance(pvforecast, dict):
        return False, "pvforecast config missing"

    providers = pvforecast.get("providers")
    if isinstance(providers, list):
        normalized = {str(item).strip() for item in providers if isinstance(item, (str, int, float))}
        if provider_id not in normalized:
            return False, "provider not listed in pvforecast.providers"

    if provider_id != "PVForecastImport":
        return True, None

    provider_settings = pvforecast.get("provider_settings")
    if not isinstance(provider_settings, dict):
        return False, "pvforecast.provider_settings missing"
    import_settings = provider_settings.get("PVForecastImport")
    if not isinstance(import_settings, dict):
        return False, "PVForecastImport settings missing"

    import_json = import_settings.get("import_json")
    import_file_path = _safe_str(import_settings.get("import_file_path"))
    if import_json is None and not import_file_path:
        return False, "PVForecastImport has neither import_json nor import_file_path"

    values = _extract_pv_import_numeric_values(import_json)
    if values:
        usable, reason = _is_usable_pv_import_values(values)
        if usable:
            return True, None
        if import_file_path:
            return True, None
        return False, reason

    if import_file_path:
        return True, None
    return False, "import_json has no numeric pvforecast values"


def _extract_pv_import_numeric_values(import_json_value: Any) -> list[float]:
    payload = import_json_value
    if isinstance(payload, str):
        text = payload.strip()
        if text == "":
            return []
        try:
            payload = json.loads(text)
        except Exception:
            return []

    values: list[float] = []

    def append_numeric(iterable: Any) -> None:
        if not isinstance(iterable, list):
            return
        for raw in iterable:
            if isinstance(raw, bool):
                continue
            if isinstance(raw, (int, float)):
                numeric = float(raw)
                if math.isfinite(numeric):
                    values.append(numeric)

    if isinstance(payload, dict):
        ac_series = payload.get("pvforecast_ac_power")
        if isinstance(ac_series, list):
            append_numeric(ac_series)
        elif isinstance(ac_series, dict):
            data_map = ac_series.get("data")
            if isinstance(data_map, dict):
                append_numeric(list(data_map.values()))

        if values:
            return values

        data_map = payload.get("data")
        if isinstance(data_map, dict):
            append_numeric(list(data_map.values()))
            return values

        dc_series = payload.get("pvforecast_dc_power")
        if isinstance(dc_series, list):
            append_numeric(dc_series)
            return values
        return values

    append_numeric(payload)
    return values


def _is_usable_pv_import_values(values: list[float]) -> tuple[bool, str | None]:
    if len(values) < 24:
        return False, f"import_json has too few points ({len(values)} < 24)"

    rounded = [round(value, 3) for value in values]
    unique_count = len(set(rounded))
    if unique_count <= 2:
        return False, f"import_json has too few unique values ({unique_count})"

    positive_count = sum(1 for value in values if value > 0.0)
    if positive_count == 0:
        return False, "import_json has no positive PV values"

    return True, None


def _is_pv_akkudoktor_refresh_error(error_text: str, *, provider_id: str | None) -> bool:
    provider = (provider_id or "").strip().lower()
    text = (error_text or "").strip().lower()
    if provider == "pvforecastakkudoktor":
        return True
    if "pvforecastakkudoktor" in text:
        return True
    if "api.akkudoktor.net/forecast" in text and "400" in text:
        return True
    if "wrongparameters" in text and "azimuth" in text:
        return True
    return False


def _extract_ems_interval(config_payload: dict[str, Any]) -> int | None:
    for path in _ems_interval_path_candidates(config_payload):
        value = _get_path_value(config_payload, path)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _extract_ems_mode(config_payload: dict[str, Any]) -> str | None:
    for path in _ems_mode_path_candidates(config_payload):
        value = _get_path_value(config_payload, path)
        text = _safe_str(value)
        if text:
            return text
    return None


def _extract_prediction_hours(config_payload: dict[str, Any]) -> int | None:
    for path in _prediction_hours_path_candidates(config_payload):
        value = _get_path_value(config_payload, path)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _extract_optimization_horizon_hours(config_payload: dict[str, Any]) -> int | None:
    for path in _optimization_horizon_hours_path_candidates(config_payload):
        value = _get_path_value(config_payload, path)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _get_path_value(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("/"):
        if not isinstance(current, dict):
            return None
        if part not in current:
            return None
        current = current[part]
    return current


def _unique_preserve_order(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
