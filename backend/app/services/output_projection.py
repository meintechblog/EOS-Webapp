from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import EosPlanInstruction
from app.repositories.emr_pipeline import get_latest_power_samples
from app.repositories.eos_runtime import (
    get_latest_successful_run_with_plan,
    get_run_input_snapshot,
    list_output_signal_access_states,
    list_plan_instructions_for_run,
    upsert_output_signal_access_state,
)

CENTRAL_OUTPUTS_HTTP_PATH = "/eos/get/outputs"


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


class OutputProjectionService:
    def __init__(
        self,
        *,
        settings: Settings,
    ) -> None:
        self._settings = settings

    def get_status_snapshot(self) -> dict[str, Any]:
        return {"enabled": True, "mode": "pull_projection"}

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

        max_power_kw_by_resource, _resource_kind_by_resource = self._extract_resource_maps(
            db, run_id=selected_run_id
        )
        latest_grid_import_w = _read_latest_grid_import_w(db)
        current = _select_current_by_resource(instructions, at=now)
        items: list[dict[str, Any]] = []
        for instruction in current:
            requested_power_kw = _to_requested_power_kw(
                resource_id=instruction.resource_id,
                operation_mode_id=instruction.operation_mode_id,
                operation_mode_factor=instruction.operation_mode_factor,
                max_power_kw_by_resource=max_power_kw_by_resource,
            )
            items.append(
                {
                    "run_id": selected_run_id,
                    "resource_id": instruction.resource_id,
                    "actuator_id": instruction.actuator_id,
                    "operation_mode_id": instruction.operation_mode_id,
                    "operation_mode_factor": instruction.operation_mode_factor,
                    "requested_power_kw": requested_power_kw,
                    "effective_at": instruction.execution_time,
                    "source_instruction": instruction.payload_json,
                    "safety_status": self._safety_status(
                        instruction=instruction,
                        latest_grid_import_w=latest_grid_import_w,
                    ),
                }
            )
        return selected_run_id, items

    def resolve_dispatchable_run_id(
        self,
        db: Session,
        *,
        run_id: int | None = None,
    ) -> int | None:
        selected_run_id, _ = self._load_run_instructions(db, run_id=run_id)
        return selected_run_id

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

        max_power_kw_by_resource, _resource_kind_by_resource = self._extract_resource_maps(
            db, run_id=selected_run_id
        )
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
                    "requested_power_kw": _to_requested_power_kw(
                        resource_id=instruction.resource_id,
                        operation_mode_id=instruction.operation_mode_id,
                        operation_mode_factor=instruction.operation_mode_factor,
                        max_power_kw_by_resource=max_power_kw_by_resource,
                    ),
                    "execution_time": instruction.execution_time,
                    "starts_at": instruction.starts_at,
                    "ends_at": instruction.ends_at,
                    "source_instruction": instruction.payload_json,
                    "deduped": True,
                }
            )
        return selected_run_id, rows

    def resolve_output_bundle(
        self,
        db: Session,
        *,
        run_id: int | None = None,
    ) -> dict[str, Any]:
        fetched_at = datetime.now(timezone.utc)
        selected_run_id, entries = self._build_signal_entries(db, run_id=run_id)
        if not entries:
            return {
                "central_http_path": CENTRAL_OUTPUTS_HTTP_PATH,
                "run_id": selected_run_id,
                "fetched_at": fetched_at,
                "signals": {},
            }

        access_rows = list_output_signal_access_states(db, signal_keys=list(entries.keys()))
        access_by_signal = {row.signal_key: row for row in access_rows}

        signals: dict[str, dict[str, Any]] = {}
        for key in sorted(entries.keys()):
            signals[key] = _apply_access_state(
                signal_row=dict(entries[key]),
                access_row=access_by_signal.get(key),
            )

        return {
            "central_http_path": CENTRAL_OUTPUTS_HTTP_PATH,
            "run_id": selected_run_id,
            "fetched_at": fetched_at,
            "signals": signals,
        }

    def record_bundle_fetch(
        self,
        db: Session,
        *,
        signal_entries: dict[str, dict[str, Any]],
        client: str | None,
    ) -> dict[str, dict[str, Any]]:
        if not signal_entries:
            return {}

        now = datetime.now(timezone.utc)
        normalized_client = _normalize_client_id(client)
        state_by_signal: dict[str, dict[str, Any]] = {}
        for signal_key, signal_row in signal_entries.items():
            state = upsert_output_signal_access_state(
                db,
                signal_key=_normalize_signal_key(signal_key),
                resource_id=_safe_str(signal_row.get("resource_id")),
                last_fetch_ts=now,
                last_fetch_client=normalized_client,
            )
            state_by_signal[signal_key] = {
                "last_fetch_ts": state.last_fetch_ts,
                "last_fetch_client": state.last_fetch_client,
                "fetch_count": int(state.fetch_count),
            }
        return state_by_signal

    @staticmethod
    def apply_fetch_state_to_bundle(
        bundle: dict[str, Any],
        fetch_state_by_signal: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if not fetch_state_by_signal:
            return bundle
        signals = bundle.get("signals")
        if not isinstance(signals, dict):
            return bundle
        for signal_key, state in fetch_state_by_signal.items():
            row = signals.get(signal_key)
            if not isinstance(row, dict):
                continue
            row["last_fetch_ts"] = state.get("last_fetch_ts")
            row["last_fetch_client"] = state.get("last_fetch_client")
            row["fetch_count"] = int(state.get("fetch_count") or 0)
        return bundle

    def _build_signal_entries(
        self,
        db: Session,
        *,
        run_id: int | None = None,
    ) -> tuple[int | None, dict[str, dict[str, Any]]]:
        selected_run_id, current_rows = self.get_current_outputs(db, run_id=run_id)
        if selected_run_id is None:
            return None, {}

        entries: dict[str, dict[str, Any]] = {}
        for current in current_rows:
            resource_id = str(current["resource_id"])
            signal_key = _resource_signal_key(resource_id)
            requested_power_kw = current.get("requested_power_kw")
            status = "ok" if requested_power_kw is not None else "missing_max_power"
            entries[signal_key] = {
                "signal_key": signal_key,
                "label": f"{resource_id} Soll-Leistung",
                "resource_id": resource_id,
                "requested_power_kw": requested_power_kw,
                "unit": "kW",
                "operation_mode_id": current.get("operation_mode_id"),
                "operation_mode_factor": current.get("operation_mode_factor"),
                "effective_at": current.get("effective_at"),
                "run_id": selected_run_id,
                "status": status,
                "json_path_value": _output_signal_json_path(signal_key),
            }

        return selected_run_id, entries

    def _load_run_instructions(
        self,
        db: Session,
        *,
        run_id: int | None,
    ) -> tuple[int | None, list[NormalizedInstruction]]:
        if run_id is not None:
            requested_instructions = _normalize_instructions(list_plan_instructions_for_run(db, run_id))
            if requested_instructions:
                return run_id, requested_instructions

            fallback_run = get_latest_successful_run_with_plan(db)
            if fallback_run is None:
                return run_id, []
            fallback_run_id = int(fallback_run.id)
            if fallback_run_id == run_id:
                return run_id, []
            fallback_instructions = _normalize_instructions(
                list_plan_instructions_for_run(db, fallback_run_id)
            )
            if fallback_instructions:
                return fallback_run_id, fallback_instructions
            return run_id, []

        run = get_latest_successful_run_with_plan(db)
        if run is None:
            return None, []
        selected_run_id = int(run.id)
        instructions = _normalize_instructions(list_plan_instructions_for_run(db, selected_run_id))
        return selected_run_id, instructions

    def _extract_resource_maps(
        self,
        db: Session,
        *,
        run_id: int,
    ) -> tuple[dict[str, float], dict[str, str]]:
        snapshot = get_run_input_snapshot(db, run_id)
        payload = snapshot.runtime_config_snapshot_json if snapshot is not None else None
        if not isinstance(payload, dict):
            return {}, {}
        devices = payload.get("devices")
        if not isinstance(devices, dict):
            return {}, {}

        max_power_kw_by_resource: dict[str, float] = {}
        resource_kind_by_resource: dict[str, str] = {}

        def put(key: str, *, max_kw: float | None, resource_kind: str) -> None:
            if max_kw is None or not isinstance(max_kw, float) or not (max_kw > 0.0):
                return
            normalized = _normalize_resource_id(key)
            if normalized == "":
                return
            max_power_kw_by_resource[normalized] = max_kw
            resource_kind_by_resource[normalized] = resource_kind

        batteries = devices.get("batteries")
        if isinstance(batteries, list):
            for index, raw in enumerate(batteries):
                if not isinstance(raw, dict):
                    continue
                max_kw = _to_positive_kw(raw.get("max_charge_power_w"))
                put(f"battery{index + 1}", max_kw=max_kw, resource_kind="battery")
                device_id = _safe_str(raw.get("device_id"))
                if device_id is not None:
                    put(device_id, max_kw=max_kw, resource_kind="battery")

        electric_vehicles = devices.get("electric_vehicles")
        if isinstance(electric_vehicles, list):
            for index, raw in enumerate(electric_vehicles):
                if not isinstance(raw, dict):
                    continue
                max_kw = _to_positive_kw(raw.get("max_charge_power_w"))
                put(f"ev{index + 1}", max_kw=max_kw, resource_kind="ev")
                put(f"electric_vehicle{index + 1}", max_kw=max_kw, resource_kind="ev")
                device_id = _safe_str(raw.get("device_id"))
                if device_id is not None:
                    put(device_id, max_kw=max_kw, resource_kind="ev")

        return max_power_kw_by_resource, resource_kind_by_resource

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
            operation_mode_id=instruction.operation_mode_id,
            operation_mode_factor=instruction.operation_mode_factor,
        )


def _to_positive_kw(value: Any) -> float | None:
    numeric = _to_float(value)
    if numeric is None or numeric <= 0.0:
        return None
    return numeric / 1000.0


def _normalize_client_id(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _mode_direction(operation_mode_id: str | None) -> int:
    mode = (operation_mode_id or "").strip().upper()
    if mode in {"", "UNKNOWN", "IDLE", "NONE", "SELF_CONSUMPTION", "NON_EXPORT"}:
        return 0
    if (
        "FORCED_DISCHARGE" in mode
        or "GRID_SUPPORT_EXPORT" in mode
        or "DISCHARGE" in mode
        or "EXPORT" in mode
    ):
        return -1
    if (
        "FORCED_CHARGE" in mode
        or "GRID_SUPPORT_IMPORT" in mode
        or "CHARGE" in mode
        or "IMPORT" in mode
    ):
        return 1
    return 0


def _to_requested_power_kw(
    *,
    resource_id: str,
    operation_mode_id: str | None,
    operation_mode_factor: float | None,
    max_power_kw_by_resource: dict[str, float],
) -> float | None:
    max_power_kw = max_power_kw_by_resource.get(_normalize_resource_id(resource_id))
    if max_power_kw is None:
        return None
    direction = _mode_direction(operation_mode_id)
    if direction == 0:
        return 0.0
    factor = abs(operation_mode_factor) if operation_mode_factor is not None else 1.0
    return float(direction) * factor * max_power_kw


def _resource_signal_key(resource_id: str) -> str:
    normalized_resource = _normalize_resource_id(resource_id)
    if normalized_resource == "":
        normalized_resource = "resource"
    return f"{normalized_resource}_target_power_kw"


def _normalize_resource_id(value: str) -> str:
    lowered = value.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", "_", lowered)
    return lowered.strip("_")


def _normalize_signal_key(value: str) -> str:
    return _normalize_resource_id(value)


def _output_signal_json_path(signal_key: str) -> str:
    return f"signals.{signal_key}.requested_power_kw"


def _apply_access_state(
    *,
    signal_row: dict[str, Any],
    access_row: Any | None,
) -> dict[str, Any]:
    if access_row is None:
        signal_row["last_fetch_ts"] = None
        signal_row["last_fetch_client"] = None
        signal_row["fetch_count"] = 0
        return signal_row

    signal_row["last_fetch_ts"] = access_row.last_fetch_ts
    signal_row["last_fetch_client"] = access_row.last_fetch_client
    signal_row["fetch_count"] = int(access_row.fetch_count or 0)
    return signal_row


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


def _looks_like_charge_instruction(
    *,
    operation_mode_id: str | None,
    operation_mode_factor: float | None,
) -> bool:
    if operation_mode_factor is not None and operation_mode_factor > 0.0:
        direction = _mode_direction(operation_mode_id)
        return direction > 0
    direction = _mode_direction(operation_mode_id)
    return direction > 0


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
