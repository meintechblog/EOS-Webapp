from __future__ import annotations

import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from time import sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import EosPlanInstruction, OutputTarget
from app.repositories.emr_pipeline import get_latest_power_samples
from app.repositories.eos_runtime import (
    create_output_dispatch_event,
    get_latest_successful_run_with_plan,
    has_output_dispatch_events_for_key_prefix,
    list_output_targets,
    list_plan_instructions_for_run,
)


@dataclass(frozen=True)
class NormalizedInstruction:
    id: int
    run_id: int
    instruction_index: int
    instruction_type: str
    resource_id: str
    actuator_id: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    execution_time: datetime | None
    operation_mode_id: str | None
    operation_mode_factor: float | None
    payload_json: dict[str, Any]


class OutputDispatchService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._logger = logging.getLogger("app.output_dispatch")

        self._stop_event = Event()
        self._thread: Thread | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="output-dispatch-force")

        self._lock = Lock()
        self._running = False
        self._last_tick_ts: datetime | None = None
        self._last_error: str | None = None
        self._last_status: str | None = None
        self._last_run_id: int | None = None
        self._scheduler_cursor_ts: datetime | None = None
        self._next_heartbeat_ts: datetime | None = None
        self._force_future: Future[dict[str, Any]] | None = None

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            now = datetime.now(timezone.utc)
            self._scheduler_cursor_ts = now
            self._next_heartbeat_ts = now
        self._stop_event.clear()
        self._thread = Thread(target=self._loop, name="output-dispatch", daemon=True)
        self._thread.start()
        self._logger.info(
            "started output dispatch enabled=%s tick=%ss heartbeat=%ss",
            self._settings.output_http_dispatch_enabled,
            self._settings.output_scheduler_tick_seconds,
            self._settings.output_heartbeat_seconds,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._executor.shutdown(wait=False, cancel_futures=False)
        with self._lock:
            self._running = False

    def request_force_dispatch(self, *, resource_ids: list[str] | None = None) -> dict[str, Any]:
        if not self._settings.output_http_dispatch_enabled:
            raise RuntimeError("HTTP output dispatch is disabled by configuration")

        requested_resources = [item.strip() for item in (resource_ids or []) if item and item.strip()]

        with self._lock:
            force_future = self._force_future
            if force_future is not None and not force_future.done():
                raise RuntimeError("A force dispatch is already in progress")

        with self._session_factory() as db:
            run = get_latest_successful_run_with_plan(db)
            if run is None:
                raise RuntimeError("No successful EOS run available for force dispatch")
            run_id = int(run.id)
            queued_resources = requested_resources
            if not queued_resources:
                instructions = _normalize_instructions(list_plan_instructions_for_run(db, run_id))
                active = _select_current_by_resource(
                    instructions,
                    at=datetime.now(timezone.utc),
                )
                queued_resources = [instruction.resource_id for instruction in active]

        force_token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        future = self._executor.submit(
            self._force_worker,
            run_id,
            queued_resources,
            force_token,
        )
        with self._lock:
            self._force_future = future
            self._last_run_id = run_id
        return {
            "run_id": run_id,
            "status": "accepted",
            "message": "Force dispatch queued",
            "queued_resources": queued_resources,
        }

    def get_status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            force_future = self._force_future
            return {
                "enabled": self._settings.output_http_dispatch_enabled,
                "running": self._running and not self._stop_event.is_set(),
                "tick_seconds": self._settings.output_scheduler_tick_seconds,
                "heartbeat_seconds": self._settings.output_heartbeat_seconds,
                "last_tick_ts": _to_iso(self._last_tick_ts),
                "last_status": self._last_status,
                "last_error": self._last_error,
                "last_run_id": self._last_run_id,
                "next_heartbeat_ts": _to_iso(self._next_heartbeat_ts),
                "force_in_progress": bool(force_future and not force_future.done()),
            }

    def get_current_outputs(
        self,
        db: Session,
        *,
        run_id: int | None = None,
        at: datetime | None = None,
    ) -> tuple[int | None, list[dict[str, Any]]]:
        now = _to_utc(at or datetime.now(timezone.utc))
        selected_run_id, instructions = self._load_run_instructions(db, run_id=run_id)
        if selected_run_id is None:
            return None, []

        latest_grid_import_w = _read_latest_grid_import_w(db)
        current = _select_current_by_resource(instructions, at=now)
        items: list[dict[str, Any]] = []
        for instruction in current:
            items.append(
                {
                    "run_id": selected_run_id,
                    "resource_id": instruction.resource_id,
                    "actuator_id": instruction.actuator_id,
                    "operation_mode_id": instruction.operation_mode_id,
                    "operation_mode_factor": instruction.operation_mode_factor,
                    "effective_at": instruction.execution_time,
                    "source_instruction": instruction.payload_json,
                    "safety_status": self._safety_status(
                        instruction=instruction,
                        latest_grid_import_w=latest_grid_import_w,
                    ),
                }
            )
        return selected_run_id, items

    def get_timeline(
        self,
        db: Session,
        *,
        run_id: int | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        resource_id: str | None = None,
    ) -> tuple[int | None, list[dict[str, Any]]]:
        selected_run_id, instructions = self._load_run_instructions(db, run_id=run_id)
        if selected_run_id is None:
            return None, []

        timeline = _build_deduped_timeline(
            instructions,
            from_ts=_to_utc(from_ts) if from_ts else None,
            to_ts=_to_utc(to_ts) if to_ts else None,
            resource_id=resource_id,
        )
        rows: list[dict[str, Any]] = []
        for instruction in timeline:
            rows.append(
                {
                    "run_id": selected_run_id,
                    "instruction_id": instruction.id,
                    "instruction_index": instruction.instruction_index,
                    "resource_id": instruction.resource_id,
                    "actuator_id": instruction.actuator_id,
                    "instruction_type": instruction.instruction_type,
                    "operation_mode_id": instruction.operation_mode_id,
                    "operation_mode_factor": instruction.operation_mode_factor,
                    "execution_time": instruction.execution_time,
                    "starts_at": instruction.starts_at,
                    "ends_at": instruction.ends_at,
                    "source_instruction": instruction.payload_json,
                    "deduped": True,
                }
            )
        return selected_run_id, rows

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._settings.output_http_dispatch_enabled:
                self._stop_event.wait(1.0)
                continue

            now = datetime.now(timezone.utc)
            try:
                self._run_scheduler_tick(now=now)
                self._set_runtime_status(status="ok", error=None, tick_ts=now)
            except Exception as exc:
                self._logger.exception("output dispatch tick failed")
                self._set_runtime_status(status="error", error=str(exc), tick_ts=now)

            self._stop_event.wait(1.0)

    def _run_scheduler_tick(self, *, now: datetime) -> None:
        with self._lock:
            cursor = self._scheduler_cursor_ts or now
            next_heartbeat = self._next_heartbeat_ts or now

        if now >= cursor + timedelta(seconds=self._settings.output_scheduler_tick_seconds):
            self._dispatch_due_transitions(now=now, previous_cursor=cursor)
            with self._lock:
                self._scheduler_cursor_ts = now

        if now >= next_heartbeat:
            self._dispatch_heartbeat(now=now)
            with self._lock:
                self._next_heartbeat_ts = now + timedelta(seconds=self._settings.output_heartbeat_seconds)

    def _dispatch_due_transitions(self, *, now: datetime, previous_cursor: datetime) -> None:
        with self._session_factory() as db:
            run = get_latest_successful_run_with_plan(db)
            if run is None:
                self._set_runtime_status(status="no_successful_run", error=None, tick_ts=now)
                return
            run_id = int(run.id)
            self._set_last_run_id(run_id)
            instructions = _normalize_instructions(list_plan_instructions_for_run(db, run_id))
            targets = {target.resource_id: target for target in list_output_targets(db)}
            latest_grid_import_w = _read_latest_grid_import_w(db)

        due = [
            instruction
            for instruction in instructions
            if instruction.execution_time is not None
            and previous_cursor < instruction.execution_time <= now
        ]

        for instruction in due:
            self._dispatch_instruction(
                run_id=run_id,
                instruction=instruction,
                target=targets.get(instruction.resource_id),
                latest_grid_import_w=latest_grid_import_w,
                dispatch_kind="scheduled",
                heartbeat_bucket=None,
                force_token=None,
            )

    def _dispatch_heartbeat(self, *, now: datetime) -> None:
        with self._session_factory() as db:
            run = get_latest_successful_run_with_plan(db)
            if run is None:
                self._set_runtime_status(status="no_successful_run", error=None, tick_ts=now)
                return
            run_id = int(run.id)
            self._set_last_run_id(run_id)
            instructions = _normalize_instructions(list_plan_instructions_for_run(db, run_id))
            targets = {target.resource_id: target for target in list_output_targets(db)}
            latest_grid_import_w = _read_latest_grid_import_w(db)

        active = _select_current_by_resource(instructions, at=now)
        heartbeat_bucket = int(now.timestamp() // max(1, self._settings.output_heartbeat_seconds))
        for instruction in active:
            self._dispatch_instruction(
                run_id=run_id,
                instruction=instruction,
                target=targets.get(instruction.resource_id),
                latest_grid_import_w=latest_grid_import_w,
                dispatch_kind="heartbeat",
                heartbeat_bucket=heartbeat_bucket,
                force_token=None,
            )

    def _force_worker(self, run_id: int, resource_ids: list[str], force_token: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self._session_factory() as db:
            instructions = _normalize_instructions(list_plan_instructions_for_run(db, run_id))
            targets = {target.resource_id: target for target in list_output_targets(db)}
            latest_grid_import_w = _read_latest_grid_import_w(db)

        active = _select_current_by_resource(instructions, at=now)
        if resource_ids:
            resource_set = {item.strip() for item in resource_ids if item and item.strip()}
            active = [instruction for instruction in active if instruction.resource_id in resource_set]

        for instruction in active:
            self._dispatch_instruction(
                run_id=run_id,
                instruction=instruction,
                target=targets.get(instruction.resource_id),
                latest_grid_import_w=latest_grid_import_w,
                dispatch_kind="force",
                heartbeat_bucket=None,
                force_token=force_token,
            )

        self._set_runtime_status(status="force_done", error=None, tick_ts=datetime.now(timezone.utc))
        return {"run_id": run_id, "count": len(active)}

    def _dispatch_instruction(
        self,
        *,
        run_id: int,
        instruction: NormalizedInstruction,
        target: OutputTarget | None,
        latest_grid_import_w: float | None,
        dispatch_kind: str,
        heartbeat_bucket: int | None,
        force_token: str | None,
    ) -> None:
        execution_token = _to_iso(instruction.execution_time) or "na"
        base_key = f"{run_id}:{instruction.resource_id}:{execution_token}:{dispatch_kind}"
        if heartbeat_bucket is not None:
            base_key = f"{base_key}:{heartbeat_bucket}"
        if force_token is not None:
            base_key = f"{base_key}:{force_token}"

        with self._session_factory() as db:
            if has_output_dispatch_events_for_key_prefix(db, idempotency_prefix=base_key):
                return

        payload = _build_dispatch_payload(
            run_id=run_id,
            dispatch_kind=dispatch_kind,
            instruction=instruction,
            generated_at=datetime.now(timezone.utc),
            template=target.payload_template_json if target is not None else None,
        )

        if target is None:
            self._record_dispatch_event(
                run_id=run_id,
                resource_id=instruction.resource_id,
                execution_time=instruction.execution_time,
                dispatch_kind=dispatch_kind,
                target_url=None,
                payload=payload,
                status="skipped_no_target",
                http_status=None,
                error_text="no output target configured",
                idempotency_key=base_key,
            )
            return

        if not target.enabled:
            self._record_dispatch_event(
                run_id=run_id,
                resource_id=instruction.resource_id,
                execution_time=instruction.execution_time,
                dispatch_kind=dispatch_kind,
                target_url=target.webhook_url,
                payload=payload,
                status="blocked",
                http_status=None,
                error_text="output target disabled",
                idempotency_key=base_key,
            )
            return

        if self._is_blocked_by_grid_charge_guard(
            instruction=instruction,
            latest_grid_import_w=latest_grid_import_w,
        ):
            self._record_dispatch_event(
                run_id=run_id,
                resource_id=instruction.resource_id,
                execution_time=instruction.execution_time,
                dispatch_kind=dispatch_kind,
                target_url=target.webhook_url,
                payload=payload,
                status="blocked",
                http_status=None,
                error_text=(
                    "blocked by no-grid-charge guard "
                    f"(grid_import_w={latest_grid_import_w}, threshold_w={self._settings.eos_no_grid_charge_guard_threshold_w})"
                ),
                idempotency_key=base_key,
            )
            return

        retry_max = max(0, int(target.retry_max))
        for attempt in range(retry_max + 1):
            if attempt > 0:
                sleep(min(10.0, float(2**attempt)))
            attempt_key = base_key if attempt == 0 else f"{base_key}:retry:{attempt}"
            try:
                http_status, _body = _send_webhook_request(
                    url=target.webhook_url,
                    method=target.method,
                    payload=payload,
                    headers=target.headers_json,
                    timeout_seconds=int(target.timeout_seconds),
                    idempotency_key=attempt_key,
                )
                self._record_dispatch_event(
                    run_id=run_id,
                    resource_id=instruction.resource_id,
                    execution_time=instruction.execution_time,
                    dispatch_kind=dispatch_kind,
                    target_url=target.webhook_url,
                    payload=payload,
                    status="sent",
                    http_status=http_status,
                    error_text=None,
                    idempotency_key=attempt_key,
                )
                return
            except Exception as exc:
                is_last = attempt >= retry_max
                self._record_dispatch_event(
                    run_id=run_id,
                    resource_id=instruction.resource_id,
                    execution_time=instruction.execution_time,
                    dispatch_kind=dispatch_kind,
                    target_url=target.webhook_url,
                    payload=payload,
                    status="failed" if is_last else "retrying",
                    http_status=getattr(exc, "http_status", None),
                    error_text=str(exc),
                    idempotency_key=attempt_key,
                )

    def _record_dispatch_event(
        self,
        *,
        run_id: int,
        resource_id: str,
        execution_time: datetime | None,
        dispatch_kind: str,
        target_url: str | None,
        payload: dict[str, Any],
        status: str,
        http_status: int | None,
        error_text: str | None,
        idempotency_key: str,
    ) -> None:
        try:
            with self._session_factory() as db:
                create_output_dispatch_event(
                    db,
                    run_id=run_id,
                    resource_id=resource_id,
                    execution_time=execution_time,
                    dispatch_kind=dispatch_kind,
                    target_url=target_url,
                    request_payload_json=payload,
                    status=status,
                    http_status=http_status,
                    error_text=error_text,
                    idempotency_key=idempotency_key,
                )
        except Exception:
            self._logger.exception(
                "failed to persist output dispatch event run_id=%s resource_id=%s kind=%s",
                run_id,
                resource_id,
                dispatch_kind,
            )

    def _load_run_instructions(
        self,
        db: Session,
        *,
        run_id: int | None,
    ) -> tuple[int | None, list[NormalizedInstruction]]:
        selected_run_id = run_id
        if selected_run_id is None:
            run = get_latest_successful_run_with_plan(db)
            if run is None:
                return None, []
            selected_run_id = int(run.id)
        instructions = _normalize_instructions(list_plan_instructions_for_run(db, selected_run_id))
        return selected_run_id, instructions

    def _safety_status(
        self,
        *,
        instruction: NormalizedInstruction,
        latest_grid_import_w: float | None,
    ) -> str:
        if self._is_blocked_by_grid_charge_guard(
            instruction=instruction,
            latest_grid_import_w=latest_grid_import_w,
        ):
            return "blocked_no_grid_charge"
        return "ok"

    def _is_blocked_by_grid_charge_guard(
        self,
        *,
        instruction: NormalizedInstruction,
        latest_grid_import_w: float | None,
    ) -> bool:
        if not self._settings.eos_no_grid_charge_guard_enabled:
            return False
        if latest_grid_import_w is None:
            return False
        if latest_grid_import_w <= max(0.0, self._settings.eos_no_grid_charge_guard_threshold_w):
            return False
        if "battery" not in instruction.resource_id.lower() and "lfp" not in instruction.resource_id.lower():
            return False
        return _looks_like_charge_instruction(
            instruction_type=instruction.instruction_type,
            operation_mode_id=instruction.operation_mode_id,
            operation_mode_factor=instruction.operation_mode_factor,
        )

    def _set_runtime_status(self, *, status: str, error: str | None, tick_ts: datetime) -> None:
        with self._lock:
            self._last_tick_ts = tick_ts
            self._last_status = status
            self._last_error = error

    def _set_last_run_id(self, run_id: int) -> None:
        with self._lock:
            self._last_run_id = run_id


class _HttpRequestError(RuntimeError):
    def __init__(self, *, message: str, http_status: int | None = None):
        self.http_status = http_status
        super().__init__(message)


def _send_webhook_request(
    *,
    url: str,
    method: str,
    payload: dict[str, Any],
    headers: dict[str, Any] | list[Any] | None,
    timeout_seconds: int,
    idempotency_key: str,
) -> tuple[int, str]:
    safe_method = (method or "POST").upper().strip()
    if safe_method not in {"POST", "PUT", "PATCH"}:
        raise _HttpRequestError(message=f"unsupported method '{safe_method}'")

    encoded = json.dumps(payload).encode("utf-8")
    request_headers: dict[str, str] = {}
    if isinstance(headers, dict):
        for key, value in headers.items():
            if value is None:
                continue
            request_headers[str(key)] = str(value)
    if "Content-Type" not in request_headers:
        request_headers["Content-Type"] = "application/json"
    request_headers["X-Idempotency-Key"] = idempotency_key

    req = Request(url=url, data=encoded, headers=request_headers, method=safe_method)
    try:
        with urlopen(req, timeout=max(1.0, float(timeout_seconds))) as response:
            body = response.read().decode("utf-8", errors="replace")
            if response.status < 200 or response.status >= 300:
                raise _HttpRequestError(
                    message=f"unexpected http status {response.status}: {body}",
                    http_status=response.status,
                )
            return response.status, body
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise _HttpRequestError(
            message=f"http error {exc.code}: {body}",
            http_status=exc.code,
        )
    except URLError as exc:
        raise _HttpRequestError(message=f"connection error: {exc}")
    except TimeoutError as exc:
        raise _HttpRequestError(message=f"timeout: {exc}")


def _normalize_instructions(rows: list[EosPlanInstruction]) -> list[NormalizedInstruction]:
    instructions: list[NormalizedInstruction] = []
    for row in rows:
        resource_id = _safe_str(row.resource_id)
        if resource_id is None:
            continue
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        effective = _to_utc(row.execution_time) or _to_utc(row.starts_at)
        starts_at = _to_utc(row.starts_at)
        ends_at = _to_utc(row.ends_at)
        instructions.append(
            NormalizedInstruction(
                id=int(row.id),
                run_id=int(row.run_id),
                instruction_index=int(row.instruction_index),
                instruction_type=str(row.instruction_type),
                resource_id=resource_id,
                actuator_id=_safe_str(row.actuator_id),
                starts_at=starts_at,
                ends_at=ends_at,
                execution_time=effective,
                operation_mode_id=_safe_str(row.operation_mode_id),
                operation_mode_factor=_to_float(row.operation_mode_factor),
                payload_json=payload,
            )
        )

    instructions.sort(
        key=lambda item: (
            item.resource_id,
            item.execution_time or datetime.min.replace(tzinfo=timezone.utc),
            item.instruction_index,
        )
    )
    return instructions


def _build_deduped_timeline(
    instructions: list[NormalizedInstruction],
    *,
    from_ts: datetime | None,
    to_ts: datetime | None,
    resource_id: str | None,
) -> list[NormalizedInstruction]:
    result: list[NormalizedInstruction] = []
    last_signature_by_resource: dict[str, tuple[Any, ...]] = {}

    for instruction in instructions:
        if resource_id is not None and instruction.resource_id != resource_id:
            continue
        if from_ts is not None and instruction.execution_time is not None and instruction.execution_time < from_ts:
            continue
        if to_ts is not None and instruction.execution_time is not None and instruction.execution_time > to_ts:
            continue

        signature = (
            instruction.instruction_type,
            instruction.operation_mode_id,
            instruction.operation_mode_factor,
            instruction.actuator_id,
        )
        if last_signature_by_resource.get(instruction.resource_id) == signature:
            continue
        last_signature_by_resource[instruction.resource_id] = signature
        result.append(instruction)

    result.sort(
        key=lambda item: (
            item.execution_time or datetime.max.replace(tzinfo=timezone.utc),
            item.resource_id,
            item.instruction_index,
        )
    )
    return result


def _select_current_by_resource(
    instructions: list[NormalizedInstruction],
    *,
    at: datetime,
) -> list[NormalizedInstruction]:
    current_by_resource: dict[str, NormalizedInstruction] = {}

    for instruction in instructions:
        effective = instruction.execution_time or instruction.starts_at
        if effective is not None and effective > at:
            continue
        if instruction.ends_at is not None and instruction.ends_at <= at:
            continue

        existing = current_by_resource.get(instruction.resource_id)
        if existing is None:
            current_by_resource[instruction.resource_id] = instruction
            continue

        existing_effective = existing.execution_time or existing.starts_at or datetime.min.replace(tzinfo=timezone.utc)
        current_effective = effective or datetime.min.replace(tzinfo=timezone.utc)
        if current_effective > existing_effective:
            current_by_resource[instruction.resource_id] = instruction
        elif current_effective == existing_effective and instruction.instruction_index >= existing.instruction_index:
            current_by_resource[instruction.resource_id] = instruction

    return sorted(current_by_resource.values(), key=lambda item: item.resource_id)


def _build_dispatch_payload(
    *,
    run_id: int,
    dispatch_kind: str,
    instruction: NormalizedInstruction,
    generated_at: datetime,
    template: dict[str, Any] | list[Any] | None,
) -> dict[str, Any]:
    payload = {
        "run_id": run_id,
        "dispatch_kind": dispatch_kind,
        "generated_at": generated_at.isoformat(),
        "resource_id": instruction.resource_id,
        "actuator_id": instruction.actuator_id,
        "instruction_type": instruction.instruction_type,
        "operation_mode_id": instruction.operation_mode_id,
        "operation_mode_factor": instruction.operation_mode_factor,
        "effective_at": _to_iso(instruction.execution_time),
        "starts_at": _to_iso(instruction.starts_at),
        "ends_at": _to_iso(instruction.ends_at),
        "instruction": instruction.payload_json,
    }
    if isinstance(template, dict):
        merged = dict(template)
        merged.update(payload)
        return merged
    return payload


def _looks_like_charge_instruction(
    *,
    instruction_type: str,
    operation_mode_id: str | None,
    operation_mode_factor: float | None,
) -> bool:
    instruction_lower = instruction_type.lower()
    if "discharge" in instruction_lower or "entladen" in instruction_lower:
        return False
    if "charge" in instruction_lower or "laden" in instruction_lower:
        return True

    if operation_mode_factor is not None and operation_mode_factor > 0.0:
        return True

    mode_lower = (operation_mode_id or "").lower()
    if "discharge" in mode_lower or "entladen" in mode_lower:
        return False
    if "charge" in mode_lower or "laden" in mode_lower:
        return True

    return False


def _read_latest_grid_import_w(db: Session) -> float | None:
    rows = get_latest_power_samples(db, keys=["grid_import_w"])
    for row in rows:
        if row.get("key") != "grid_import_w":
            continue
        raw_value = row.get("value_w")
        try:
            return float(raw_value) if raw_value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _to_utc(value).isoformat()
