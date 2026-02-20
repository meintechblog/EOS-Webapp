from __future__ import annotations

import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Any

from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.repositories.eos_runtime import (
    add_artifact,
    add_output_event,
    create_run,
    get_latest_successful_run_with_plan,
    get_run_by_eos_last_run_datetime,
    get_run_by_id,
    list_control_targets,
    replace_plan_instructions,
    replace_prediction_points,
    upsert_run_input_snapshot,
    update_run_status,
)
from app.repositories.emr_pipeline import get_latest_emr_values, get_latest_power_samples
from app.repositories.mappings import list_mappings
from app.repositories.parameter_profiles import get_active_parameter_profile, get_current_draft_revision
from app.repositories.signal_backbone import infer_value_type, ingest_signal_measurement, list_latest_by_signal_keys
from app.repositories.telemetry import get_latest_events_by_mapping
from app.services.eos_client import EosApiError, EosClient
from app.services.mqtt_ingest import MqttIngestService

FORCE_MEASUREMENT_SOC_KEYS: tuple[str, str] = ("battery_soc_percent", "battery_soc_pct")
FORCE_MEASUREMENT_SOC_ALIASES: dict[str, str] = {"battery_soc_pct": "battery_soc_percent"}


class EosOrchestratorService:
    _PLAN_TYPE = "plan"
    _SOLUTION_TYPE = "solution"
    _HEALTH_TYPE = "health"
    _PREDICTION_KEYS_TYPE = "prediction_keys"
    _PREDICTION_SERIES_TYPE = "prediction_series"
    _PREDICTION_REFRESH_TYPE = "prediction_refresh"
    _MEASUREMENT_PUSH_TYPE = "measurement_push"
    _LEGACY_REQUEST_TYPE = "legacy_optimize_request"
    _LEGACY_RESPONSE_TYPE = "legacy_optimize_response"

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker,
        eos_client: EosClient,
        mqtt_service: MqttIngestService | None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._eos_client = eos_client
        self._mqtt_service = mqtt_service
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

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True

        self._stop_event.clear()

        if self._settings.eos_autoconfig_enable:
            try:
                self._set_runtime_mode_and_interval(
                    mode=self._settings.eos_autoconfig_mode,
                    interval_seconds=self._settings.eos_autoconfig_interval_seconds,
                )
            except Exception:
                self._logger.exception("failed to apply EOS autoconfig at startup")
        if self._settings.eos_aligned_scheduler_enabled:
            try:
                self._set_runtime_mode_and_interval(
                    mode=self._settings.eos_autoconfig_mode,
                    interval_seconds=self._settings.eos_aligned_scheduler_base_interval_seconds,
                )
            except Exception:
                self._logger.exception("failed to apply aligned scheduler base interval at startup")

        self._collector_thread = Thread(
            target=self._collector_loop,
            name="eos-collector",
            daemon=True,
        )
        self._collector_thread.start()
        if self._settings.eos_aligned_scheduler_enabled:
            self._aligned_scheduler_thread = Thread(
                target=self._aligned_scheduler_loop,
                name="eos-aligned-scheduler",
                daemon=True,
            )
            self._aligned_scheduler_thread.start()
        self._logger.info(
            "started eos orchestrator poll_seconds=%s",
            self._settings.eos_sync_poll_seconds,
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
                "aligned_scheduler_enabled": self._settings.eos_aligned_scheduler_enabled,
                "aligned_scheduler_minutes": self._settings.eos_aligned_scheduler_minutes,
                "aligned_scheduler_delay_seconds": self._settings.eos_aligned_scheduler_delay_seconds,
                "aligned_scheduler_next_due_ts": _to_iso(self._aligned_scheduler_next_due_ts),
                "aligned_scheduler_last_trigger_ts": _to_iso(self._aligned_scheduler_last_trigger_ts),
                "aligned_scheduler_last_skip_reason": self._aligned_scheduler_last_skip_reason,
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
            },
        }

    def update_runtime_config(self, *, mode: str, interval_seconds: int) -> dict[str, str]:
        return self._set_runtime_mode_and_interval(mode=mode, interval_seconds=interval_seconds)

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

        with self._session_factory() as db:
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
        minute_slots = _parse_aligned_minute_slots(self._settings.eos_aligned_scheduler_minutes)
        if not minute_slots:
            self._logger.error(
                "aligned scheduler disabled due to invalid minute slots config: %s",
                self._settings.eos_aligned_scheduler_minutes,
            )
            with self._lock:
                self._aligned_scheduler_last_skip_reason = "invalid_minute_slots"
            return

        delay_seconds = max(0, int(self._settings.eos_aligned_scheduler_delay_seconds))
        self._logger.info(
            "aligned scheduler active slots=%s delay_seconds=%s",
            minute_slots,
            delay_seconds,
        )
        while not self._stop_event.is_set():
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
            wait_seconds = max(0.1, (due - now).total_seconds())
            with self._lock:
                self._aligned_scheduler_next_due_ts = due

            if self._stop_event.wait(wait_seconds):
                return
            if self._stop_event.is_set():
                return

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

            if self._settings.eos_aligned_scheduler_enabled:
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

        with self._session_factory() as db:
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
        plan_payload: dict[str, Any] | list[Any] | None = None
        solution_payload: dict[str, Any] | list[Any] | None = None
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

        self._capture_prediction_artifacts(run_id=run_id, partial_reasons=partial_reasons)

        try:
            plan_payload = _to_json_payload(self._eos_client.get_plan())
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
                    mapping_id=None,
                    source_ref_id=None,
                    tags_json={"source": "eos", "artifact": "plan"},
                )
        except EosApiError as exc:
            if exc.status_code == 404:
                partial_reasons.append(f"plan unavailable: {_summarize_eos_error(exc)}")
            else:
                raise
        except Exception as exc:
            partial_reasons.append(f"plan capture failed: {exc}")

        try:
            solution_payload = _to_json_payload(self._eos_client.get_solution())
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
                    mapping_id=None,
                    source_ref_id=None,
                    tags_json={"source": "eos", "artifact": "solution"},
                )
        except EosApiError as exc:
            if exc.status_code == 404:
                partial_reasons.append(f"solution unavailable: {_summarize_eos_error(exc)}")
            else:
                raise
        except Exception as exc:
            partial_reasons.append(f"solution capture failed: {exc}")

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
                        mapping_id=None,
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

        self._publish_outputs(
            run_id=run_id,
            plan_payload=plan_payload,
            solution_payload=solution_payload,
            plan_valid_from=plan_valid_from,
            plan_valid_until=plan_valid_until,
            plan_instructions=plan_instructions_payloads,
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
            mappings = list_mappings(db)
            latest_events = get_latest_events_by_mapping(db)
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

            mappings_snapshot: list[dict[str, Any]] = []
            mapped_inputs: dict[str, Any] = {}
            live_state_snapshot: dict[str, Any] = {}
            now = datetime.now(timezone.utc)
            for mapping in mappings:
                mappings_snapshot.append(
                    {
                        "id": mapping.id,
                        "eos_field": mapping.eos_field,
                        "channel_id": mapping.channel_id,
                        "channel_code": mapping.channel_code,
                        "channel_type": mapping.channel_type,
                        "input_key": mapping.input_key,
                        "mqtt_topic": mapping.mqtt_topic,
                        "fixed_value": mapping.fixed_value,
                        "payload_path": mapping.payload_path,
                        "timestamp_path": mapping.timestamp_path,
                        "unit": mapping.unit,
                        "value_multiplier": mapping.value_multiplier,
                        "sign_convention": mapping.sign_convention,
                        "enabled": mapping.enabled,
                        "updated_at": mapping.updated_at.isoformat(),
                    }
                )

                if not mapping.enabled:
                    continue

                if mapping.fixed_value is not None:
                    transformed_value = _apply_mapping_transform(
                        raw_value=mapping.fixed_value,
                        value_multiplier=mapping.value_multiplier,
                        sign_convention=mapping.sign_convention,
                    )
                    mapped_inputs[mapping.eos_field] = transformed_value
                    live_state_snapshot[mapping.eos_field] = {
                        "value": transformed_value,
                        "status": "fixed",
                        "source": "fixed_input",
                        "ts": now.isoformat(),
                    }
                    continue

                latest = latest_events.get(mapping.id)
                if latest is None:
                    continue
                mapped_inputs[mapping.eos_field] = latest.parsed_value
                live_state_snapshot[mapping.eos_field] = {
                    "value": latest.parsed_value,
                    "status": "live",
                    "source": f"{mapping.channel_type or 'unknown'}_input",
                    "ts": latest.ts.isoformat() if latest.ts else None,
                }

            assembled = {
                "captured_at": now.isoformat(),
                "run_id": run_id,
                "parameters_payload": parameter_payload,
                "mapped_inputs": mapped_inputs,
                "runtime_config": runtime_config_snapshot,
                "previous_successful_run": previous_successful_summary,
            }

            upsert_run_input_snapshot(
                db,
                run_id=run_id,
                parameter_profile_id=active_profile.id if active_profile else None,
                parameter_revision_id=draft_revision.id if draft_revision else None,
                parameter_payload_json=_to_json_payload(parameter_payload),
                mappings_snapshot_json=_to_json_payload({"items": mappings_snapshot}),
                live_state_snapshot_json=_to_json_payload(live_state_snapshot),
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
        feedin_spot_sync: dict[str, Any] | None = None
        global_error: str | None = None

        if normalized_scope == "all":
            try:
                self._eos_client.trigger_prediction_update(force_update=True, force_enable=False)
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
                            self._eos_client.trigger_prediction_update(
                                force_update=True,
                                force_enable=False,
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

            return {
                "scope": normalized_scope,
                "force_update": True,
                "force_enable": False,
                "providers": refreshed,
                "failed": failed,
                "global_error": global_error,
                "fallback_applied": fallback_applied,
                "feedin_spot_sync": feedin_spot_sync,
            }

        for provider_id in provider_ids:
            try:
                self._eos_client.trigger_prediction_update_provider(
                    provider_id=provider_id,
                    force_update=True,
                    force_enable=False,
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
                            self._eos_client.trigger_prediction_update_provider(
                                provider_id=provider_id,
                                force_update=True,
                                force_enable=False,
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

        return {
            "scope": normalized_scope,
            "force_update": True,
            "force_enable": False,
            "providers": refreshed,
            "failed": failed,
            "global_error": global_error,
            "fallback_applied": fallback_applied,
            "feedin_spot_sync": feedin_spot_sync,
        }

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
        if not _is_valid_pv_fallback_provider(config_payload, fallback_provider):
            return {
                "scope": scope,
                "failed_provider_id": failed_provider_id,
                "from_provider": current_provider,
                "to_provider": fallback_provider,
                "applied": False,
                "note": "fallback provider not configured with usable import data",
            }

        try:
            path = self._set_config_value(
                path_candidates=_pvforecast_provider_path_candidates(config_payload),
                value=fallback_provider,
            )
            try:
                self._eos_client.save_config_file()
            except Exception as exc:
                self._logger.warning("pv fallback config switched but save to EOS config file failed: %s", exc)
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

    def _resolve_prediction_scope_providers(self, *, scope: str) -> list[str]:
        config_payload = self._eos_client.get_config()
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

    def _capture_prediction_artifacts(self, *, run_id: int, partial_reasons: list[str]) -> None:
        try:
            prediction_keys = self._eos_client.get_prediction_keys()
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
                    series_payload = self._eos_client.get_prediction_series(key=key)
                    points = _extract_prediction_points(series_payload)
                    if not points:
                        fallback_list = self._eos_client.get_prediction_list(key=key)
                        points = _extract_prediction_points(fallback_list)
                        if fallback_list:
                            series_payload = {"values": fallback_list}

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
                        replace_prediction_points(
                            db,
                            run_id=run_id,
                            prediction_key=key,
                            points=points,
                        )
                        for point_ts, point_value in points:
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
                                mapping_id=None,
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

    def _force_run_worker(
        self,
        run_id: int,
        trigger_source: str = "force_run",
        run_mode: str = "pulse_then_legacy",
    ) -> None:
        original_interval: int | None = None
        original_mode: str | None = None
        prior_last_run: datetime | None = None
        pre_force_notes: list[str] = []
        try:
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
                        pre_force_notes.append(
                            f"pre-force prediction refresh global error: {global_error}"
                        )
                    elif isinstance(failed_updates, list) and failed_updates:
                        pre_force_notes.append(
                            f"pre-force prediction refresh had {len(failed_updates)} provider failure(s)"
                        )
                except Exception as exc:
                    pre_force_notes.append(f"pre-force prediction refresh failed: {exc}")

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
                    pre_force_notes.append(
                        f"pre-force measurement push had {failed_count} failure(s)"
                    )
            except Exception as exc:
                pre_force_notes.append(f"pre-force measurement push failed: {exc}")

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
            observed = self._wait_for_next_last_run_datetime(
                previous=prior_last_run,
                timeout_seconds=self._settings.eos_force_run_timeout_seconds,
            )

            if observed is None and self._settings.eos_force_run_allow_legacy:
                self._logger.warning("force run pulse timeout reached; invoking legacy /optimize fallback")
                self._run_legacy_optimize(run_id=run_id)
                observed = self._wait_for_next_last_run_datetime(
                    previous=prior_last_run,
                    timeout_seconds=min(30, self._settings.eos_force_run_timeout_seconds),
                )

            if observed is None:
                raise RuntimeError(
                    f"Force run timed out after {self._settings.eos_force_run_timeout_seconds}s"
                )

            self._collect_run_for_last_datetime(
                observed,
                trigger_source=trigger_source,
                run_mode=run_mode,
                existing_run_id=run_id,
            )

            if pre_force_notes:
                with self._session_factory() as db:
                    run = get_run_by_id(db, run_id)
                    if run is not None:
                        merged_notes: list[str] = []
                        existing_error = _safe_str(run.error_text)
                        if existing_error:
                            merged_notes.append(existing_error)
                        merged_notes.extend(pre_force_notes)
                        run.error_text = "; ".join(merged_notes) if merged_notes else None
                        if run.status == "success":
                            run.status = "partial"
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
        with self._session_factory() as db:
            latest_power = get_latest_power_samples(db)
            latest_emr = get_latest_emr_values(db)
            latest_soc = list_latest_by_signal_keys(db, signal_keys=list(FORCE_MEASUREMENT_SOC_KEYS))

        payload_rows = _build_measurement_push_rows(
            latest_power=latest_power,
            latest_emr=latest_emr,
            latest_soc=latest_soc,
        )
        available_keys = {
            key.strip()
            for key in self._eos_client.get_measurement_keys()
            if isinstance(key, str) and key.strip() != ""
        }

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

        response = self._eos_client.run_optimize(payload=payload)
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

        _trim_legacy_series_to_common_length(ems_payload)
        ems_payload["preis_euro_pro_wh_akku"] = _extract_battery_storage_cost_per_wh(
            self._eos_client.get_config()
        )

        return {
            "ems": ems_payload,
            "pv_akku": None,
            "inverter": None,
            "eauto": None,
        }

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

    def _publish_outputs(
        self,
        *,
        run_id: int,
        plan_payload: dict[str, Any] | list[Any] | None,
        solution_payload: dict[str, Any] | list[Any] | None,
        plan_valid_from: datetime | None,
        plan_valid_until: datetime | None,
        plan_instructions: list[dict[str, Any]],
    ) -> None:
        if not self._settings.eos_output_mqtt_enabled or self._mqtt_service is None:
            self._record_output_event(
                run_id=run_id,
                topic=f"{self._settings.eos_output_mqtt_prefix.rstrip('/')}/plan/latest",
                payload={
                    "reason": "MQTT output disabled",
                    "mqtt_service_available": self._mqtt_service is not None,
                },
                publish_status="skipped_output_disabled",
                qos=self._settings.eos_output_mqtt_qos,
                retain=self._settings.eos_output_mqtt_retain,
                output_kind="plan",
                resource_id=None,
                error_text=None,
            )
            return

        prefix = self._settings.eos_output_mqtt_prefix.rstrip("/")
        generated_at = datetime.now(timezone.utc)

        if plan_payload is not None:
            plan_topic = f"{prefix}/plan/latest"
            plan_message = {
                "run_id": run_id,
                "generated_at": generated_at.isoformat(),
                "valid_from": _to_iso(plan_valid_from),
                "valid_until": _to_iso(plan_valid_until),
                "plan": plan_payload,
            }
            self._publish_and_audit(
                run_id=run_id,
                topic=plan_topic,
                payload=plan_message,
                qos=self._settings.eos_output_mqtt_qos,
                retain=self._settings.eos_output_mqtt_retain,
                output_kind="plan",
                resource_id=None,
            )

        if solution_payload is not None:
            solution_topic = f"{prefix}/solution/latest"
            solution_message = {
                "run_id": run_id,
                "generated_at": generated_at.isoformat(),
                "solution": solution_payload,
            }
            self._publish_and_audit(
                run_id=run_id,
                topic=solution_topic,
                payload=solution_message,
                qos=self._settings.eos_output_mqtt_qos,
                retain=self._settings.eos_output_mqtt_retain,
                output_kind="solution",
                resource_id=None,
            )

        active_instructions = _select_active_instructions(plan_instructions, generated_at)
        if not active_instructions:
            return

        with self._session_factory() as db:
            target_by_resource = {target.resource_id: target for target in list_control_targets(db)}
            latest_power_samples = get_latest_power_samples(db, keys=["grid_import_w"])
            latest_grid_import_w = _extract_latest_power_value(latest_power_samples, key="grid_import_w")

        for instruction in active_instructions:
            resource_id = _safe_str(instruction.get("resource_id"))
            if resource_id is None:
                continue

            target = target_by_resource.get(resource_id)
            if target is None:
                self._record_output_event(
                    run_id=run_id,
                    topic=f"{prefix}/resource/{resource_id}/command",
                    payload={"reason": "no_control_target", "resource_id": resource_id},
                    publish_status="skipped_no_target",
                    qos=self._settings.eos_output_mqtt_qos,
                    retain=self._settings.eos_output_mqtt_retain,
                    output_kind="command",
                    resource_id=resource_id,
                    error_text=None,
                )
                continue

            if not target.enabled:
                self._record_output_event(
                    run_id=run_id,
                    topic=target.command_topic,
                    payload={"reason": "target_disabled", "resource_id": resource_id},
                    publish_status="skipped_target_disabled",
                    qos=target.qos,
                    retain=target.retain,
                    output_kind="command",
                    resource_id=resource_id,
                    error_text=None,
                )
                continue

            if _should_block_for_grid_charge_guard(
                instruction=instruction,
                resource_id=resource_id,
                latest_grid_import_w=latest_grid_import_w,
                enabled=self._settings.eos_no_grid_charge_guard_enabled,
                threshold_w=self._settings.eos_no_grid_charge_guard_threshold_w,
            ):
                self._record_output_event(
                    run_id=run_id,
                    topic=target.command_topic,
                    payload={
                        "reason": "blocked_grid_charge_guard",
                        "resource_id": resource_id,
                        "grid_import_w": latest_grid_import_w,
                        "threshold_w": self._settings.eos_no_grid_charge_guard_threshold_w,
                    },
                    publish_status="skipped_grid_charge_guard",
                    qos=target.qos,
                    retain=target.retain,
                    output_kind="command",
                    resource_id=resource_id,
                    error_text=None,
                )
                continue

            command_payload = _build_command_payload(
                run_id=run_id,
                instruction=instruction,
                template=target.payload_template_json,
                generated_at=generated_at,
            )
            command_topic = target.command_topic

            should_preview = (not self._settings.eos_actuation_enabled) or target.dry_run_only
            if should_preview:
                preview_topic = f"{command_topic.rstrip('/')}/preview"
                published, error_text = self._mqtt_service.publish_json(
                    topic=preview_topic,
                    payload=command_payload,
                    qos=target.qos,
                    retain=target.retain,
                )
                self._record_output_event(
                    run_id=run_id,
                    topic=preview_topic,
                    payload=command_payload,
                    publish_status="preview_sent" if published else "skipped_safety",
                    qos=target.qos,
                    retain=target.retain,
                    output_kind="preview",
                    resource_id=resource_id,
                    error_text=error_text,
                )
                continue

            self._publish_and_audit(
                run_id=run_id,
                topic=command_topic,
                payload=command_payload,
                qos=target.qos,
                retain=target.retain,
                output_kind="command",
                resource_id=resource_id,
            )

    def _publish_and_audit(
        self,
        *,
        run_id: int,
        topic: str,
        payload: dict[str, Any],
        qos: int,
        retain: bool,
        output_kind: str,
        resource_id: str | None,
    ) -> None:
        if self._mqtt_service is None:
            self._record_output_event(
                run_id=run_id,
                topic=topic,
                payload=payload,
                publish_status="skipped_output_disabled",
                qos=qos,
                retain=retain,
                output_kind=output_kind,
                resource_id=resource_id,
                error_text="MQTT service disabled",
            )
            return
        published, error_text = self._mqtt_service.publish_json(
            topic=topic,
            payload=payload,
            qos=qos,
            retain=retain,
        )
        self._record_output_event(
            run_id=run_id,
            topic=topic,
            payload=payload,
            publish_status="published" if published else "publish_failed",
            qos=qos,
            retain=retain,
            output_kind=output_kind,
            resource_id=resource_id,
            error_text=error_text,
        )

    def _record_output_event(
        self,
        *,
        run_id: int,
        topic: str,
        payload: dict[str, Any],
        publish_status: str,
        qos: int,
        retain: bool,
        output_kind: str,
        resource_id: str | None,
        error_text: str | None,
    ) -> None:
        try:
            with self._session_factory() as db:
                add_output_event(
                    db,
                    run_id=run_id,
                    topic=topic,
                    payload_json=_to_json_payload(payload),
                    qos=qos,
                    retain=retain,
                    output_kind=output_kind,
                    resource_id=resource_id,
                    publish_status=publish_status,
                    error_text=error_text,
                )
        except Exception:
            self._logger.exception("failed to persist mqtt output event run_id=%s topic=%s", run_id, topic)


def _to_json_payload(value: Any) -> dict[str, Any] | list[Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return value
    return {"value": value}


def _apply_mapping_transform(
    *,
    raw_value: str | None,
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
    if transformed.is_integer():
        return str(int(transformed))
    return format(transformed, ".12g")


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
                    or _parse_datetime(raw_item.get("ts"))
                    or _parse_datetime(raw_item.get("timestamp"))
                    or _parse_datetime(raw_item.get("start_datetime"))
                    or now + timedelta(minutes=15 * index)
                )
                numeric_value = _coerce_float(
                    raw_item.get("value")
                    if "value" in raw_item
                    else raw_item.get("y")
                    if "y" in raw_item
                    else raw_item.get("v"),
                )
                points.append((ts, numeric_value))
                continue

            ts = now + timedelta(minutes=15 * index)
            points.append((ts, _coerce_float(raw_item)))
        return points

    return []


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


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
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


def _select_active_instructions(
    instructions: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    active_by_resource: dict[str, dict[str, Any]] = {}
    for instruction in instructions:
        resource_id = _safe_str(instruction.get("resource_id"))
        if resource_id is None:
            continue

        starts_at = _parse_datetime(
            instruction.get("execution_time")
            or instruction.get("effective_at")
            or instruction.get("start_datetime")
            or instruction.get("starts_at")
        )
        ends_at = _parse_datetime(
            instruction.get("end_datetime") if "end_datetime" in instruction else instruction.get("ends_at")
        )

        if starts_at and starts_at > now:
            continue
        if ends_at and ends_at <= now:
            continue

        existing = active_by_resource.get(resource_id)
        if existing is None:
            active_by_resource[resource_id] = instruction
            continue

        existing_start = _parse_datetime(
            existing.get("execution_time")
            or existing.get("effective_at")
            or existing.get("start_datetime")
            or existing.get("starts_at")
        ) or datetime.min.replace(tzinfo=timezone.utc)
        current_start = starts_at or datetime.min.replace(tzinfo=timezone.utc)
        if current_start >= existing_start:
            active_by_resource[resource_id] = instruction

    return list(active_by_resource.values())


def _build_command_payload(
    *,
    run_id: int,
    instruction: dict[str, Any],
    template: dict[str, Any] | list[Any] | None,
    generated_at: datetime,
) -> dict[str, Any]:
    payload = {
        "run_id": run_id,
        "generated_at": generated_at.isoformat(),
        "resource_id": instruction.get("resource_id"),
        "instruction_type": instruction.get("type") or instruction.get("instruction_type"),
        "operation_mode_id": instruction.get("operation_mode_id"),
        "operation_mode_factor": instruction.get("operation_mode_factor"),
        "execution_time": instruction.get("execution_time") or instruction.get("effective_at"),
        "starts_at": instruction.get("start_datetime") or instruction.get("starts_at"),
        "ends_at": instruction.get("end_datetime") or instruction.get("ends_at"),
        "instruction": instruction,
    }
    if isinstance(template, dict):
        merged = dict(template)
        merged.update(payload)
        return merged
    return payload


def _extract_latest_power_value(rows: list[dict[str, Any]], *, key: str) -> float | None:
    for row in rows:
        if row.get("key") != key:
            continue
        value = row.get("value_w")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _should_block_for_grid_charge_guard(
    *,
    instruction: dict[str, Any],
    resource_id: str,
    latest_grid_import_w: float | None,
    enabled: bool,
    threshold_w: float,
) -> bool:
    if not enabled:
        return False
    if latest_grid_import_w is None:
        return False
    if latest_grid_import_w <= max(0.0, threshold_w):
        return False
    if not _looks_like_battery_resource(resource_id):
        return False
    return _looks_like_charge_instruction(instruction)


def _looks_like_battery_resource(resource_id: str) -> bool:
    return "battery" in resource_id.lower()


def _looks_like_charge_instruction(instruction: dict[str, Any]) -> bool:
    instruction_type = _safe_str(instruction.get("type") or instruction.get("instruction_type"))
    if instruction_type:
        lower = instruction_type.lower()
        if "discharge" in lower or "entladen" in lower:
            return False
        if "charge" in lower or "laden" in lower:
            return True

    factor = instruction.get("operation_mode_factor")
    try:
        if factor is not None and float(factor) > 0.0:
            return True
    except (TypeError, ValueError):
        pass

    mode_id = _safe_str(instruction.get("operation_mode_id"))
    if mode_id:
        lower = mode_id.lower()
        if "discharge" in lower or "entladen" in lower:
            return False
        if "charge" in lower or "laden" in lower:
            return True
    return False


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


def _is_valid_pv_fallback_provider(config_payload: dict[str, Any], provider_id: str) -> bool:
    pvforecast = config_payload.get("pvforecast")
    if not isinstance(pvforecast, dict):
        return False

    providers = pvforecast.get("providers")
    if isinstance(providers, list):
        normalized = {str(item).strip() for item in providers if isinstance(item, (str, int, float))}
        if provider_id not in normalized:
            return False

    if provider_id != "PVForecastImport":
        return True

    provider_settings = pvforecast.get("provider_settings")
    if not isinstance(provider_settings, dict):
        return False
    import_settings = provider_settings.get("PVForecastImport")
    if not isinstance(import_settings, dict):
        return False

    import_json = _safe_str(import_settings.get("import_json"))
    import_file_path = _safe_str(import_settings.get("import_file_path"))
    return bool(import_json or import_file_path)


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
