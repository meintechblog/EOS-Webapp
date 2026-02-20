from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import sessionmaker

from app.db.models import InputChannel
from app.repositories.mappings import EnabledMappingSnapshot, get_mapping_by_channel_input_key
from app.repositories.signal_backbone import infer_value_type, ingest_signal_measurement
from app.repositories.telemetry import create_telemetry_event
from app.repositories.topic_observations import normalize_input_key, upsert_input_observation
from app.services.payload_parser import parse_event_timestamp, parse_payload

if TYPE_CHECKING:
    from app.services.emr_pipeline import EmrPipelineService


@dataclass(frozen=True)
class InputIngestResult:
    accepted: bool
    channel_id: int
    channel_code: str
    channel_type: str
    input_key: str
    normalized_key: str
    mapping_matched: bool
    mapping_id: int | None
    event_ts: datetime


class InputIngestPipelineService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker,
        emr_pipeline_service: "EmrPipelineService | None" = None,
    ):
        self._session_factory = session_factory
        self._emr_pipeline_service = emr_pipeline_service
        self._logger = logging.getLogger("app.input_ingest")

    def ingest(
        self,
        *,
        channel: InputChannel,
        input_key: str,
        payload_text: str,
        event_received_ts: datetime,
        metadata: dict[str, Any] | None = None,
        explicit_timestamp: datetime | None = None,
    ) -> InputIngestResult:
        event_received_utc = _to_utc(event_received_ts)
        if input_key.strip().startswith("eos/param/"):
            return InputIngestResult(
                accepted=True,
                channel_id=channel.id,
                channel_code=channel.code,
                channel_type=channel.channel_type,
                input_key=input_key,
                normalized_key=input_key.strip(),
                mapping_matched=False,
                mapping_id=None,
                event_ts=explicit_timestamp or event_received_utc,
            )
        normalized_key = normalize_input_key(input_key)
        meta = metadata if isinstance(metadata, dict) else {}

        with self._session_factory() as db:
            upsert_input_observation(
                db,
                channel_id=channel.id,
                input_key=input_key,
                normalized_key=normalized_key,
                payload=payload_text,
                last_meta_json=meta,
                event_ts=event_received_utc,
            )

            mapping = get_mapping_by_channel_input_key(
                db,
                channel_id=channel.id,
                input_key=normalized_key,
            )

        if mapping is None:
            return InputIngestResult(
                accepted=True,
                channel_id=channel.id,
                channel_code=channel.code,
                channel_type=channel.channel_type,
                input_key=input_key,
                normalized_key=normalized_key,
                mapping_matched=False,
                mapping_id=None,
                event_ts=explicit_timestamp or event_received_utc,
            )

        parsed_value = parse_payload(payload_text, mapping.payload_path, logger=self._logger)
        timestamp_fallback = explicit_timestamp or event_received_utc
        source_ts = parse_event_timestamp(
            payload_text,
            mapping.timestamp_path,
            fallback_ts=timestamp_fallback,
            logger=self._logger,
        )
        transformed_value = self._apply_value_transform(parsed_value, mapping)

        with self._session_factory() as db:
            telemetry_event = create_telemetry_event(
                db,
                mapping_id=mapping.id,
                eos_field=mapping.eos_field,
                raw_payload=payload_text,
                parsed_value=transformed_value,
                event_ts=source_ts,
            )

            ingest_signal_measurement(
                db,
                signal_key=mapping.eos_field,
                label=mapping.eos_field,
                value_type=infer_value_type(transformed_value),
                canonical_unit=_canonical_unit_for_field(mapping.eos_field, mapping.unit),
                value=transformed_value,
                ts=source_ts,
                quality_status="ok",
                source_type="mqtt_input" if channel.channel_type == "mqtt" else "http_input",
                run_id=None,
                mapping_id=mapping.id,
                source_ref_id=telemetry_event.id,
                tags_json={
                    "eos_field": mapping.eos_field,
                    "source": channel.channel_type,
                    "channel_code": channel.code,
                    "input_key": normalized_key,
                },
                ingested_at=datetime.now(timezone.utc),
            )

        if self._emr_pipeline_service is not None:
            self._emr_pipeline_service.process_mapped_value(
                mapping=EnabledMappingSnapshot(
                    id=mapping.id,
                    eos_field=mapping.eos_field,
                    channel_id=channel.id,
                    channel_code=channel.code,
                    channel_type=channel.channel_type,
                    mqtt_topic=normalized_key,
                    payload_path=mapping.payload_path,
                    timestamp_path=mapping.timestamp_path,
                    unit=mapping.unit,
                    value_multiplier=mapping.value_multiplier,
                    sign_convention=mapping.sign_convention,
                ),
                transformed_value=transformed_value,
                source_ts=source_ts,
                raw_payload=payload_text,
            )

        return InputIngestResult(
            accepted=True,
            channel_id=channel.id,
            channel_code=channel.code,
            channel_type=channel.channel_type,
            input_key=input_key,
            normalized_key=normalized_key,
            mapping_matched=True,
            mapping_id=mapping.id,
            event_ts=source_ts,
        )

    def _apply_value_transform(self, parsed_value: str | None, mapping) -> str | None:
        if parsed_value is None:
            return None

        try:
            numeric_value = float(parsed_value)
        except (TypeError, ValueError):
            if not math.isclose(mapping.value_multiplier, 1.0, rel_tol=0.0, abs_tol=1e-9) or (
                mapping.sign_convention == "positive_is_export"
            ):
                self._logger.warning(
                    "value transform skipped for non-numeric payload field=%s value=%s",
                    mapping.eos_field,
                    parsed_value,
                )
            return parsed_value

        transformed = numeric_value * mapping.value_multiplier
        if mapping.sign_convention == "positive_is_export":
            transformed = transformed * -1.0

        if math.isclose(transformed, round(transformed), rel_tol=0.0, abs_tol=1e-9):
            return str(int(round(transformed)))
        return format(transformed, ".12g")


def _canonical_unit_for_field(eos_field: str, unit: str | None) -> str | None:
    field = eos_field.strip().lower()
    if field.endswith("_w"):
        return "W"
    if field.endswith("_wh"):
        return "Wh"
    if field.endswith("_pct") or field.endswith("_percentage"):
        return "%"
    if "euro_pro_wh" in field:
        return "EUR/Wh"
    return unit


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
