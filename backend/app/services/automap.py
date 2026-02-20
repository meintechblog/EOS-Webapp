from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import InputMapping
from app.repositories.mappings import list_mappings
from app.repositories.parameter_bindings import list_parameter_bindings
from app.repositories.topic_observations import (
    InputObservationSnapshot,
    infer_namespace_from_normalized_key,
    list_input_observations,
    normalize_input_key,
)
from app.schemas.discovery import (
    AutomapAppliedItem,
    AutomapResult,
    AutomapSkippedItem,
    DiscoveredInputItem,
    DiscoveredTopicItem,
    TopicNormalization,
)
from app.schemas.mappings import SignConvention
from app.services.eos_catalog import EosFieldCatalogService


@dataclass(frozen=True)
class FieldMetadata:
    eos_field: str
    suggested_unit: str | None
    info_notes: list[str]


@dataclass(frozen=True)
class MatchDecision:
    input_key: str
    normalized_topic: str
    suggested_eos_field: str | None
    suggested_multiplier: float | None
    confidence: float | None
    notes: list[str]
    reason: str | None


@dataclass(frozen=True)
class Candidate:
    observation: InputObservationSnapshot
    decision: MatchDecision


class AutomapService:
    _DISCOVERY_INPUT_PREFIX = "eos/input/"
    _SYNONYMS: dict[str, str] = {
        "house_load": "house_load_w",
        "pv_power": "pv_power_w",
        "grid_power": "grid_power_w",
        "grid_power_consumption": "grid_power_w",
        "grid_power_import": "grid_power_w",
        "battery_soc": "battery_soc_pct",
        "battery_soc_percent": "battery_soc_pct",
        "battery_soc_percentage": "battery_soc_pct",
        "battery_power": "battery_power_w",
        "battery_power_charge": "battery_power_w",
        "battery_power_charging": "battery_power_w",
        "battery_power_discharge": "battery_power_w",
        "battery_power_discharging": "battery_power_w",
        "ev_charging_power": "ev_charging_power_w",
        "temperature": "temperature_c",
    }

    def __init__(self, *, eos_catalog_service: EosFieldCatalogService, settings: Settings):
        self._eos_catalog_service = eos_catalog_service
        self._settings = settings
        self._logger = logging.getLogger("app.automap")

    def list_discovered_inputs(
        self,
        db: Session,
        *,
        limit: int = 500,
        namespace: str = "all",
        channel_type: str | None = None,
        channel_id: int | None = None,
        active_only: bool = True,
        active_seconds: int | None = None,
    ) -> list[DiscoveredInputItem]:
        seen_after = None
        if active_only:
            seconds = active_seconds or self._settings.mqtt_discovery_active_seconds
            seen_after = datetime.now(timezone.utc) - timedelta(seconds=max(1, seconds))

        required_prefix = None
        if namespace == "input":
            required_prefix = self._DISCOVERY_INPUT_PREFIX
        elif namespace == "param":
            required_prefix = "eos/param/"

        observations = list_input_observations(
            db,
            limit=limit,
            channel_type=channel_type,
            channel_id=channel_id,
            required_prefix=required_prefix,
            seen_after=seen_after,
        )

        field_metadata = self._build_field_metadata()
        mappings = list_mappings(db)
        by_channel_key = {
            (mapping.channel_id, mapping.mqtt_topic): mapping
            for mapping in mappings
            if mapping.channel_id is not None and mapping.mqtt_topic is not None
        }
        parameter_bindings = list_parameter_bindings(db)
        parameter_binding_by_channel_key = {
            (snapshot.binding.channel_id, snapshot.binding.input_key): snapshot.binding
            for snapshot in parameter_bindings
        }

        items: list[DiscoveredInputItem] = []
        for observation in observations:
            observed_namespace = infer_namespace_from_normalized_key(observation.normalized_key)
            if observed_namespace == "param":
                binding = parameter_binding_by_channel_key.get(
                    (observation.channel_id, observation.normalized_key)
                )
                mapped_status = "mapped_correct" if binding is not None else "unmapped"
                mapped_kind = "parameter_binding" if binding is not None else "unmapped"
                decision = MatchDecision(
                    input_key=observation.input_key,
                    normalized_topic=observation.normalized_key,
                    suggested_eos_field=None,
                    suggested_multiplier=None,
                    confidence=None,
                    notes=["Dynamic parameter namespace."],
                    reason=None,
                )
            else:
                decision = self._build_match_decision(observation.input_key, field_metadata)
                mapped_status = self._mapped_status(
                    by_channel_key=by_channel_key,
                    channel_id=observation.channel_id,
                    normalized_topic=decision.normalized_topic,
                    suggested_eos_field=decision.suggested_eos_field,
                )
                mapping = by_channel_key.get((observation.channel_id, decision.normalized_topic))
                mapped_kind = "signal_mapping" if mapping is not None else "unmapped"

            items.append(
                DiscoveredInputItem(
                    namespace=observed_namespace,  # type: ignore[arg-type]
                    channel_id=observation.channel_id,
                    channel_code=observation.channel_code,
                    channel_type=observation.channel_type,
                    input_key=observation.input_key,
                    normalized_key=decision.normalized_topic,
                    last_seen=observation.last_seen,
                    last_payload=observation.last_payload,
                    message_count=observation.message_count,
                    last_meta_json=observation.last_meta_json,
                    suggested_eos_field=decision.suggested_eos_field,
                    suggested_multiplier=decision.suggested_multiplier,
                    confidence=decision.confidence,
                    notes=decision.notes,
                    mapped_status=mapped_status,
                    mapped_kind=mapped_kind,  # type: ignore[arg-type]
                )
            )
        return items

    def list_discovered_topics(self, db: Session, limit: int = 500) -> list[DiscoveredTopicItem]:
        discovered_inputs = self.list_discovered_inputs(
            db,
            limit=limit,
            namespace="input",
            channel_type="mqtt",
            active_only=True,
        )
        items: list[DiscoveredTopicItem] = []
        for item in discovered_inputs:
            retain = bool(item.last_meta_json.get("retain", False))
            qos = int(item.last_meta_json.get("qos", 0))
            items.append(
                DiscoveredTopicItem(
                    mqtt_topic=item.normalized_key,
                    last_seen=item.last_seen,
                    last_payload=item.last_payload,
                    message_count=item.message_count,
                    last_retain=retain,
                    last_qos=qos,
                    normalized_topic=item.normalized_key,
                    suggested_eos_field=item.suggested_eos_field,
                    suggested_multiplier=item.suggested_multiplier,
                    confidence=item.confidence,
                    notes=item.notes,
                )
            )
        return items

    def apply_automap(
        self,
        db: Session,
        *,
        channel_ids: list[int] | None = None,
        channel_type: str | None = None,
    ) -> AutomapResult:
        result = AutomapResult()

        observations = list_input_observations(
            db,
            channel_type=channel_type,
            required_prefix=self._DISCOVERY_INPUT_PREFIX,
            seen_after=self._discovery_seen_after(),
        )
        if channel_ids:
            allowed = set(channel_ids)
            observations = [obs for obs in observations if obs.channel_id in allowed]

        field_metadata = self._build_field_metadata()
        observed_normalized = {(obs.channel_id, normalize_input_key(obs.input_key)) for obs in observations}

        mappings = list_mappings(db)
        mapping_by_field = {mapping.eos_field: mapping for mapping in mappings}
        mapping_by_channel_topic = {
            (mapping.channel_id, mapping.mqtt_topic): mapping
            for mapping in mappings
            if mapping.channel_id is not None and mapping.mqtt_topic is not None
        }

        candidates: list[Candidate] = []
        no_match_candidates: list[Candidate] = []
        for observation in observations:
            decision = self._build_match_decision(observation.input_key, field_metadata)
            if decision.input_key != decision.normalized_topic:
                result.normalizations.append(
                    TopicNormalization(
                        from_topic=decision.input_key,
                        to_topic=decision.normalized_topic,
                        channel_id=observation.channel_id,
                        channel_code=observation.channel_code,
                        channel_type=observation.channel_type,
                    )
                )
                if (observation.channel_id, decision.normalized_topic) not in observed_normalized:
                    result.warnings.append(
                        f"Input key '{decision.input_key}' was normalized to '{decision.normalized_topic}' "
                        f"for channel '{observation.channel_code}'. Source should switch to normalized key."
                    )

            candidate = Candidate(observation=observation, decision=decision)
            if decision.suggested_eos_field is None or decision.suggested_multiplier is None:
                no_match_candidates.append(candidate)
                continue
            candidates.append(candidate)

        for candidate in no_match_candidates:
            result.skipped.append(
                AutomapSkippedItem(
                    mqtt_topic=candidate.decision.input_key,
                    normalized_topic=candidate.decision.normalized_topic,
                    channel_id=candidate.observation.channel_id,
                    channel_code=candidate.observation.channel_code,
                    channel_type=candidate.observation.channel_type,
                    reason=candidate.decision.reason or "no_match",
                    notes=candidate.decision.notes,
                )
            )

        selected: list[Candidate] = []
        grouped: dict[str, list[Candidate]] = {}
        for candidate in candidates:
            eos_field = candidate.decision.suggested_eos_field
            if eos_field is None:
                continue
            grouped.setdefault(eos_field, []).append(candidate)

        for eos_field, group in grouped.items():
            group_sorted = sorted(
                group,
                key=lambda item: (
                    -(item.decision.confidence or 0.0),
                    -item.observation.last_seen.timestamp(),
                    -item.observation.message_count,
                    0 if item.observation.channel_type == "mqtt" else 1,
                ),
            )

            if len(group_sorted) >= 2:
                first = group_sorted[0]
                second = group_sorted[1]
                first_conf = first.decision.confidence or 0.0
                second_conf = second.decision.confidence or 0.0
                same_confidence = abs(first_conf - second_conf) < 1e-9
                if same_confidence and (
                    first.observation.channel_id != second.observation.channel_id
                    or first.decision.normalized_topic != second.decision.normalized_topic
                ):
                    for ambiguous in group_sorted:
                        result.skipped.append(
                            AutomapSkippedItem(
                                mqtt_topic=ambiguous.decision.input_key,
                                normalized_topic=ambiguous.decision.normalized_topic,
                                channel_id=ambiguous.observation.channel_id,
                                channel_code=ambiguous.observation.channel_code,
                                channel_type=ambiguous.observation.channel_type,
                                reason="ambiguous_channel_candidate",
                                notes=ambiguous.decision.notes,
                            )
                        )
                    continue

            selected.append(group_sorted[0])
            for ignored in group_sorted[1:]:
                result.skipped.append(
                    AutomapSkippedItem(
                        mqtt_topic=ignored.decision.input_key,
                        normalized_topic=ignored.decision.normalized_topic,
                        channel_id=ignored.observation.channel_id,
                        channel_code=ignored.observation.channel_code,
                        channel_type=ignored.observation.channel_type,
                        reason="lower_priority_candidate",
                        notes=ignored.decision.notes,
                    )
                )

        for candidate in selected:
            observation = candidate.observation
            decision = candidate.decision
            eos_field = decision.suggested_eos_field
            desired_multiplier = decision.suggested_multiplier
            if eos_field is None or desired_multiplier is None:
                continue

            desired_topic = decision.normalized_topic
            desired_channel_id = observation.channel_id
            topic_key = desired_topic.split("/")[-1]
            desired_sign = self._default_sign_for_field(eos_field, topic_key=topic_key)
            desired_unit = field_metadata.get(eos_field).suggested_unit if eos_field in field_metadata else None

            existing_by_field = mapping_by_field.get(eos_field)
            existing_by_channel_topic = mapping_by_channel_topic.get((desired_channel_id, desired_topic))
            if existing_by_field is not None:
                if existing_by_field.fixed_value is not None:
                    result.skipped.append(
                        AutomapSkippedItem(
                            mqtt_topic=decision.input_key,
                            normalized_topic=desired_topic,
                            channel_id=observation.channel_id,
                            channel_code=observation.channel_code,
                            channel_type=observation.channel_type,
                            reason="field_has_fixed_value_manual_review",
                            notes=decision.notes
                            + [
                                "Existing mapping for this eos_field uses fixed_value. "
                                "Update manually if you want channel-based ingest."
                            ],
                        )
                    )
                    continue

                desired_sign = self._resolve_existing_sign(
                    existing_by_field.sign_convention,
                    eos_field,
                    topic_key=topic_key,
                )

                if (
                    existing_by_channel_topic is not None
                    and existing_by_channel_topic.id != existing_by_field.id
                ):
                    result.skipped.append(
                        AutomapSkippedItem(
                            mqtt_topic=decision.input_key,
                            normalized_topic=desired_topic,
                            channel_id=observation.channel_id,
                            channel_code=observation.channel_code,
                            channel_type=observation.channel_type,
                            reason="topic_conflict_manual_review",
                            notes=decision.notes
                            + [
                                "Input key already mapped by eos_field "
                                f"'{existing_by_channel_topic.eos_field}' in this channel."
                            ],
                        )
                    )
                    continue

                previous_channel_topic = (
                    existing_by_field.channel_id,
                    existing_by_field.mqtt_topic,
                )
                try:
                    updated = self._apply_update_to_existing(
                        db=db,
                        mapping=existing_by_field,
                        channel_id=desired_channel_id,
                        mqtt_topic=desired_topic,
                        unit=desired_unit,
                        value_multiplier=desired_multiplier,
                        sign_convention=desired_sign,
                    )
                except IntegrityError:
                    db.rollback()
                    self._logger.exception(
                        "automap update failed channel_id=%s key=%s field=%s",
                        desired_channel_id,
                        desired_topic,
                        existing_by_field.eos_field,
                    )
                    result.skipped.append(
                        AutomapSkippedItem(
                            mqtt_topic=decision.input_key,
                            normalized_topic=desired_topic,
                            channel_id=observation.channel_id,
                            channel_code=observation.channel_code,
                            channel_type=observation.channel_type,
                            reason="topic_conflict_manual_review",
                            notes=decision.notes + ["Constraint conflict while updating mapping."],
                        )
                    )
                    continue

                item = AutomapAppliedItem(
                    mapping_id=existing_by_field.id,
                    eos_field=existing_by_field.eos_field,
                    mqtt_topic=existing_by_field.mqtt_topic or desired_topic,
                    channel_id=existing_by_field.channel_id,
                    channel_code=observation.channel_code,
                    channel_type=observation.channel_type,
                    value_multiplier=existing_by_field.value_multiplier,
                    sign_convention=existing_by_field.sign_convention,  # type: ignore[arg-type]
                    notes=decision.notes,
                )
                if updated:
                    result.updated.append(item)
                else:
                    result.unchanged.append(item)

                mapping_by_field[existing_by_field.eos_field] = existing_by_field
                mapping_by_channel_topic.pop(previous_channel_topic, None)
                if existing_by_field.channel_id is not None and existing_by_field.mqtt_topic is not None:
                    mapping_by_channel_topic[(existing_by_field.channel_id, existing_by_field.mqtt_topic)] = (
                        existing_by_field
                    )
                continue

            if existing_by_channel_topic is not None:
                result.skipped.append(
                    AutomapSkippedItem(
                        mqtt_topic=decision.input_key,
                        normalized_topic=desired_topic,
                        channel_id=observation.channel_id,
                        channel_code=observation.channel_code,
                        channel_type=observation.channel_type,
                        reason="topic_conflict_manual_review",
                        notes=decision.notes
                        + [
                            "Input key already mapped by eos_field "
                            f"'{existing_by_channel_topic.eos_field}' in this channel."
                        ],
                    )
                )
                continue

            mapping = InputMapping(
                eos_field=eos_field,
                channel_id=desired_channel_id,
                mqtt_topic=desired_topic,
                fixed_value=None,
                payload_path=None,
                unit=desired_unit,
                value_multiplier=desired_multiplier,
                sign_convention=desired_sign,
                enabled=True,
            )
            db.add(mapping)
            try:
                db.commit()
                db.refresh(mapping)
            except IntegrityError:
                db.rollback()
                self._logger.exception(
                    "automap create failed channel_id=%s key=%s field=%s",
                    desired_channel_id,
                    desired_topic,
                    eos_field,
                )
                result.skipped.append(
                    AutomapSkippedItem(
                        mqtt_topic=decision.input_key,
                        normalized_topic=desired_topic,
                        channel_id=observation.channel_id,
                        channel_code=observation.channel_code,
                        channel_type=observation.channel_type,
                        reason="topic_conflict_manual_review",
                        notes=decision.notes + ["Constraint conflict while creating mapping."],
                    )
                )
                continue

            mapping_by_field[mapping.eos_field] = mapping
            mapping_by_channel_topic[(mapping.channel_id, mapping.mqtt_topic)] = mapping  # type: ignore[arg-type]
            result.created.append(
                AutomapAppliedItem(
                    mapping_id=mapping.id,
                    eos_field=mapping.eos_field,
                    mqtt_topic=mapping.mqtt_topic or desired_topic,
                    channel_id=mapping.channel_id,
                    channel_code=observation.channel_code,
                    channel_type=observation.channel_type,
                    value_multiplier=mapping.value_multiplier,
                    sign_convention=mapping.sign_convention,  # type: ignore[arg-type]
                    notes=decision.notes,
                )
            )

        result.warnings = sorted(set(result.warnings))
        return result

    def _apply_update_to_existing(
        self,
        *,
        db: Session,
        mapping: InputMapping,
        channel_id: int,
        mqtt_topic: str,
        unit: str | None,
        value_multiplier: float,
        sign_convention: SignConvention,
    ) -> bool:
        updated = False
        if mapping.channel_id != channel_id:
            mapping.channel_id = channel_id
            updated = True
        if mapping.mqtt_topic != mqtt_topic:
            mapping.mqtt_topic = mqtt_topic
            updated = True
        if mapping.fixed_value is not None:
            mapping.fixed_value = None
            updated = True
        if unit is not None and mapping.unit != unit:
            mapping.unit = unit
            updated = True
        if not math.isclose(mapping.value_multiplier, value_multiplier, rel_tol=0.0, abs_tol=1e-9):
            mapping.value_multiplier = value_multiplier
            updated = True
        if mapping.sign_convention != sign_convention:
            mapping.sign_convention = sign_convention
            updated = True
        if not mapping.enabled:
            mapping.enabled = True
            updated = True

        if updated:
            db.add(mapping)
            db.commit()
            db.refresh(mapping)
        return updated

    def _build_field_metadata(self) -> dict[str, FieldMetadata]:
        metadata: dict[str, FieldMetadata] = {}
        for field in self._eos_catalog_service.list_fields():
            metadata[field.eos_field] = FieldMetadata(
                eos_field=field.eos_field,
                suggested_unit=field.suggested_units[0] if field.suggested_units else None,
                info_notes=field.info_notes,
            )
        return metadata

    def _build_match_decision(
        self,
        input_key: str,
        field_metadata: dict[str, FieldMetadata],
    ) -> MatchDecision:
        normalized_topic, normalization_notes = self._normalize_topic(input_key)
        topic_key = normalized_topic.split("/")[-1]
        known_fields = set(field_metadata)
        candidates: list[tuple[str, float, float, list[str]]] = []

        if topic_key in known_fields:
            candidates.append((topic_key, 1.0, 1.0, ["Exact eos_field match from input key."]))

        if topic_key.endswith("_kw"):
            converted = topic_key[: -len("_kw")] + "_w"
            if converted in known_fields:
                candidates.append(
                    (
                        converted,
                        0.95,
                        1000.0,
                        ["Converted suffix from _kw to _w using multiplier 1000."],
                    )
                )
        if topic_key.endswith("_kwh"):
            converted = topic_key[: -len("_kwh")] + "_wh"
            if converted in known_fields:
                candidates.append(
                    (
                        converted,
                        0.95,
                        1000.0,
                        ["Converted suffix from _kwh to _wh using multiplier 1000."],
                    )
                )

        synonym_key = topic_key
        for suffix in ("_kw", "_kwh", "_w", "_wh"):
            if synonym_key.endswith(suffix):
                synonym_key = synonym_key[: -len(suffix)]
                break

        synonym_target = self._SYNONYMS.get(synonym_key)
        if synonym_target and synonym_target in known_fields:
            synonym_multiplier = 1.0
            synonym_notes: list[str] = [
                f"Matched synonym '{synonym_key}' to eos_field '{synonym_target}'."
            ]
            if topic_key.endswith("_kw") and synonym_target.endswith("_w"):
                synonym_multiplier = 1000.0
                synonym_notes.append("Applied multiplier 1000 for _kw to _w conversion.")
            elif topic_key.endswith("_kwh") and synonym_target.endswith("_wh"):
                synonym_multiplier = 1000.0
                synonym_notes.append("Applied multiplier 1000 for _kwh to _wh conversion.")
            candidates.append((synonym_target, 0.90, synonym_multiplier, synonym_notes))

        if not candidates:
            notes = list(normalization_notes)
            notes.append("No eos_field suggestion found for this input key.")
            return MatchDecision(
                input_key=input_key,
                normalized_topic=normalized_topic,
                suggested_eos_field=None,
                suggested_multiplier=None,
                confidence=None,
                notes=notes,
                reason="no_match",
            )

        candidates.sort(key=lambda item: (-item[1], item[0]))
        best_field, best_score, best_multiplier, best_notes = candidates[0]

        if len(candidates) > 1:
            second_field, second_score, _, _ = candidates[1]
            if abs(best_score - second_score) < 1e-9 and second_field != best_field:
                notes = list(normalization_notes)
                notes.append(
                    f"Ambiguous match between '{best_field}' and '{second_field}'."
                )
                return MatchDecision(
                    input_key=input_key,
                    normalized_topic=normalized_topic,
                    suggested_eos_field=None,
                    suggested_multiplier=None,
                    confidence=None,
                    notes=notes,
                    reason="ambiguous_match",
                )

        notes = list(normalization_notes) + best_notes
        return MatchDecision(
            input_key=input_key,
            normalized_topic=normalized_topic,
            suggested_eos_field=best_field,
            suggested_multiplier=best_multiplier,
            confidence=best_score,
            notes=notes,
            reason=None,
        )

    def _normalize_topic(self, input_key: str) -> tuple[str, list[str]]:
        normalized = normalize_input_key(input_key)
        notes: list[str] = []
        if normalized != input_key:
            notes.append(f"Normalized input key '{input_key}' to '{normalized}'.")
        return normalized, notes

    def _default_sign_for_field(self, eos_field: str, *, topic_key: str) -> SignConvention:
        if eos_field != "grid_power_w":
            return "canonical"

        lower_topic_key = topic_key.lower()
        if "consumption" in lower_topic_key or "import" in lower_topic_key:
            return "positive_is_import"
        if "export" in lower_topic_key or "feed" in lower_topic_key:
            return "positive_is_export"
        return "unknown"

    def _resolve_existing_sign(
        self,
        current: str,
        eos_field: str,
        *,
        topic_key: str,
    ) -> SignConvention:
        if eos_field != "grid_power_w":
            return "canonical"

        if current in {"positive_is_import", "positive_is_export"}:
            return current  # type: ignore[return-value]

        inferred = self._default_sign_for_field(eos_field, topic_key=topic_key)
        if inferred in {"positive_is_import", "positive_is_export"}:
            return inferred
        return "unknown"

    def _mapped_status(
        self,
        *,
        by_channel_key: dict[tuple[int | None, str | None], InputMapping],
        channel_id: int,
        normalized_topic: str,
        suggested_eos_field: str | None,
    ) -> str:
        mapping = by_channel_key.get((channel_id, normalized_topic))
        if mapping is None:
            return "unmapped"
        if suggested_eos_field is None:
            return "mapped_other"
        if mapping.eos_field == suggested_eos_field:
            return "mapped_correct"
        return "mapped_other"

    def _discovery_seen_after(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(
            seconds=max(1, self._settings.mqtt_discovery_active_seconds)
        )
