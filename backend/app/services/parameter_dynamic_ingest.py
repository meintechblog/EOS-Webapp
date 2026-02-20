from __future__ import annotations

import copy
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock, Timer
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.models import InputChannel, ParameterBinding
from app.repositories.parameter_bindings import (
    bulk_update_parameter_input_event_status,
    create_parameter_input_event,
    get_parameter_binding_by_channel_input_key,
)
from app.repositories.parameter_profiles import (
    get_active_parameter_profile,
    get_current_draft_revision,
)
from app.repositories.signal_backbone import infer_value_type, ingest_signal_measurement
from app.repositories.topic_observations import normalize_param_key, upsert_input_observation
from app.services.parameter_dynamic_catalog import (
    DynamicParameterCatalogEntry,
    ParameterDynamicCatalogService,
)
from app.services.parameter_profiles import ParameterProfileService
from app.services.payload_parser import parse_event_timestamp, parse_payload


@dataclass(frozen=True)
class ParameterDynamicIngestResult:
    accepted: bool
    channel_id: int
    channel_code: str
    channel_type: str
    input_key: str
    normalized_key: str
    binding_matched: bool
    binding_id: int | None
    event_id: int | None
    event_ts: datetime
    apply_status: str
    error_text: str | None


class ParameterDynamicIngestService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker,
        catalog_service: ParameterDynamicCatalogService,
        profile_service: ParameterProfileService,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._catalog_service = catalog_service
        self._profile_service = profile_service
        self._logger = logging.getLogger("app.parameter_dynamic_ingest")

        self._status_lock = Lock()
        self._debounce_lock = Lock()
        self._pending_apply_events: dict[int, set[int]] = {}
        self._debounce_timer: Timer | None = None

        self._last_dynamic_event_ts: datetime | None = None
        self._last_apply_ts: datetime | None = None
        self._last_error: str | None = None

    def stop(self) -> None:
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None

    def normalize_key(self, raw_key: str) -> str:
        return normalize_param_key(raw_key)

    def ingest(
        self,
        *,
        channel: InputChannel,
        input_key: str,
        payload_text: str,
        event_received_ts: datetime,
        metadata: dict[str, Any] | None = None,
        explicit_timestamp: datetime | None = None,
    ) -> ParameterDynamicIngestResult:
        if not self._settings.param_dynamic_enabled:
            return ParameterDynamicIngestResult(
                accepted=False,
                channel_id=channel.id,
                channel_code=channel.code,
                channel_type=channel.channel_type,
                input_key=input_key,
                normalized_key=self.normalize_key(input_key),
                binding_matched=False,
                binding_id=None,
                event_id=None,
                event_ts=explicit_timestamp or _to_utc(event_received_ts),
                apply_status="ignored_unbound",
                error_text="Dynamic parameter ingest disabled",
            )

        received_ts = _to_utc(event_received_ts)
        normalized_key = self.normalize_key(input_key)
        meta = metadata if isinstance(metadata, dict) else {}

        with self._session_factory() as db:
            upsert_input_observation(
                db,
                channel_id=channel.id,
                input_key=input_key,
                normalized_key=normalized_key,
                payload=payload_text,
                last_meta_json={**meta, "namespace": "param"},
                event_ts=received_ts,
            )
            binding = get_parameter_binding_by_channel_input_key(
                db,
                channel_id=channel.id,
                input_key=normalized_key,
            )

        if binding is None or not binding.enabled:
            with self._session_factory() as db:
                event = create_parameter_input_event(
                    db,
                    binding_id=binding.id if binding is not None else None,
                    channel_id=channel.id,
                    input_key=input_key,
                    normalized_key=normalized_key,
                    raw_payload=payload_text,
                    parsed_value_text=None,
                    event_ts=explicit_timestamp or received_ts,
                    revision_id=None,
                    apply_status="ignored_unbound",
                    error_text="No enabled parameter binding for key",
                    meta_json=meta,
                )
            self._set_last_dynamic_event(event.event_ts, None)
            return ParameterDynamicIngestResult(
                accepted=True,
                channel_id=channel.id,
                channel_code=channel.code,
                channel_type=channel.channel_type,
                input_key=input_key,
                normalized_key=normalized_key,
                binding_matched=False,
                binding_id=binding.id if binding is not None else None,
                event_id=event.id,
                event_ts=event.event_ts,
                apply_status=event.apply_status,
                error_text=event.error_text,
            )

        timestamp_fallback = explicit_timestamp or received_ts
        event_ts = parse_event_timestamp(
            payload_text,
            binding.timestamp_path,
            fallback_ts=timestamp_fallback,
            logger=self._logger,
        )
        parsed_value = parse_payload(payload_text, binding.payload_path, logger=self._logger)

        catalog_entry = self._catalog_service.get_entry(binding.parameter_key)
        if catalog_entry is None:
            with self._session_factory() as db:
                event = create_parameter_input_event(
                    db,
                    binding_id=binding.id,
                    channel_id=channel.id,
                    input_key=input_key,
                    normalized_key=normalized_key,
                    raw_payload=payload_text,
                    parsed_value_text=parsed_value,
                    event_ts=event_ts,
                    revision_id=None,
                    apply_status="rejected",
                    error_text=f"parameter_key '{binding.parameter_key}' is not in dynamic whitelist",
                    meta_json=meta,
                )
            self._set_last_dynamic_event(event_ts, event.error_text)
            return self._result_from_event(channel=channel, input_key=input_key, normalized_key=normalized_key, event=event)

        converted_value, converted_preview, validation_error = self._convert_value(
            parsed_value=parsed_value,
            binding=binding,
            entry=catalog_entry,
        )
        if validation_error is not None:
            with self._session_factory() as db:
                event = create_parameter_input_event(
                    db,
                    binding_id=binding.id,
                    channel_id=channel.id,
                    input_key=input_key,
                    normalized_key=normalized_key,
                    raw_payload=payload_text,
                    parsed_value_text=converted_preview,
                    event_ts=event_ts,
                    revision_id=None,
                    apply_status="rejected",
                    error_text=validation_error,
                    meta_json=meta,
                )
            self._set_last_dynamic_event(event_ts, validation_error)
            return self._result_from_event(channel=channel, input_key=input_key, normalized_key=normalized_key, event=event)

        with self._session_factory() as db:
            active_profile = get_active_parameter_profile(db)
            if active_profile is None:
                event = create_parameter_input_event(
                    db,
                    binding_id=binding.id,
                    channel_id=channel.id,
                    input_key=input_key,
                    normalized_key=normalized_key,
                    raw_payload=payload_text,
                    parsed_value_text=converted_preview,
                    event_ts=event_ts,
                    revision_id=None,
                    apply_status="blocked_no_active_profile",
                    error_text="No active parameter profile available",
                    meta_json=meta,
                )
                self._set_last_dynamic_event(event_ts, event.error_text)
                return self._result_from_event(
                    channel=channel,
                    input_key=input_key,
                    normalized_key=normalized_key,
                    event=event,
                )

            draft = get_current_draft_revision(db, profile_id=active_profile.id)
            if draft is None or not isinstance(draft.payload_json, dict):
                event = create_parameter_input_event(
                    db,
                    binding_id=binding.id,
                    channel_id=channel.id,
                    input_key=input_key,
                    normalized_key=normalized_key,
                    raw_payload=payload_text,
                    parsed_value_text=converted_preview,
                    event_ts=event_ts,
                    revision_id=None,
                    apply_status="blocked_no_active_profile",
                    error_text="Active profile has no current draft payload",
                    meta_json=meta,
                )
                self._set_last_dynamic_event(event_ts, event.error_text)
                return self._result_from_event(
                    channel=channel,
                    input_key=input_key,
                    normalized_key=normalized_key,
                    event=event,
                )

            next_payload = copy.deepcopy(draft.payload_json)
            set_error = _set_parameter_value(
                payload=next_payload,
                parameter_key=binding.parameter_key,
                selector_value=binding.selector_value,
                value=converted_value,
                requires_selector=catalog_entry.requires_selector,
            )
            if set_error is not None:
                event = create_parameter_input_event(
                    db,
                    binding_id=binding.id,
                    channel_id=channel.id,
                    input_key=input_key,
                    normalized_key=normalized_key,
                    raw_payload=payload_text,
                    parsed_value_text=converted_preview,
                    event_ts=event_ts,
                    revision_id=None,
                    apply_status="rejected",
                    error_text=set_error,
                    meta_json=meta,
                )
                self._set_last_dynamic_event(event_ts, set_error)
                return self._result_from_event(
                    channel=channel,
                    input_key=input_key,
                    normalized_key=normalized_key,
                    event=event,
                )

        with self._session_factory() as db:
            detail = self._profile_service.save_profile_draft(
                db,
                profile_id=active_profile.id,
                payload_json=next_payload,
                source="dynamic_input",
            )
            revision_id = _read_current_draft_revision_id(detail)
            event = create_parameter_input_event(
                db,
                binding_id=binding.id,
                channel_id=channel.id,
                input_key=input_key,
                normalized_key=normalized_key,
                raw_payload=payload_text,
                parsed_value_text=converted_preview,
                event_ts=event_ts,
                revision_id=revision_id,
                apply_status="accepted",
                error_text=None,
                meta_json=meta,
            )

        self._ingest_param_signal(
            binding=binding,
            channel=channel,
            normalized_key=normalized_key,
            value=converted_value,
            event_ts=event_ts,
            source_ref_id=event.id,
        )
        self._set_last_dynamic_event(event_ts, None)
        self._queue_debounced_apply(profile_id=active_profile.id, event_id=event.id)
        return self._result_from_event(channel=channel, input_key=input_key, normalized_key=normalized_key, event=event)

    def get_status_snapshot(self, db_session) -> dict[str, object]:
        bindings_count = int(
            db_session.scalar(select(func.count(ParameterBinding.id))) or 0
        )
        with self._status_lock:
            return {
                "enabled": self._settings.param_dynamic_enabled,
                "bindings_count": bindings_count,
                "last_dynamic_event_ts": (
                    self._last_dynamic_event_ts.isoformat() if self._last_dynamic_event_ts else None
                ),
                "last_apply_ts": self._last_apply_ts.isoformat() if self._last_apply_ts else None,
                "last_error": self._last_error,
            }

    def _convert_value(
        self,
        *,
        parsed_value: str | None,
        binding: ParameterBinding,
        entry: DynamicParameterCatalogEntry,
    ) -> tuple[Any, str | None, str | None]:
        if entry.value_type in {"number"}:
            if parsed_value is None:
                return None, None, "Numeric value expected but payload resolved to null"
            try:
                numeric = float(parsed_value)
            except (TypeError, ValueError):
                return None, parsed_value, f"Numeric value expected for '{entry.parameter_key}'"

            if binding.incoming_unit and entry.expected_unit:
                if not _is_supported_unit_pair(entry.expected_unit, binding.incoming_unit):
                    return (
                        None,
                        parsed_value,
                        f"incoming_unit '{binding.incoming_unit}' is incompatible with expected unit '{entry.expected_unit}'",
                    )

            converted = numeric * binding.value_multiplier
            if entry.minimum is not None and converted < entry.minimum:
                return (
                    None,
                    str(converted),
                    f"value {converted} is below minimum {entry.minimum} for '{entry.parameter_key}'",
                )
            if entry.maximum is not None and converted > entry.maximum:
                return (
                    None,
                    str(converted),
                    f"value {converted} is above maximum {entry.maximum} for '{entry.parameter_key}'",
                )
            if math.isclose(converted, round(converted), rel_tol=0.0, abs_tol=1e-9):
                return int(round(converted)), str(converted), None
            return converted, format(converted, ".12g"), None

        if entry.value_type == "enum":
            value = (parsed_value or "").strip()
            if value == "":
                return None, None, "Non-empty value required"
            if entry.options and value not in entry.options:
                return None, value, f"value '{value}' is not allowed ({', '.join(entry.options)})"
            return value, value, None

        if entry.value_type == "string_list":
            parsed_list = _parse_string_list(parsed_value)
            if parsed_list is None or len(parsed_list) == 0:
                return None, parsed_value, "List of strings required"
            preview = ",".join(parsed_list)
            return parsed_list, preview, None

        value = parsed_value if parsed_value is not None else ""
        return value, value, None

    def _ingest_param_signal(
        self,
        *,
        binding: ParameterBinding,
        channel: InputChannel,
        normalized_key: str,
        value: Any,
        event_ts: datetime,
        source_ref_id: int,
    ) -> None:
        signal_key = _dynamic_signal_key(binding.parameter_key, binding.selector_value)
        with self._session_factory() as db:
            ingest_signal_measurement(
                db,
                signal_key=signal_key,
                label=signal_key,
                value_type=infer_value_type(value),
                canonical_unit=None,
                value=value,
                ts=event_ts,
                quality_status="ok",
                source_type="param_input",
                run_id=None,
                mapping_id=None,
                source_ref_id=source_ref_id,
                tags_json={
                    "parameter_key": binding.parameter_key,
                    "selector_value": binding.selector_value,
                    "channel_code": channel.code,
                    "input_key": normalized_key,
                },
                ingested_at=datetime.now(timezone.utc),
            )

    def _queue_debounced_apply(self, *, profile_id: int, event_id: int) -> None:
        with self._debounce_lock:
            self._pending_apply_events.setdefault(profile_id, set()).add(event_id)
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = Timer(
                max(0.1, float(self._settings.param_dynamic_apply_debounce_seconds)),
                self._flush_debounced_apply,
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _flush_debounced_apply(self) -> None:
        with self._debounce_lock:
            pending = {profile_id: set(event_ids) for profile_id, event_ids in self._pending_apply_events.items()}
            self._pending_apply_events = {}
            self._debounce_timer = None

        for profile_id, event_ids in pending.items():
            event_ids_list = sorted(event_ids)
            apply_status = "applied"
            error_text: str | None = None

            try:
                with self._session_factory() as db:
                    outcome = self._profile_service.apply_profile(
                        db,
                        profile_id=profile_id,
                        set_active_profile=True,
                    )
                if not outcome.valid:
                    apply_status = "apply_failed"
                    error_text = "; ".join(outcome.errors) or "Validation failed during auto-apply"
            except Exception as exc:
                apply_status = "apply_failed"
                error_text = str(exc)
                self._logger.exception("dynamic parameter auto-apply failed profile_id=%s", profile_id)

            with self._session_factory() as db:
                bulk_update_parameter_input_event_status(
                    db,
                    event_ids=event_ids_list,
                    apply_status=apply_status,
                    error_text=error_text,
                )

            self._set_last_apply(error_text)

    def _set_last_dynamic_event(self, ts: datetime, error_text: str | None) -> None:
        with self._status_lock:
            self._last_dynamic_event_ts = ts
            if error_text:
                self._last_error = error_text

    def _set_last_apply(self, error_text: str | None) -> None:
        with self._status_lock:
            self._last_apply_ts = datetime.now(timezone.utc)
            self._last_error = error_text

    def _result_from_event(
        self,
        *,
        channel: InputChannel,
        input_key: str,
        normalized_key: str,
        event,
    ) -> ParameterDynamicIngestResult:
        return ParameterDynamicIngestResult(
            accepted=True,
            channel_id=channel.id,
            channel_code=channel.code,
            channel_type=channel.channel_type,
            input_key=input_key,
            normalized_key=normalized_key,
            binding_matched=event.binding_id is not None,
            binding_id=event.binding_id,
            event_id=event.id,
            event_ts=_to_utc(event.event_ts),
            apply_status=event.apply_status,
            error_text=event.error_text,
        )


def _read_current_draft_revision_id(detail: dict[str, Any]) -> int | None:
    draft = detail.get("current_draft")
    if not isinstance(draft, dict):
        return None
    revision_id = draft.get("id")
    if isinstance(revision_id, int):
        return revision_id
    return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _set_parameter_value(
    *,
    payload: dict[str, Any],
    parameter_key: str,
    selector_value: str | None,
    value: Any,
    requires_selector: bool,
) -> str | None:
    if "[]." not in parameter_key:
        _set_path_value(payload, parameter_key, value)
        return None

    list_path, field_name = parameter_key.split("[].", 1)
    list_value = _read_path(payload, list_path)
    if not isinstance(list_value, list):
        return f"Target list '{list_path}' is missing in active draft"

    selector = (selector_value or "").strip()
    if requires_selector and selector == "":
        return f"selector_value required for '{parameter_key}'"

    for item in list_value:
        if not isinstance(item, dict):
            continue
        device_id = item.get("device_id")
        if isinstance(device_id, str) and device_id.strip() == selector:
            item[field_name] = value
            return None
    return f"No entry with device_id '{selector}' found for '{list_path}'"


def _set_path_value(payload: dict[str, Any], dot_path: str, value: Any) -> None:
    tokens = [token for token in dot_path.split(".") if token != ""]
    current: dict[str, Any] = payload
    for token in tokens[:-1]:
        child = current.get(token)
        if not isinstance(child, dict):
            child = {}
            current[token] = child
        current = child
    if tokens:
        current[tokens[-1]] = value


def _read_path(payload: dict[str, Any], dot_path: str) -> Any:
    tokens = [token for token in dot_path.split(".") if token != ""]
    current: Any = payload
    for token in tokens:
        if not isinstance(current, dict):
            return None
        current = current.get(token)
    return current


def _is_supported_unit_pair(expected_unit: str, incoming_unit: str) -> bool:
    expected = _normalize_unit(expected_unit)
    incoming = _normalize_unit(incoming_unit)
    if expected == incoming:
        return True
    allowed_pairs = {
        ("w", "kw"),
        ("kw", "w"),
        ("wh", "kwh"),
        ("kwh", "wh"),
        ("eur/wh", "eur/kwh"),
        ("eur/kwh", "eur/wh"),
    }
    return (expected, incoming) in allowed_pairs


def _normalize_unit(value: str) -> str:
    return value.strip().lower().replace(" ", "")


def _parse_string_list(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    text = raw.strip()
    if text == "":
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            values = [str(item).strip() for item in parsed if str(item).strip() != ""]
            return values
    return [part.strip() for part in text.split(",") if part.strip() != ""]


def _dynamic_signal_key(parameter_key: str, selector_value: str | None) -> str:
    key = parameter_key.replace("[].", ".")
    if selector_value and selector_value.strip() != "":
        return f"param/{selector_value.strip()}/{key}"
    return f"param/{key}"
