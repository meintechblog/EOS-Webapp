from __future__ import annotations

import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.repositories.emr_pipeline import (
    EMR_KEY_BY_POWER_KEY,
    POWER_KEYS,
    get_latest_emr_state,
    get_power_sample_value_by_key_ts,
    update_power_sample_value_by_key_ts,
    upsert_energy_emr,
    upsert_power_sample,
)
from app.repositories.signal_backbone import ingest_signal_measurement


class EmrPipelineService:
    _HTTP_SOURCE = "http"

    def __init__(self, *, settings: Settings, session_factory: sessionmaker):
        self._settings = settings
        self._session_factory = session_factory
        self._logger = logging.getLogger("app.emr_pipeline")
        self._lock = Lock()
        self._last_emr_update_ts: datetime | None = None
        self._last_error: str | None = None
        self._tracked_keys = [
            "house_load_w",
            "pv_power_w",
            "grid_import_w",
            "grid_export_w",
            "battery_power_w",
        ]

    def process_signal_value(
        self,
        *,
        signal_key: str,
        value: Any,
        source_ts: datetime,
        source: str,
        raw_payload: str | None = None,
    ) -> None:
        if not self._settings.emr_enabled:
            return

        source_ts_utc = _to_utc(source_ts)
        power_values = self._derive_power_values(
            eos_field=signal_key,
            transformed_value=value,
        )
        if not power_values:
            return

        source_label = source.strip().lower() if isinstance(source, str) else ""
        if source_label == "http":
            persisted_source = self._HTTP_SOURCE
        elif source_label:
            persisted_source = source_label
        else:
            persisted_source = self._HTTP_SOURCE

        self._process_power_values(
            power_values=power_values,
            source_ts=source_ts_utc,
            source=persisted_source,
            raw_payload=raw_payload if raw_payload is not None else str(value),
            log_context=f"signal_key={signal_key}",
        )

    def _process_power_values(
        self,
        *,
        power_values: dict[str, float],
        source_ts: datetime,
        source: str,
        raw_payload: str | None,
        log_context: str,
    ) -> None:
        try:
            with self._session_factory() as db:
                notes_by_key: dict[str, list[str]] = {}
                normalized_values: dict[str, float] = {}

                for key, raw_value in power_values.items():
                    normalized_value, note = self._normalize_power_value(
                        key=key,
                        value_w=raw_value,
                    )
                    upsert_power_sample(
                        db,
                        ts=source_ts,
                        key=key,
                        value_w=normalized_value,
                        source=source,
                        quality="ok",
                        raw_payload=raw_payload,
                    )
                    normalized_values[key] = normalized_value
                    if note:
                        notes_by_key.setdefault(key, []).append(note)

                grid_conflict_note = self._resolve_grid_conflict_if_any(db=db, ts=source_ts)
                if grid_conflict_note:
                    notes_by_key.setdefault("grid_import_w", []).append(grid_conflict_note)
                    notes_by_key.setdefault("grid_export_w", []).append(grid_conflict_note)
                    for key in ("grid_import_w", "grid_export_w"):
                        latest = get_power_sample_value_by_key_ts(db, key=key, ts=source_ts)
                        if latest is not None:
                            normalized_values[key] = latest

                for power_key, current_power_w in normalized_values.items():
                    emr_key = EMR_KEY_BY_POWER_KEY.get(power_key)
                    if emr_key is None:
                        continue
                    self._advance_emr(
                        db=db,
                        power_key=power_key,
                        emr_key=emr_key,
                        current_power_w=current_power_w,
                        ts=source_ts,
                        notes=notes_by_key.get(power_key, []),
                    )

                db.commit()

            with self._lock:
                self._last_emr_update_ts = datetime.now(timezone.utc)
                self._last_error = None
        except Exception as exc:
            self._logger.exception("emr processing failed %s", log_context)
            with self._lock:
                self._last_error = str(exc)

    def get_status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._settings.emr_enabled,
                "tracked_keys": list(self._tracked_keys),
                "last_emr_update_ts": _iso(self._last_emr_update_ts),
                "last_error": self._last_error,
            }

    def _advance_emr(
        self,
        *,
        db,
        power_key: str,
        emr_key: str,
        current_power_w: float,
        ts: datetime,
        notes: list[str],
    ) -> None:
        previous = get_latest_emr_state(db, emr_key=emr_key)

        if previous is None:
            note_parts = ["seed_initial_state"] + notes
            emr_kwh = 0.0
            upsert_energy_emr(
                db,
                ts=ts,
                emr_key=emr_key,
                emr_kwh=emr_kwh,
                last_power_w=current_power_w,
                last_ts=ts,
                method="hold",
                notes=_join_notes(note_parts),
            )
            ingest_signal_measurement(
                db,
                signal_key=emr_key,
                label=emr_key,
                value_type="number",
                canonical_unit="kWh",
                value=emr_kwh,
                ts=ts,
                quality_status="derived",
                source_type="derived",
                run_id=None,
                source_ref_id=None,
                tags_json={"source": "emr_pipeline", "power_key": power_key},
            )
            return

        reference_ts = _to_utc(previous.last_ts or previous.ts)
        if ts < reference_ts:
            self._logger.warning(
                "skip out-of-order emr sample emr_key=%s ts=%s reference_ts=%s",
                emr_key,
                ts.isoformat(),
                reference_ts.isoformat(),
            )
            return

        delta_seconds = max(0.0, (ts - reference_ts).total_seconds())
        previous_emr = previous.emr_kwh
        previous_power = max(0.0, float(previous.last_power_w or 0.0))
        emr_kwh = previous_emr
        method = "hold"
        note_parts = list(notes)

        if delta_seconds < self._settings.emr_delta_min_seconds:
            note_parts.append("delta_below_min")
        elif delta_seconds <= self._settings.emr_hold_max_seconds:
            delta_kwh = (previous_power * delta_seconds) / 3_600_000.0
            emr_kwh = previous_emr + max(0.0, delta_kwh)
            method = "integrate"
        elif delta_seconds <= self._settings.emr_delta_max_seconds:
            note_parts.append("gap_no_integrate")
        else:
            note_parts.append("delta_out_of_range")

        emr_kwh = max(previous_emr, emr_kwh)

        upsert_energy_emr(
            db,
            ts=ts,
            emr_key=emr_key,
            emr_kwh=emr_kwh,
            last_power_w=current_power_w,
            last_ts=ts,
            method=method,
            notes=_join_notes(note_parts),
        )
        ingest_signal_measurement(
            db,
            signal_key=emr_key,
            label=emr_key,
            value_type="number",
            canonical_unit="kWh",
            value=emr_kwh,
            ts=ts,
            quality_status="derived",
            source_type="derived",
            run_id=None,
            source_ref_id=None,
            tags_json={"source": "emr_pipeline", "power_key": power_key},
        )

    def _resolve_grid_conflict_if_any(self, *, db, ts: datetime) -> str | None:
        import_value = get_power_sample_value_by_key_ts(db, key="grid_import_w", ts=ts)
        export_value = get_power_sample_value_by_key_ts(db, key="grid_export_w", ts=ts)
        if import_value is None or export_value is None:
            return None
        threshold = max(0.0, float(self._settings.emr_grid_conflict_threshold_w))
        if import_value <= threshold or export_value <= threshold:
            return None

        if import_value >= export_value:
            normalized_import = import_value
            normalized_export = 0.0
            note = "grid_conflict_resolved_keep_import"
        else:
            normalized_import = 0.0
            normalized_export = export_value
            note = "grid_conflict_resolved_keep_export"

        update_power_sample_value_by_key_ts(
            db,
            key="grid_import_w",
            ts=ts,
            value_w=normalized_import,
            quality="ok",
        )
        update_power_sample_value_by_key_ts(
            db,
            key="grid_export_w",
            ts=ts,
            value_w=normalized_export,
            quality="ok",
        )
        return note

    def _derive_power_values(
        self,
        *,
        eos_field: str,
        transformed_value: Any,
    ) -> dict[str, float]:
        numeric_value = _as_float(transformed_value)
        if numeric_value is None:
            return {}

        if eos_field in POWER_KEYS:
            return {eos_field: numeric_value}

        if eos_field == "grid_power_w":
            return {
                "grid_import_w": max(0.0, numeric_value),
                "grid_export_w": max(0.0, -numeric_value),
            }
        return {}

    def _normalize_power_value(self, *, key: str, value_w: float) -> tuple[float, str | None]:
        notes: list[str] = []
        normalized = value_w

        if key == "battery_power_w":
            if normalized < self._settings.emr_battery_power_min_w:
                normalized = self._settings.emr_battery_power_min_w
                notes.append("battery_clamped_min")
            if normalized > self._settings.emr_battery_power_max_w:
                normalized = self._settings.emr_battery_power_max_w
                notes.append("battery_clamped_max")
            return normalized, _join_notes(notes)

        min_value = max(0.0, float(self._settings.emr_power_min_w))
        max_value = float(self._settings.emr_power_max_w)
        if key == "house_load_w":
            max_value = min(max_value, float(self._settings.emr_house_power_max_w))
        elif key == "pv_power_w":
            max_value = min(max_value, float(self._settings.emr_pv_power_max_w))
        elif key in ("grid_import_w", "grid_export_w"):
            max_value = min(max_value, float(self._settings.emr_grid_power_max_w))

        if normalized < min_value:
            normalized = min_value
            notes.append("clamped_min")
        if normalized > max_value:
            normalized = max_value
            notes.append("clamped_max")

        return normalized, _join_notes(notes)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _join_notes(parts: list[str]) -> str | None:
    filtered = [part for part in parts if part]
    if not filtered:
        return None
    return ";".join(filtered)
