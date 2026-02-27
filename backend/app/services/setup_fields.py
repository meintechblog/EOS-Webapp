from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.repositories.parameter_profiles import (
    get_active_parameter_profile,
    get_current_draft_revision,
    list_parameter_profiles,
    set_active_parameter_profile,
)
from app.repositories.setup_field_events import (
    create_setup_field_event,
    list_latest_setup_field_events,
    update_setup_field_events_status,
)
from app.repositories.signal_backbone import (
    infer_value_type,
    ingest_signal_measurement,
    list_latest_by_signal_keys,
)
from app.schemas.setup_fields import (
    SetupCategoryItemResponse,
    SetupCategoryResponse,
    SetupEntityMutateRequest,
    SetupEntityMutateResponse,
    SetupExportResponse,
    SetupLayoutResponse,
    SetupFieldResponse,
    SetupFieldUpdate,
    SetupFieldUpdateResult,
    SetupImportResponse,
    SetupReadinessItem,
    SetupReadinessResponse,
)
from app.services.parameter_profiles import ParameterProfileService
from app.services.parameters_catalog import ParameterCatalogService

if TYPE_CHECKING:
    from app.services.emr_pipeline import EmrPipelineService


@dataclass(frozen=True)
class SetupFieldDef:
    field_id: str
    group: str
    label: str
    required: bool
    value_type: str
    unit: str | None
    options_key: str | None
    minimum: float | None
    maximum: float | None
    param_path: tuple[str | int, ...] | None
    signal_key: str | None
    http_path_template: str
    advanced: bool = False
    item_key: str | None = None
    category_id: str | None = None


class SetupFieldService:
    _REQUIRED_SIGNAL_KEYS = ("house_load_w", "pv_power_w", "grid_import_w", "grid_export_w")
    _FIXED_IMPORT_PRICE_FIELD_ID = "param.elecprice.elecpriceimport.import_json.value"
    _CATEGORY_ORDER: tuple[str, ...] = (
        "location_base",
        "pv_forecast",
        "tariffs_load",
        "storage_inverter",
        "electric_vehicles",
        "home_appliances",
        "measurement_emr",
        "live_signals",
    )
    _CATEGORY_META: dict[str, dict[str, Any]] = {
        "location_base": {
            "title": "Standort & Basis",
            "description": "Stammdaten fur den Anlagenstandort.",
            "requirement_label": "MUSS",
            "repeatable": False,
            "add_entity_type": None,
        },
        "pv_forecast": {
            "title": "PV & Forecast",
            "description": "PV-Forecast-Basis plus optionale Zusatz-Planes.",
            "requirement_label": "MUSS",
            "repeatable": True,
            "add_entity_type": "pv_plane",
        },
        "tariffs_load": {
            "title": "Tarife & Last",
            "description": "Preisquellen, Lastmodell und Prognose-Settings.",
            "requirement_label": "MUSS/KANN",
            "repeatable": False,
            "add_entity_type": None,
        },
        "storage_inverter": {
            "title": "Speicher & Inverter",
            "description": "Basiskonfiguration fur Batterie und Inverter.",
            "requirement_label": "MUSS",
            "repeatable": False,
            "add_entity_type": None,
        },
        "electric_vehicles": {
            "title": "E-Autos",
            "description": "Optionale EV-Objekte, klonbar und loschbar.",
            "requirement_label": "KANN",
            "repeatable": True,
            "add_entity_type": "electric_vehicle",
        },
        "home_appliances": {
            "title": "Home-Appliances",
            "description": "Optionale Appliances inklusive editierbarer Zeitfenster.",
            "requirement_label": "KANN",
            "repeatable": True,
            "add_entity_type": "home_appliance",
        },
        "measurement_emr": {
            "title": "Messwerte & EMR",
            "description": "Measurement- und EMR-Key-Konfiguration.",
            "requirement_label": "MUSS",
            "repeatable": False,
            "add_entity_type": None,
        },
        "live_signals": {
            "title": "Live-Signale",
            "description": "Kuratiertes Live-Signalset fur aktuelle Leistungswerte.",
            "requirement_label": "MUSS/KANN",
            "repeatable": False,
            "add_entity_type": None,
        },
    }
    _UI_TO_STORAGE_FACTORS: dict[str, float] = {
        _FIXED_IMPORT_PRICE_FIELD_ID: 0.01,  # ct/kWh -> EUR/kWh
        "param.feedintariff.provider_settings.FeedInTariffFixed.feed_in_tariff_kwh": 0.01,  # ct/kWh -> EUR/kWh
        "param.elecprice.charges_kwh": 0.01,  # ct/kWh -> EUR/kWh
        "signal.house_load_w": 1000.0,  # kW -> W
        "signal.pv_power_w": 1000.0,  # kW -> W
        "signal.grid_import_w": 1000.0,  # kW -> W
        "signal.grid_export_w": 1000.0,  # kW -> W
        "signal.battery_power_w": 1000.0,  # kW -> W
    }
    _UI_TO_STORAGE_FACTOR_PATTERNS: tuple[tuple[re.Pattern[str], float], ...] = (
        (re.compile(r"^param\.pvforecast\.planes\.\d+\.inverter_paco$"), 1000.0),
        (re.compile(r"^param\.devices\.batteries\.\d+\.capacity_wh$"), 1000.0),
        (re.compile(r"^param\.devices\.batteries\.\d+\.min_charge_power_w$"), 1000.0),
        (re.compile(r"^param\.devices\.batteries\.\d+\.max_charge_power_w$"), 1000.0),
        (re.compile(r"^param\.devices\.inverters\.\d+\.max_power_w$"), 1000.0),
        (re.compile(r"^param\.devices\.electric_vehicles\.\d+\.capacity_wh$"), 1000.0),
        (re.compile(r"^param\.devices\.electric_vehicles\.\d+\.min_charge_power_w$"), 1000.0),
        (re.compile(r"^param\.devices\.electric_vehicles\.\d+\.max_charge_power_w$"), 1000.0),
        (re.compile(r"^param\.devices\.home_appliances\.\d+\.consumption_wh$"), 1000.0),
    )
    _ENTITY_LIMITS: dict[str, int] = {
        "pv_plane": 32,
        "electric_vehicle": 32,
        "home_appliance": 32,
        "home_appliance_window": 96,
    }

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker,
        parameter_profile_service: ParameterProfileService,
        parameter_catalog_service: ParameterCatalogService,
        emr_pipeline_service: "EmrPipelineService | None" = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._parameter_profile_service = parameter_profile_service
        self._parameter_catalog_service = parameter_catalog_service
        self._emr_pipeline_service = emr_pipeline_service

    def list_fields(self, db: Session) -> list[SetupFieldResponse]:
        payload = self._load_current_payload(db)
        defs = self._field_defs(payload)
        latest_events = list_latest_setup_field_events(db, field_ids=[field.field_id for field in defs])
        signal_state = self._load_signal_state(db, [field.signal_key for field in defs if field.signal_key])
        now = datetime.now(timezone.utc)
        fields: list[SetupFieldResponse] = []
        for field in defs:
            state = self._build_field_state(
                field=field,
                payload=payload,
                signal_state=signal_state,
                latest_event=latest_events.get(field.field_id),
                now=now,
            )
            fields.append(state)
        return fields

    def get_layout(self, db: Session) -> SetupLayoutResponse:
        payload = self._load_current_payload(db)
        fields = self.list_fields(db)
        return self._build_layout(fields=fields, payload=payload)

    def mutate_entity(self, db: Session, *, request: SetupEntityMutateRequest) -> SetupEntityMutateResponse:
        payload = self._load_current_payload(db)
        mutable_payload = copy.deepcopy(payload)
        warnings: list[str] = []

        try:
            self._mutate_payload_for_request(payload=mutable_payload, request=request, warnings=warnings)
        except ValueError as exc:
            return SetupEntityMutateResponse(
                status="rejected",
                message=str(exc),
                warnings=warnings,
                layout=self.get_layout(db),
            )

        active_profile = self._ensure_active_profile(db)
        if active_profile is None:
            return SetupEntityMutateResponse(
                status="rejected",
                message="No active profile available",
                warnings=warnings,
                layout=self.get_layout(db),
            )

        event_id = create_setup_field_event(
            db,
            field_id=f"entity.{request.entity_type}",
            source=request.source,
            raw_value_text=_raw_value_text(request.model_dump(mode="json")),
            normalized_value=mutable_payload,
            event_ts=datetime.now(timezone.utc),
            apply_status="accepted",
            error_text=None,
        )

        validation = self._parameter_profile_service.validate_payload(
            payload_json=mutable_payload,
            strict_unknown_fields=True,
            fail_on_masked_secrets=True,
        )
        if not validation.valid or validation.normalized_payload is None:
            error_text = " | ".join(validation.errors) if validation.errors else "Validation failed"
            update_setup_field_events_status(
                db,
                event_ids=[event_id],
                apply_status="failed",
                error_text=error_text,
            )
            db.commit()
            return SetupEntityMutateResponse(
                status="rejected",
                message=error_text,
                warnings=warnings,
                layout=self.get_layout(db),
            )

        self._parameter_profile_service.save_profile_draft(
            db,
            profile_id=active_profile.id,
            payload_json=validation.normalized_payload,
            source="manual",
        )
        outcome = self._parameter_profile_service.apply_profile(
            db,
            profile_id=active_profile.id,
            set_active_profile=True,
        )
        if not outcome.valid:
            error_text = " | ".join(outcome.errors) if outcome.errors else "EOS apply failed"
            # Functional rollback of current draft on mutation failures.
            self._parameter_profile_service.save_profile_draft(
                db,
                profile_id=active_profile.id,
                payload_json=payload,
                source="manual",
            )
            update_setup_field_events_status(
                db,
                event_ids=[event_id],
                apply_status="failed",
                error_text=error_text,
            )
            db.commit()
            return SetupEntityMutateResponse(
                status="rejected",
                message=error_text,
                warnings=warnings,
                layout=self.get_layout(db),
            )

        update_setup_field_events_status(
            db,
            event_ids=[event_id],
            apply_status="applied",
            error_text=None,
        )
        db.commit()
        return SetupEntityMutateResponse(
            status="saved",
            message="Entity mutation applied.",
            warnings=warnings,
            layout=self.get_layout(db),
        )

    def patch_fields(
        self,
        db: Session,
        *,
        updates: list[SetupFieldUpdate],
    ) -> list[SetupFieldUpdateResult]:
        payload = self._load_current_payload(db)
        defs = self._field_defs(payload)
        defs_by_id = {field.field_id: field for field in defs}
        latest_events_before = list_latest_setup_field_events(db, field_ids=[field.field_id for field in defs])
        signal_state_before = self._load_signal_state(db, [field.signal_key for field in defs if field.signal_key])

        mutable_payload = copy.deepcopy(payload)
        param_event_ids: list[int] = []
        event_sources: list[str] = []
        results: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for update in updates:
            field = defs_by_id.get(update.field_id)
            event_ts = _coerce_datetime(update.ts if update.ts is not None else update.timestamp) or now
            if field is None:
                event_id = create_setup_field_event(
                    db,
                    field_id=update.field_id,
                    source=update.source,
                    raw_value_text=_raw_value_text(update.value),
                    normalized_value=None,
                    event_ts=event_ts,
                    apply_status="rejected",
                    error_text="Unknown field_id",
                )
                results.append(
                    {
                        "field_id": update.field_id,
                        "status": "rejected",
                        "error": "Unknown field_id",
                        "event_id": event_id,
                    }
                )
                continue

            effective_required = self._is_effectively_required(field, mutable_payload)
            normalized, error = self._normalize_field_value(
                field,
                update.value,
                required=effective_required,
                payload=mutable_payload,
            )
            if error is not None:
                event_id = create_setup_field_event(
                    db,
                    field_id=field.field_id,
                    source=update.source,
                    raw_value_text=_raw_value_text(update.value),
                    normalized_value=None,
                    event_ts=event_ts,
                    apply_status="rejected",
                    error_text=error,
                )
                results.append(
                    {
                        "field_id": field.field_id,
                        "status": "rejected",
                        "error": error,
                        "event_id": event_id,
                    }
                )
                continue

            storage_value = _to_storage_numeric_if_needed(
                field_id=field.field_id,
                value=normalized,
                factors=self._UI_TO_STORAGE_FACTORS,
                pattern_factors=self._UI_TO_STORAGE_FACTOR_PATTERNS,
            )

            if field.param_path is not None:
                synced_inverter_field_ids: list[str] = []
                synced_battery_id_value: str | None = None
                if field.field_id == self._FIXED_IMPORT_PRICE_FIELD_ID:
                    fixed_import_json = _build_fixed_elecprice_import_json(
                        payload=mutable_payload,
                        eur_per_kwh=float(storage_value),
                    )
                    _set_payload_path(
                        mutable_payload,
                        ("elecprice", "elecpriceimport", "import_json"),
                        fixed_import_json,
                    )
                    _set_payload_path(
                        mutable_payload,
                        ("elecprice", "elecpriceimport", "import_file_path"),
                        None,
                    )
                else:
                    battery_index = _battery_index_for_device_id_field(field.field_id)
                    old_battery_id: str | None = None
                    if battery_index is not None:
                        old_battery_id = _string_or_none_from_value(
                            _get_payload_path(mutable_payload, field.param_path)
                        )
                    _set_payload_path(mutable_payload, field.param_path, storage_value)
                    if battery_index is not None:
                        synced_battery_id_value = _string_or_none_from_value(storage_value)
                        synced_indices = _sync_inverter_battery_references(
                            payload=mutable_payload,
                            battery_index=battery_index,
                            old_battery_id=old_battery_id,
                            new_battery_id=synced_battery_id_value,
                        )
                        synced_inverter_field_ids = [
                            f"param.devices.inverters.{index}.battery_id" for index in synced_indices
                        ]
                event_id = create_setup_field_event(
                    db,
                    field_id=field.field_id,
                    source=update.source,
                    raw_value_text=_raw_value_text(update.value),
                    normalized_value=storage_value,
                    event_ts=event_ts,
                    apply_status="accepted",
                    error_text=None,
                )
                param_event_ids.append(event_id)
                event_sources.append(update.source)
                if synced_battery_id_value is not None:
                    for synced_field_id in synced_inverter_field_ids:
                        synced_event_id = create_setup_field_event(
                            db,
                            field_id=synced_field_id,
                            source=update.source,
                            raw_value_text=_raw_value_text(synced_battery_id_value),
                            normalized_value=synced_battery_id_value,
                            event_ts=event_ts,
                            apply_status="accepted",
                            error_text=None,
                        )
                        param_event_ids.append(synced_event_id)
                        event_sources.append(update.source)
                results.append(
                    {
                        "field_id": field.field_id,
                        "status": "saved",
                        "error": None,
                        "event_id": event_id,
                    }
                )
                continue

            if field.signal_key is not None:
                self._ingest_signal(
                    signal_key=field.signal_key,
                    value=storage_value,
                    source=update.source,
                    ts=event_ts,
                )
                event_id = create_setup_field_event(
                    db,
                    field_id=field.field_id,
                    source=update.source,
                    raw_value_text=_raw_value_text(update.value),
                    normalized_value=storage_value,
                    event_ts=event_ts,
                    apply_status="applied",
                    error_text=None,
                )
                results.append(
                    {
                        "field_id": field.field_id,
                        "status": "saved",
                        "error": None,
                        "event_id": event_id,
                    }
                )
                continue

            event_id = create_setup_field_event(
                db,
                field_id=field.field_id,
                source=update.source,
                raw_value_text=_raw_value_text(update.value),
                normalized_value=None,
                event_ts=event_ts,
                apply_status="rejected",
                error_text="Field is not writable",
            )
            results.append(
                {
                    "field_id": field.field_id,
                    "status": "rejected",
                    "error": "Field is not writable",
                    "event_id": event_id,
                }
            )

        if param_event_ids:
            active_profile = self._ensure_active_profile(db)
            if active_profile is None:
                update_setup_field_events_status(
                    db,
                    event_ids=param_event_ids,
                    apply_status="failed",
                    error_text="No active profile",
                )
                for item in results:
                    if item["event_id"] in param_event_ids:
                        item["status"] = "rejected"
                        item["error"] = "No active profile"
            else:
                validation = self._parameter_profile_service.validate_payload(
                    payload_json=mutable_payload,
                    strict_unknown_fields=True,
                    fail_on_masked_secrets=True,
                )
                if not validation.valid or validation.normalized_payload is None:
                    error_text = (
                        " | ".join(validation.errors)
                        if validation.errors
                        else "Validation failed"
                    )
                    update_setup_field_events_status(
                        db,
                        event_ids=param_event_ids,
                        apply_status="failed",
                        error_text=error_text,
                    )
                    for item in results:
                        if item["event_id"] in param_event_ids:
                            item["status"] = "rejected"
                            item["error"] = error_text
                else:
                    revision_source = _profile_source_for_updates(event_sources)
                    self._parameter_profile_service.save_profile_draft(
                        db,
                        profile_id=active_profile.id,
                        payload_json=validation.normalized_payload,
                        source=revision_source,
                    )
                    outcome = self._parameter_profile_service.apply_profile(
                        db,
                        profile_id=active_profile.id,
                        set_active_profile=True,
                    )
                    if outcome.valid:
                        update_setup_field_events_status(
                            db,
                            event_ids=param_event_ids,
                            apply_status="applied",
                            error_text=None,
                        )
                    else:
                        error_text = " | ".join(outcome.errors) if outcome.errors else "EOS apply failed"
                        update_setup_field_events_status(
                            db,
                            event_ids=param_event_ids,
                            apply_status="failed",
                            error_text=error_text,
                        )
                        for item in results:
                            if item["event_id"] in param_event_ids:
                                item["status"] = "rejected"
                                item["error"] = error_text

        db.commit()

        refreshed_fields = self.list_fields(db)
        fields_by_id = {field.field_id: field for field in refreshed_fields}
        final_results: list[SetupFieldUpdateResult] = []
        for item in results:
            field_id = item["field_id"]
            field_state = fields_by_id.get(field_id)
            if field_state is None:
                field_state = SetupFieldResponse(
                    field_id=field_id,
                    group="optional",
                    label=field_id,
                    required=False,
                    value_type="string",
                    unit=None,
                    options=[],
                    current_value=None,
                    valid=False,
                    missing=True,
                    dirty=False,
                    last_source=None,
                    last_update_ts=None,
                    http_path_template=f"/eos/set/param/{field_id}=<value>",
                    http_override_active=False,
                    http_override_last_ts=None,
                    advanced=False,
                    item_key=None,
                    category_id=None,
                    error=item["error"],
                )
            final_results.append(
                SetupFieldUpdateResult(
                    field_id=field_id,
                    status=item["status"],
                    error=item["error"],
                    field=field_state,
                )
            )
        return final_results

    def readiness(self, db: Session) -> SetupReadinessResponse:
        fields = self.list_fields(db)
        items: list[SetupReadinessItem] = []
        blockers = 0
        warnings = 0

        for field in fields:
            if not field.required:
                continue
            if field.missing or not field.valid:
                items.append(
                    SetupReadinessItem(
                        field_id=field.field_id,
                        required=True,
                        status="blocked",
                        message="Pflichtfeld fehlt oder ist ungültig.",
                    )
                )
                blockers += 1

        # Cross-field validations for run minimum consistency.
        fields_by_id = {field.field_id: field for field in fields}
        battery_id = _string_or_none(fields_by_id.get("param.devices.batteries.0.device_id"))
        inverter_battery_id = _string_or_none(fields_by_id.get("param.devices.inverters.0.battery_id"))
        if battery_id and inverter_battery_id and inverter_battery_id != battery_id:
            items.append(
                SetupReadinessItem(
                    field_id="param.devices.inverters.0.battery_id",
                    required=True,
                    status="blocked",
                    message="Inverter battery_id passt nicht zur konfigurierten Batterie.",
                )
            )
            blockers += 1

        measurement_keys = _string_list_or_empty(fields_by_id.get("param.measurement.keys"))
        for emr_field_id in (
            "param.measurement.load_emr_keys",
            "param.measurement.grid_import_emr_keys",
            "param.measurement.grid_export_emr_keys",
            "param.measurement.pv_production_emr_keys",
        ):
            keys = _string_list_or_empty(fields_by_id.get(emr_field_id))
            for key in keys:
                if key not in measurement_keys:
                    items.append(
                        SetupReadinessItem(
                            field_id=emr_field_id,
                            required=True,
                            status="blocked",
                            message=f"Key '{key}' fehlt in measurement.keys.",
                        )
                    )
                    blockers += 1

        readiness_level: str = "ready"
        if blockers > 0:
            readiness_level = "blocked"
        elif warnings > 0:
            readiness_level = "degraded"
        return SetupReadinessResponse(
            readiness_level=readiness_level,  # type: ignore[arg-type]
            blockers_count=blockers,
            warnings_count=warnings,
            items=items,
        )

    def export_package(self, db: Session) -> SetupExportResponse:
        payload = self._load_current_payload(db)
        signal_values = self._load_signal_values_for_export(db)
        return SetupExportResponse(
            format="eos-webapp.inputs-setup.v2",
            exported_at=datetime.now(timezone.utc),
            payload={
                "parameter_payload": payload,
                "signal_values": signal_values,
            },
        )

    def import_package(self, db: Session, *, package_json: dict[str, Any]) -> SetupImportResponse:
        fmt = package_json.get("format")
        if fmt != "eos-webapp.inputs-setup.v2":
            raise ValueError("Unsupported import format. Expected eos-webapp.inputs-setup.v2")
        payload = package_json.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Import payload must be an object")
        parameter_payload = payload.get("parameter_payload")
        if not isinstance(parameter_payload, dict):
            raise ValueError("Import payload.parameter_payload must be an object")

        active_profile = self._ensure_active_profile(db)
        if active_profile is None:
            raise ValueError("No active profile available")
        self._parameter_profile_service.save_profile_draft(
            db,
            profile_id=active_profile.id,
            payload_json=parameter_payload,
            source="import",
        )
        outcome = self._parameter_profile_service.apply_profile(
            db,
            profile_id=active_profile.id,
            set_active_profile=True,
        )
        if not outcome.valid:
            raise ValueError(" | ".join(outcome.errors) if outcome.errors else "Import apply failed")

        warnings: list[str] = []
        signal_values = payload.get("signal_values")
        if signal_values is not None:
            if not isinstance(signal_values, dict):
                warnings.append("signal_values ignored: expected object")
            else:
                now = datetime.now(timezone.utc)
                for key, value in signal_values.items():
                    field_id, input_scale_to_ui = _resolve_signal_field_id_and_input_scale(str(key))
                    if field_id is None:
                        warnings.append(f"signal_values key '{key}' ignored")
                        continue
                    signal_def = next((field for field in self._field_defs(parameter_payload) if field.field_id == field_id), None)
                    if signal_def is None:
                        continue
                    value_for_normalize = value
                    if input_scale_to_ui != 1.0:
                        scaled_value, scale_error = _scale_numeric_value(value=value, factor=input_scale_to_ui)
                        if scale_error is not None:
                            warnings.append(f"signal '{key}' ignored: {scale_error}")
                            continue
                        value_for_normalize = scaled_value
                    signal_required = self._is_effectively_required(signal_def, parameter_payload)
                    normalized, error = self._normalize_field_value(
                        signal_def,
                        value_for_normalize,
                        required=signal_required,
                        payload=parameter_payload,
                    )
                    if error is not None:
                        warnings.append(f"signal '{key}' ignored: {error}")
                        continue
                    storage_value = _to_storage_numeric_if_needed(
                        field_id=signal_def.field_id,
                        value=normalized,
                        factors=self._UI_TO_STORAGE_FACTORS,
                        pattern_factors=self._UI_TO_STORAGE_FACTOR_PATTERNS,
                    )
                    self._ingest_signal(
                        signal_key=signal_def.signal_key or key,
                        value=storage_value,
                        source="import",
                        ts=now,
                    )

        db.commit()
        return SetupImportResponse(applied=True, message="Import angewendet.", warnings=warnings)

    def parse_set_path(
        self,
        db: Session,
        *,
        raw_path: str,
        value: Any,
        source: str,
        explicit_ts: datetime | None,
    ) -> SetupFieldUpdateResult:
        payload = self._load_current_payload(db)
        field_id, normalized_value = _resolve_http_set_field_id(raw_path=raw_path, payload=payload, value=value)
        update = SetupFieldUpdate(
            field_id=field_id,
            value=normalized_value,
            source=source,  # type: ignore[arg-type]
            ts=explicit_ts.isoformat() if explicit_ts is not None else None,
        )
        results = self.patch_fields(db, updates=[update])
        return results[0]

    def _build_layout(
        self,
        *,
        fields: list[SetupFieldResponse],
        payload: dict[str, Any],
    ) -> SetupLayoutResponse:
        by_category: dict[str, list[SetupFieldResponse]] = {}
        for field in fields:
            category_id = field.category_id or _category_id_for_field(field.field_id, field.group)
            by_category.setdefault(category_id, []).append(field)

        categories: list[SetupCategoryResponse] = []
        invalid_total = 0

        for category_id in self._CATEGORY_ORDER:
            meta = self._CATEGORY_META.get(category_id, {})
            category_fields = by_category.get(category_id, [])
            required_count = sum(1 for field in category_fields if field.required)
            invalid_required_count = sum(
                1
                for field in category_fields
                if field.required and (field.missing or not field.valid)
            )
            invalid_total += invalid_required_count

            item_groups: dict[str, list[SetupFieldResponse]] = {}
            for field in category_fields:
                key = field.item_key or f"{category_id}:base"
                item_groups.setdefault(key, []).append(field)

            items: list[SetupCategoryItemResponse] = []
            for item_key in sorted(item_groups.keys(), key=_item_sort_key):
                item_fields = sorted(
                    item_groups[item_key],
                    key=lambda field: (field.advanced, field.label.lower(), field.field_id),
                )
                entity_type = _entity_type_for_item_key(item_key)
                parent_item_key = _parent_item_key_for_item_key(item_key)
                deletable = _is_item_deletable(item_key=item_key)
                item_required_count = sum(1 for field in item_fields if field.required)
                item_invalid_required_count = sum(
                    1
                    for field in item_fields
                    if field.required and (field.missing or not field.valid)
                )
                items.append(
                    SetupCategoryItemResponse(
                        item_key=item_key,
                        label=_item_label_for_item(item_key=item_key, fields=item_fields),
                        entity_type=entity_type,  # type: ignore[arg-type]
                        parent_item_key=parent_item_key,
                        deletable=deletable,
                        base_object=not deletable,
                        required_count=item_required_count,
                        invalid_required_count=item_invalid_required_count,
                        fields=item_fields,
                    )
                )

            item_limit = self._category_item_limit(category_id=category_id)
            categories.append(
                SetupCategoryResponse(
                    category_id=category_id,
                    title=str(meta.get("title", category_id)),
                    description=(
                        str(meta["description"])
                        if isinstance(meta.get("description"), str)
                        else None
                    ),
                    requirement_label=str(meta.get("requirement_label", "KANN")),  # type: ignore[arg-type]
                    repeatable=bool(meta.get("repeatable", False)),
                    add_entity_type=meta.get("add_entity_type"),  # type: ignore[arg-type]
                    default_open=required_count > 0,
                    required_count=required_count,
                    invalid_required_count=invalid_required_count,
                    item_limit=item_limit,
                    items=items,
                )
            )

        return SetupLayoutResponse(
            generated_at=datetime.now(timezone.utc),
            invalid_required_total=invalid_total,
            categories=categories,
        )

    def _category_item_limit(self, *, category_id: str) -> int | None:
        if category_id == "pv_forecast":
            return self._ENTITY_LIMITS.get("pv_plane")
        if category_id == "electric_vehicles":
            return self._ENTITY_LIMITS.get("electric_vehicle")
        if category_id == "home_appliances":
            return self._ENTITY_LIMITS.get("home_appliance")
        return None

    def _mutate_payload_for_request(
        self,
        *,
        payload: dict[str, Any],
        request: SetupEntityMutateRequest,
        warnings: list[str],
    ) -> None:
        action = request.action
        entity_type = request.entity_type

        if action == "add":
            if entity_type == "pv_plane":
                planes = _ensure_payload_list(payload, ("pvforecast", "planes"))
                self._assert_limit_not_reached(entity_type=entity_type, count=len(planes))
                source_plane = _select_clone_source(
                    items=planes,
                    clone_from_item_key=request.clone_from_item_key,
                    expected_prefix="pv_plane",
                )
                if source_plane is None:
                    source_plane = planes[0] if planes else _default_pv_plane_template()
                new_plane = copy.deepcopy(source_plane if isinstance(source_plane, dict) else _default_pv_plane_template())
                planes.append(new_plane)
            elif entity_type == "electric_vehicle":
                vehicles = _ensure_payload_list(payload, ("devices", "electric_vehicles"))
                self._assert_limit_not_reached(entity_type=entity_type, count=len(vehicles))
                source_vehicle = _select_clone_source(
                    items=vehicles,
                    clone_from_item_key=request.clone_from_item_key,
                    expected_prefix="electric_vehicle",
                )
                if source_vehicle is None:
                    source_vehicle = vehicles[0] if vehicles else _default_ev_template()
                new_vehicle = copy.deepcopy(source_vehicle if isinstance(source_vehicle, dict) else _default_ev_template())
                _ensure_vehicle_defaults(new_vehicle)
                existing_ids = _collect_device_ids(vehicles)
                new_vehicle["device_id"] = _assign_unique_device_id(
                    preferred=_string_or_none_from_value(new_vehicle.get("device_id")),
                    existing_ids=existing_ids,
                    prefix="ev",
                )
                vehicles.append(new_vehicle)
            elif entity_type == "home_appliance":
                appliances = _ensure_payload_list(payload, ("devices", "home_appliances"))
                self._assert_limit_not_reached(entity_type=entity_type, count=len(appliances))
                source_appliance = _select_clone_source(
                    items=appliances,
                    clone_from_item_key=request.clone_from_item_key,
                    expected_prefix="home_appliance",
                )
                if source_appliance is None:
                    source_appliance = appliances[0] if appliances else _default_home_appliance_template()
                new_appliance = copy.deepcopy(
                    source_appliance if isinstance(source_appliance, dict) else _default_home_appliance_template()
                )
                _ensure_home_appliance_defaults(new_appliance)
                existing_ids = _collect_device_ids(appliances)
                new_appliance["device_id"] = _assign_unique_device_id(
                    preferred=_string_or_none_from_value(new_appliance.get("device_id")),
                    existing_ids=existing_ids,
                    prefix="appliance",
                )
                windows = _ensure_home_appliance_windows(new_appliance)
                if not windows:
                    windows.append(_default_home_appliance_window())
                appliances.append(new_appliance)
            elif entity_type == "home_appliance_window":
                if request.parent_item_key is None:
                    raise ValueError("parent_item_key is required for home_appliance_window add")
                appliance_index = _parse_item_index(request.parent_item_key, expected_prefix="home_appliance")
                appliances = _ensure_payload_list(payload, ("devices", "home_appliances"))
                if appliance_index < 0 or appliance_index >= len(appliances):
                    raise ValueError("parent_item_key not found")
                appliance = appliances[appliance_index]
                if not isinstance(appliance, dict):
                    appliance = {}
                    appliances[appliance_index] = appliance
                windows = _ensure_home_appliance_windows(appliance)
                self._assert_limit_not_reached(entity_type=entity_type, count=len(windows))

                source_window: dict[str, Any] | None = None
                if request.clone_from_item_key is not None:
                    clone_appliance_index, clone_window_index = _parse_window_item_key(request.clone_from_item_key)
                    if clone_appliance_index < 0 or clone_appliance_index >= len(appliances):
                        raise ValueError("clone_from_item_key not found")
                    clone_appliance = appliances[clone_appliance_index]
                    if not isinstance(clone_appliance, dict):
                        raise ValueError("clone_from_item_key not found")
                    clone_windows = _ensure_home_appliance_windows(clone_appliance)
                    if clone_window_index < 0 or clone_window_index >= len(clone_windows):
                        raise ValueError("clone_from_item_key not found")
                    clone_window = clone_windows[clone_window_index]
                    if isinstance(clone_window, dict):
                        source_window = clone_window
                if source_window is None:
                    source_window = windows[0] if windows and isinstance(windows[0], dict) else _default_home_appliance_window()
                windows.append(copy.deepcopy(source_window))
            else:
                raise ValueError(f"unsupported entity_type '{entity_type}'")
        elif action == "remove":
            if request.item_key is None:
                raise ValueError("item_key is required for remove")

            if entity_type == "pv_plane":
                plane_index = _parse_item_index(request.item_key, expected_prefix="pv_plane")
                if plane_index == 0:
                    raise ValueError("PV Plane #1 is a base object and cannot be removed")
                planes = _ensure_payload_list(payload, ("pvforecast", "planes"))
                if plane_index < 0 or plane_index >= len(planes):
                    raise ValueError("item_key not found")
                planes.pop(plane_index)
            elif entity_type == "electric_vehicle":
                vehicle_index = _parse_item_index(request.item_key, expected_prefix="electric_vehicle")
                vehicles = _ensure_payload_list(payload, ("devices", "electric_vehicles"))
                if vehicle_index < 0 or vehicle_index >= len(vehicles):
                    raise ValueError("item_key not found")
                vehicles.pop(vehicle_index)
            elif entity_type == "home_appliance":
                appliance_index = _parse_item_index(request.item_key, expected_prefix="home_appliance")
                appliances = _ensure_payload_list(payload, ("devices", "home_appliances"))
                if appliance_index < 0 or appliance_index >= len(appliances):
                    raise ValueError("item_key not found")
                appliances.pop(appliance_index)
            elif entity_type == "home_appliance_window":
                appliance_index, window_index = _parse_window_item_key(request.item_key)
                appliances = _ensure_payload_list(payload, ("devices", "home_appliances"))
                if appliance_index < 0 or appliance_index >= len(appliances):
                    raise ValueError("item_key not found")
                appliance = appliances[appliance_index]
                if not isinstance(appliance, dict):
                    raise ValueError("item_key not found")
                windows = _ensure_home_appliance_windows(appliance)
                if window_index < 0 or window_index >= len(windows):
                    raise ValueError("item_key not found")
                windows.pop(window_index)
            else:
                raise ValueError(f"unsupported entity_type '{entity_type}'")
        else:
            raise ValueError(f"unsupported action '{action}'")

        self._sync_repeatable_max_fields(payload)
        if request.action == "add" and request.clone_from_item_key is None:
            warnings.append("No clone source given; template fallback used where needed.")

    def _assert_limit_not_reached(self, *, entity_type: str, count: int) -> None:
        limit = self._ENTITY_LIMITS.get(entity_type)
        if limit is not None and count >= limit:
            raise ValueError(f"limit reached for {entity_type} ({limit})")

    def _sync_repeatable_max_fields(self, payload: dict[str, Any]) -> None:
        planes = _get_payload_path(payload, ("pvforecast", "planes"))
        if isinstance(planes, list):
            _set_payload_path(payload, ("pvforecast", "max_planes"), len(planes))

        vehicles = _get_payload_path(payload, ("devices", "electric_vehicles"))
        if isinstance(vehicles, list):
            _set_payload_path(payload, ("devices", "max_electric_vehicles"), len(vehicles))

        appliances = _get_payload_path(payload, ("devices", "home_appliances"))
        if isinstance(appliances, list):
            _set_payload_path(payload, ("devices", "max_home_appliances"), len(appliances))

    def _field_defs(self, payload: dict[str, Any]) -> list[SetupFieldDef]:
        battery_selector = _battery_selector(payload)
        inverter_selector = _inverter_selector(payload)
        defs: list[SetupFieldDef] = [
            SetupFieldDef("param.general.latitude", "mandatory", "Breitengrad", True, "number", "°", None, -90, 90, ("general", "latitude"), None, "/eos/set/param/general/latitude=<value>"),
            SetupFieldDef("param.general.longitude", "mandatory", "Längengrad", True, "number", "°", None, -180, 180, ("general", "longitude"), None, "/eos/set/param/general/longitude=<value>"),
            SetupFieldDef("param.pvforecast.provider", "mandatory", "PV Forecast Provider", True, "select", None, "pvforecast.provider", None, None, ("pvforecast", "provider"), None, "/eos/set/param/pvforecast/provider=<value>"),
            SetupFieldDef("param.pvforecast.planes.0.peakpower", "mandatory", "PV Plane #1 Peakpower", True, "number", "kW", None, 0, None, ("pvforecast", "planes", 0, "peakpower"), None, "/eos/set/param/pvforecast/planes/0/peakpower=<value>"),
            SetupFieldDef("param.pvforecast.planes.0.surface_azimuth", "mandatory", "PV Plane #1 Azimuth", True, "number", "°", None, 0, 360, ("pvforecast", "planes", 0, "surface_azimuth"), None, "/eos/set/param/pvforecast/planes/0/surface_azimuth=<value>"),
            SetupFieldDef("param.pvforecast.planes.0.surface_tilt", "mandatory", "PV Plane #1 Tilt", True, "number", "°", None, 0, 90, ("pvforecast", "planes", 0, "surface_tilt"), None, "/eos/set/param/pvforecast/planes/0/surface_tilt=<value>"),
            SetupFieldDef("param.pvforecast.planes.0.inverter_paco", "mandatory", "PV Plane #1 Inverter PACO", True, "number", "kW", None, 0, None, ("pvforecast", "planes", 0, "inverter_paco"), None, "/eos/set/param/pvforecast/planes/0/inverter_paco_kw=<value>"),
            SetupFieldDef("param.elecprice.provider", "mandatory", "Strompreis Provider", True, "select", None, "elecprice.provider", None, None, ("elecprice", "provider"), None, "/eos/set/param/elecprice/provider=<value>"),
            SetupFieldDef(self._FIXED_IMPORT_PRICE_FIELD_ID, "mandatory", "Bezugspreis fix (EOS Import-Serie)", True, "number", "ct/kWh", None, 0, None, ("elecprice", "elecpriceimport", "import_json"), None, "/eos/set/param/elecprice/elecpriceimport/import_json/value_ct_per_kwh=<value>"),
            SetupFieldDef("param.feedintariff.provider", "mandatory", "Einspeisevergutung Provider", True, "select", None, "feedintariff.provider", None, None, ("feedintariff", "provider"), None, "/eos/set/param/feedintariff/provider=<value>"),
            SetupFieldDef("param.feedintariff.provider_settings.FeedInTariffFixed.feed_in_tariff_kwh", "mandatory", "Fester Einspeisetarif", True, "number", "ct/kWh", None, 0, None, ("feedintariff", "provider_settings", "FeedInTariffFixed", "feed_in_tariff_kwh"), None, "/eos/set/param/feedintariff/provider_settings/FeedInTariffFixed/feed_in_tariff_ct_per_kwh=<value>"),
            SetupFieldDef("param.load.provider", "mandatory", "Last Provider", True, "select", None, "load.provider", None, None, ("load", "provider"), None, "/eos/set/param/load/provider=<value>"),
            SetupFieldDef("param.load.provider_settings.LoadAkkudoktor.loadakkudoktor_year_energy_kwh", "mandatory", "Jahresverbrauch", True, "number", "kWh/a", None, 0, None, ("load", "provider_settings", "LoadAkkudoktor", "loadakkudoktor_year_energy_kwh"), None, "/eos/set/param/load/provider_settings/LoadAkkudoktor/loadakkudoktor_year_energy_kwh=<value>"),
            SetupFieldDef("param.devices.batteries.0.device_id", "mandatory", "Batterie #1 Device ID", True, "string", None, None, None, None, ("devices", "batteries", 0, "device_id"), None, "/eos/set/param/devices/batteries/0/device_id=<value>"),
            SetupFieldDef("param.devices.batteries.0.capacity_wh", "mandatory", "Batterie #1 Kapazitat", True, "number", "kWh", None, 0, None, ("devices", "batteries", 0, "capacity_wh"), None, f"/eos/set/param/devices/batteries/{battery_selector}/capacity_kwh=<value>"),
            SetupFieldDef("param.devices.batteries.0.min_charge_power_w", "mandatory", "Batterie #1 Min Charge", True, "number", "kW", None, 0, None, ("devices", "batteries", 0, "min_charge_power_w"), None, f"/eos/set/param/devices/batteries/{battery_selector}/min_charge_power_kw=<value>"),
            SetupFieldDef("param.devices.batteries.0.max_charge_power_w", "mandatory", "Batterie #1 Max Charge", True, "number", "kW", None, 0, None, ("devices", "batteries", 0, "max_charge_power_w"), None, f"/eos/set/param/devices/batteries/{battery_selector}/max_charge_power_kw=<value>"),
            SetupFieldDef("param.devices.batteries.0.min_soc_percentage", "mandatory", "Batterie #1 Min SOC", True, "number", "%", None, 0, 100, ("devices", "batteries", 0, "min_soc_percentage"), None, f"/eos/set/param/devices/batteries/{battery_selector}/min_soc_percentage=<value>"),
            SetupFieldDef("param.devices.batteries.0.max_soc_percentage", "mandatory", "Batterie #1 Max SOC", True, "number", "%", None, 0, 100, ("devices", "batteries", 0, "max_soc_percentage"), None, f"/eos/set/param/devices/batteries/{battery_selector}/max_soc_percentage=<value>"),
            SetupFieldDef("param.devices.inverters.0.device_id", "mandatory", "Inverter #1 Device ID", True, "string", None, None, None, None, ("devices", "inverters", 0, "device_id"), None, "/eos/set/param/devices/inverters/0/device_id=<value>"),
            SetupFieldDef("param.devices.inverters.0.max_power_w", "mandatory", "Inverter #1 Max Power", True, "number", "kW", None, 0, None, ("devices", "inverters", 0, "max_power_w"), None, f"/eos/set/param/devices/inverters/{inverter_selector}/max_power_kw=<value>"),
            SetupFieldDef("param.devices.inverters.0.battery_id", "mandatory", "Inverter #1 Battery ID", True, "select", None, "devices.battery_ids", None, None, ("devices", "inverters", 0, "battery_id"), None, f"/eos/set/param/devices/inverters/{inverter_selector}/battery_id=<value>"),
            SetupFieldDef("param.measurement.keys", "mandatory", "Measurement Keys", True, "string_list", None, None, None, None, ("measurement", "keys"), None, "/eos/set/param/measurement/keys=<csv>"),
            SetupFieldDef("param.measurement.load_emr_keys", "mandatory", "Load EMR Keys", True, "string_list", None, None, None, None, ("measurement", "load_emr_keys"), None, "/eos/set/param/measurement/load_emr_keys=<csv>"),
            SetupFieldDef("param.measurement.grid_import_emr_keys", "mandatory", "Grid Import EMR Keys", True, "string_list", None, None, None, None, ("measurement", "grid_import_emr_keys"), None, "/eos/set/param/measurement/grid_import_emr_keys=<csv>"),
            SetupFieldDef("param.measurement.grid_export_emr_keys", "mandatory", "Grid Export EMR Keys", True, "string_list", None, None, None, None, ("measurement", "grid_export_emr_keys"), None, "/eos/set/param/measurement/grid_export_emr_keys=<csv>"),
            SetupFieldDef("param.measurement.pv_production_emr_keys", "mandatory", "PV Production EMR Keys", True, "string_list", None, None, None, None, ("measurement", "pv_production_emr_keys"), None, "/eos/set/param/measurement/pv_production_emr_keys=<csv>"),
            SetupFieldDef("param.pvforecast.planes.0.loss", "optional", "PV Plane #1 Loss", False, "number", "%", None, 0, 100, ("pvforecast", "planes", 0, "loss"), None, "/eos/set/param/pvforecast/planes/0/loss=<value>", advanced=True),
            SetupFieldDef("param.pvforecast.planes.0.trackingtype", "optional", "PV Plane #1 Trackingtype", False, "string", None, None, None, None, ("pvforecast", "planes", 0, "trackingtype"), None, "/eos/set/param/pvforecast/planes/0/trackingtype=<value>", advanced=True),
            SetupFieldDef("param.elecprice.charges_kwh", "optional", "Strompreis Zuschlag", False, "number", "ct/kWh", None, 0, None, ("elecprice", "charges_kwh"), None, "/eos/set/param/elecprice/charges_ct_per_kwh=<value>", advanced=True),
            SetupFieldDef("param.elecprice.vat_rate", "optional", "MwSt Faktor", False, "number", "x", None, 0, None, ("elecprice", "vat_rate"), None, "/eos/set/param/elecprice/vat_rate=<value>", advanced=True),
            SetupFieldDef("param.elecprice.energycharts.bidding_zone", "optional", "EnergyCharts Zone", False, "select", None, "elecprice.energycharts.bidding_zone", None, None, ("elecprice", "energycharts", "bidding_zone"), None, "/eos/set/param/elecprice/energycharts/bidding_zone=<value>", advanced=True),
            SetupFieldDef("param.prediction.hours", "optional", "Vorschau-Horizont", False, "number", "h", None, 1, 192, ("prediction", "hours"), None, "/eos/set/param/prediction/hours=<value>", advanced=True),
            SetupFieldDef("param.prediction.historic_hours", "optional", "Prediction Historie", False, "number", "h", None, 1, 672, ("prediction", "historic_hours"), None, "/eos/set/param/prediction/historic_hours=<value>", advanced=True),
            SetupFieldDef("signal.house_load_w", "live", "Hauslast", True, "number", "kW", None, 0, 100, None, "house_load_w", "/eos/set/signal/house_load_kw=<value>"),
            SetupFieldDef("signal.pv_power_w", "live", "PV Leistung", True, "number", "kW", None, 0, 100, None, "pv_power_w", "/eos/set/signal/pv_power_kw=<value>"),
            SetupFieldDef("signal.grid_import_w", "live", "Netzbezug", True, "number", "kW", None, 0, 100, None, "grid_import_w", "/eos/set/signal/grid_import_kw=<value>"),
            SetupFieldDef("signal.grid_export_w", "live", "Netzeinspeisung", True, "number", "kW", None, 0, 100, None, "grid_export_w", "/eos/set/signal/grid_export_kw=<value>"),
            SetupFieldDef("signal.battery_power_w", "live", "Batterieleistung", False, "number", "kW", None, -100, 100, None, "battery_power_w", "/eos/set/signal/battery_power_kw=<value>"),
            SetupFieldDef("signal.battery_soc_pct", "live", "Batterie-SOC", False, "number", "%", None, 0, 100, None, "battery_soc_pct", "/eos/set/signal/battery_soc_pct=<value>"),
        ]

        if _payload_has_path(payload, ("optimization", "horizon_hours")):
            defs.append(
                SetupFieldDef(
                    "param.optimization.horizon_hours",
                    "optional",
                    "Optimierungs-Horizont",
                    False,
                    "number",
                    "h",
                    None,
                    1,
                    192,
                    ("optimization", "horizon_hours"),
                    None,
                    "/eos/set/param/optimization/horizon_hours=<value>",
                    advanced=True,
                )
            )
        if _payload_has_path(payload, ("optimization", "hours")):
            defs.append(
                SetupFieldDef(
                    "param.optimization.hours",
                    "optional",
                    "Optimierungs-Horizont (legacy)",
                    False,
                    "number",
                    "h",
                    None,
                    1,
                    192,
                    ("optimization", "hours"),
                    None,
                    "/eos/set/param/optimization/hours=<value>",
                    advanced=True,
                )
            )

        planes = _get_payload_path(payload, ("pvforecast", "planes"))
        if not isinstance(planes, list):
            planes = []
        for plane_index in range(1, len(planes)):
            defs.extend(
                [
                    SetupFieldDef(
                        f"param.pvforecast.planes.{plane_index}.peakpower",
                        "optional",
                        f"PV Plane #{plane_index + 1} Peakpower",
                        True,
                        "number",
                        "kW",
                        None,
                        0,
                        None,
                        ("pvforecast", "planes", plane_index, "peakpower"),
                        None,
                        f"/eos/set/param/pvforecast/planes/{plane_index}/peakpower=<value>",
                    ),
                    SetupFieldDef(
                        f"param.pvforecast.planes.{plane_index}.surface_azimuth",
                        "optional",
                        f"PV Plane #{plane_index + 1} Azimuth",
                        True,
                        "number",
                        "°",
                        None,
                        0,
                        360,
                        ("pvforecast", "planes", plane_index, "surface_azimuth"),
                        None,
                        f"/eos/set/param/pvforecast/planes/{plane_index}/surface_azimuth=<value>",
                    ),
                    SetupFieldDef(
                        f"param.pvforecast.planes.{plane_index}.surface_tilt",
                        "optional",
                        f"PV Plane #{plane_index + 1} Tilt",
                        True,
                        "number",
                        "°",
                        None,
                        0,
                        90,
                        ("pvforecast", "planes", plane_index, "surface_tilt"),
                        None,
                        f"/eos/set/param/pvforecast/planes/{plane_index}/surface_tilt=<value>",
                    ),
                    SetupFieldDef(
                        f"param.pvforecast.planes.{plane_index}.inverter_paco",
                        "optional",
                        f"PV Plane #{plane_index + 1} Inverter PACO",
                        True,
                        "number",
                        "kW",
                        None,
                        0,
                        None,
                        ("pvforecast", "planes", plane_index, "inverter_paco"),
                        None,
                        f"/eos/set/param/pvforecast/planes/{plane_index}/inverter_paco_kw=<value>",
                    ),
                    SetupFieldDef(
                        f"param.pvforecast.planes.{plane_index}.loss",
                        "optional",
                        f"PV Plane #{plane_index + 1} Loss",
                        False,
                        "number",
                        "%",
                        None,
                        0,
                        100,
                        ("pvforecast", "planes", plane_index, "loss"),
                        None,
                        f"/eos/set/param/pvforecast/planes/{plane_index}/loss=<value>",
                        advanced=True,
                    ),
                    SetupFieldDef(
                        f"param.pvforecast.planes.{plane_index}.trackingtype",
                        "optional",
                        f"PV Plane #{plane_index + 1} Trackingtype",
                        False,
                        "string",
                        None,
                        None,
                        None,
                        None,
                        ("pvforecast", "planes", plane_index, "trackingtype"),
                        None,
                        f"/eos/set/param/pvforecast/planes/{plane_index}/trackingtype=<value>",
                        advanced=True,
                    ),
                ]
            )

        electric_vehicles = _get_payload_path(payload, ("devices", "electric_vehicles"))
        if isinstance(electric_vehicles, list):
            for vehicle_index, vehicle in enumerate(electric_vehicles):
                selector = _device_selector_for_item(vehicle, vehicle_index)
                defs.extend(
                    [
                        SetupFieldDef(
                            f"param.devices.electric_vehicles.{vehicle_index}.device_id",
                            "optional",
                            f"E-Auto #{vehicle_index + 1} Device ID",
                            True,
                            "string",
                            None,
                            None,
                            None,
                            None,
                            ("devices", "electric_vehicles", vehicle_index, "device_id"),
                            None,
                            f"/eos/set/param/devices/electric_vehicles/{selector}/device_id=<value>",
                        ),
                        SetupFieldDef(
                            f"param.devices.electric_vehicles.{vehicle_index}.capacity_wh",
                            "optional",
                            f"E-Auto #{vehicle_index + 1} Kapazitat",
                            True,
                            "number",
                            "kWh",
                            None,
                            0,
                            None,
                            ("devices", "electric_vehicles", vehicle_index, "capacity_wh"),
                            None,
                            f"/eos/set/param/devices/electric_vehicles/{selector}/capacity_kwh=<value>",
                        ),
                        SetupFieldDef(
                            f"param.devices.electric_vehicles.{vehicle_index}.min_charge_power_w",
                            "optional",
                            f"E-Auto #{vehicle_index + 1} Min Charge",
                            True,
                            "number",
                            "kW",
                            None,
                            0,
                            None,
                            ("devices", "electric_vehicles", vehicle_index, "min_charge_power_w"),
                            None,
                            f"/eos/set/param/devices/electric_vehicles/{selector}/min_charge_power_kw=<value>",
                        ),
                        SetupFieldDef(
                            f"param.devices.electric_vehicles.{vehicle_index}.max_charge_power_w",
                            "optional",
                            f"E-Auto #{vehicle_index + 1} Max Charge",
                            True,
                            "number",
                            "kW",
                            None,
                            0,
                            None,
                            ("devices", "electric_vehicles", vehicle_index, "max_charge_power_w"),
                            None,
                            f"/eos/set/param/devices/electric_vehicles/{selector}/max_charge_power_kw=<value>",
                        ),
                        SetupFieldDef(
                            f"param.devices.electric_vehicles.{vehicle_index}.min_soc_percentage",
                            "optional",
                            f"E-Auto #{vehicle_index + 1} Min SOC",
                            True,
                            "number",
                            "%",
                            None,
                            0,
                            100,
                            ("devices", "electric_vehicles", vehicle_index, "min_soc_percentage"),
                            None,
                            f"/eos/set/param/devices/electric_vehicles/{selector}/min_soc_percentage=<value>",
                        ),
                        SetupFieldDef(
                            f"param.devices.electric_vehicles.{vehicle_index}.max_soc_percentage",
                            "optional",
                            f"E-Auto #{vehicle_index + 1} Max SOC",
                            True,
                            "number",
                            "%",
                            None,
                            0,
                            100,
                            ("devices", "electric_vehicles", vehicle_index, "max_soc_percentage"),
                            None,
                            f"/eos/set/param/devices/electric_vehicles/{selector}/max_soc_percentage=<value>",
                        ),
                        SetupFieldDef(
                            f"param.devices.electric_vehicles.{vehicle_index}.charging_efficiency",
                            "optional",
                            f"E-Auto #{vehicle_index + 1} Charge-Effizienz",
                            False,
                            "number",
                            "x",
                            None,
                            0,
                            1.2,
                            ("devices", "electric_vehicles", vehicle_index, "charging_efficiency"),
                            None,
                            f"/eos/set/param/devices/electric_vehicles/{selector}/charging_efficiency=<value>",
                            advanced=True,
                        ),
                        SetupFieldDef(
                            f"param.devices.electric_vehicles.{vehicle_index}.discharging_efficiency",
                            "optional",
                            f"E-Auto #{vehicle_index + 1} Discharge-Effizienz",
                            False,
                            "number",
                            "x",
                            None,
                            0,
                            1.2,
                            ("devices", "electric_vehicles", vehicle_index, "discharging_efficiency"),
                            None,
                            f"/eos/set/param/devices/electric_vehicles/{selector}/discharging_efficiency=<value>",
                            advanced=True,
                        ),
                    ]
                )

        home_appliances = _get_payload_path(payload, ("devices", "home_appliances"))
        if isinstance(home_appliances, list):
            for appliance_index, appliance in enumerate(home_appliances):
                selector = _device_selector_for_item(appliance, appliance_index)
                defs.extend(
                    [
                        SetupFieldDef(
                            f"param.devices.home_appliances.{appliance_index}.device_id",
                            "optional",
                            f"Home-Appliance #{appliance_index + 1} Device ID",
                            True,
                            "string",
                            None,
                            None,
                            None,
                            None,
                            ("devices", "home_appliances", appliance_index, "device_id"),
                            None,
                            f"/eos/set/param/devices/home_appliances/{selector}/device_id=<value>",
                        ),
                        SetupFieldDef(
                            f"param.devices.home_appliances.{appliance_index}.consumption_wh",
                            "optional",
                            f"Home-Appliance #{appliance_index + 1} Verbrauch",
                            True,
                            "number",
                            "kWh",
                            None,
                            0,
                            None,
                            ("devices", "home_appliances", appliance_index, "consumption_wh"),
                            None,
                            f"/eos/set/param/devices/home_appliances/{selector}/consumption_kwh=<value>",
                        ),
                        SetupFieldDef(
                            f"param.devices.home_appliances.{appliance_index}.duration_h",
                            "optional",
                            f"Home-Appliance #{appliance_index + 1} Standard-Dauer",
                            False,
                            "number",
                            "h",
                            None,
                            0,
                            None,
                            ("devices", "home_appliances", appliance_index, "duration_h"),
                            None,
                            f"/eos/set/param/devices/home_appliances/{selector}/duration_h=<value>",
                            advanced=True,
                        ),
                    ]
                )
                if isinstance(appliance, dict) and "measurement_keys" in appliance:
                    defs.append(
                        SetupFieldDef(
                            f"param.devices.home_appliances.{appliance_index}.measurement_keys",
                            "optional",
                            f"Home-Appliance #{appliance_index + 1} Measurement Keys",
                            False,
                            "string_list",
                            None,
                            None,
                            None,
                            None,
                            ("devices", "home_appliances", appliance_index, "measurement_keys"),
                            None,
                            f"/eos/set/param/devices/home_appliances/{selector}/measurement_keys=<csv>",
                            advanced=True,
                        )
                    )
                windows = _home_appliance_windows_from_item(appliance)
                for window_index, _ in enumerate(windows):
                    defs.extend(
                        [
                            SetupFieldDef(
                                f"param.devices.home_appliances.{appliance_index}.time_windows.windows.{window_index}.start_time",
                                "optional",
                                f"Zeitfenster #{window_index + 1} Start",
                                True,
                                "string",
                                "HH:MM",
                                None,
                                None,
                                None,
                                ("devices", "home_appliances", appliance_index, "time_windows", "windows", window_index, "start_time"),
                                None,
                                f"/eos/set/param/devices/home_appliances/{selector}/time_windows/windows/{window_index}/start_time=<value>",
                                item_key=f"home_appliance:{appliance_index}:window:{window_index}",
                                category_id="home_appliances",
                            ),
                            SetupFieldDef(
                                f"param.devices.home_appliances.{appliance_index}.time_windows.windows.{window_index}.duration_h",
                                "optional",
                                f"Zeitfenster #{window_index + 1} Dauer",
                                True,
                                "number",
                                "h",
                                None,
                                0.0,
                                48.0,
                                ("devices", "home_appliances", appliance_index, "time_windows", "windows", window_index, "duration"),
                                None,
                                f"/eos/set/param/devices/home_appliances/{selector}/time_windows/windows/{window_index}/duration_h=<value>",
                                item_key=f"home_appliance:{appliance_index}:window:{window_index}",
                                category_id="home_appliances",
                            ),
                        ]
                    )

        return [self._annotate_field_def(field) for field in defs]

    def _annotate_field_def(self, field: SetupFieldDef) -> SetupFieldDef:
        category_id = field.category_id or _category_id_for_field(field.field_id, field.group)
        item_key = field.item_key or _item_key_for_field(field.field_id, category_id)
        advanced = field.advanced or _is_advanced_field(field.field_id)
        return replace(
            field,
            category_id=category_id,
            item_key=item_key,
            advanced=advanced,
        )

    def _build_field_state(
        self,
        *,
        field: SetupFieldDef,
        payload: dict[str, Any],
        signal_state: dict[str, dict[str, Any]],
        latest_event: dict[str, Any] | None,
        now: datetime,
    ) -> SetupFieldResponse:
        options = self._resolve_options(field, payload=payload)
        value: Any = None
        signal_last_ts: datetime | None = None
        if field.param_path is not None:
            value = _get_payload_path(payload, field.param_path)
            if field.field_id == self._FIXED_IMPORT_PRICE_FIELD_ID:
                value = _extract_fixed_elecprice_import_value_eur_per_kwh(value)
            if _is_home_appliance_window_start_field(field.field_id):
                value = _home_appliance_window_start_to_ui(value)
            if _is_home_appliance_window_duration_field(field.field_id):
                value = _home_appliance_window_duration_to_ui(value)
        elif field.signal_key is not None:
            signal_row = signal_state.get(field.signal_key)
            if signal_row is not None:
                value = _signal_row_value(signal_row)
                signal_last_ts = _to_utc_if_datetime(signal_row.get("last_ts"))
        value = _to_ui_numeric_if_needed(
            field_id=field.field_id,
            value=value,
            factors=self._UI_TO_STORAGE_FACTORS,
            pattern_factors=self._UI_TO_STORAGE_FACTOR_PATTERNS,
        )

        effective_required = self._is_effectively_required(field, payload)
        valid, error = self._validate_value(
            field,
            value,
            options,
            required=effective_required,
        )
        missing = effective_required and _is_missing(value)
        if field.signal_key is not None and effective_required and signal_last_ts is not None:
            stale_seconds = max(
                1,
                self._settings.setup_check_live_stale_seconds or self._settings.live_stale_seconds,
            )
            age_seconds = (now - signal_last_ts).total_seconds()
            if age_seconds > stale_seconds:
                valid = False
                missing = True
                error = f"stale signal ({int(age_seconds)}s)"

        last_source = None
        last_update_ts = None
        http_override_last_ts = None
        http_override_active = False
        event_error = None
        if latest_event is not None:
            source = latest_event.get("source")
            if isinstance(source, str):
                last_source = source
            ts_value = latest_event.get("event_ts")
            if isinstance(ts_value, datetime):
                last_update_ts = _to_utc(ts_value)
            elif isinstance(ts_value, str):
                try:
                    last_update_ts = _to_utc(datetime.fromisoformat(ts_value.replace("Z", "+00:00")))
                except ValueError:
                    last_update_ts = None

            if last_source == "http" and last_update_ts is not None:
                http_override_last_ts = last_update_ts
                http_override_active = (
                    (now - last_update_ts).total_seconds()
                    <= max(1, self._settings.http_override_active_seconds)
                )

            apply_status = latest_event.get("apply_status")
            error_text = latest_event.get("error_text")
            if apply_status in {"rejected", "failed"} and isinstance(error_text, str) and error_text.strip():
                event_error = error_text

        final_error = error
        if (missing or not valid) and event_error:
            final_error = event_error

        return SetupFieldResponse(
            field_id=field.field_id,
            group=field.group,  # type: ignore[arg-type]
            label=field.label,
            required=effective_required,
            value_type=field.value_type,  # type: ignore[arg-type]
            unit=field.unit,
            options=options,
            current_value=value,
            valid=valid,
            missing=missing,
            dirty=False,
            last_source=last_source,  # type: ignore[arg-type]
            last_update_ts=last_update_ts,
            http_path_template=field.http_path_template,
            http_override_active=http_override_active,
            http_override_last_ts=http_override_last_ts,
            advanced=field.advanced,
            item_key=field.item_key,
            category_id=field.category_id,
            error=final_error,
        )

    def _resolve_options(self, field: SetupFieldDef, *, payload: dict[str, Any] | None = None) -> list[str]:
        if field.options_key is None:
            return []
        if field.options_key == "devices.battery_ids":
            batteries = _get_payload_path(payload or {}, ("devices", "batteries"))
            if not isinstance(batteries, list):
                return []
            options: list[str] = []
            seen: set[str] = set()
            for battery in batteries:
                if not isinstance(battery, dict):
                    continue
                battery_id = _string_or_none_from_value(battery.get("device_id"))
                if battery_id is None:
                    continue
                lowered = battery_id.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                options.append(battery_id)
            return options
        catalog = self._parameter_catalog_service.build_catalog()
        provider_options = catalog.get("provider_options", {})
        if field.options_key in provider_options and isinstance(provider_options[field.options_key], list):
            return [str(item) for item in provider_options[field.options_key]]
        if field.options_key == "elecprice.energycharts.bidding_zone":
            zones = catalog.get("bidding_zone_options", [])
            if isinstance(zones, list):
                return [str(item) for item in zones]
        return []

    def _normalize_field_value(
        self,
        field: SetupFieldDef,
        raw_value: Any,
        *,
        required: bool,
        payload: dict[str, Any] | None = None,
    ) -> tuple[Any, str | None]:
        if _is_home_appliance_window_start_field(field.field_id):
            if raw_value is None:
                return None, "start_time is required"
            normalized, error = _normalize_home_appliance_window_start(raw_value)
            if error is not None:
                return None, error
            if required and normalized is None:
                return None, "start_time is required"
            return normalized, None

        if _is_home_appliance_window_duration_field(field.field_id):
            normalized_duration, error = _normalize_home_appliance_window_duration(raw_value)
            if error is not None:
                return None, error
            if required and normalized_duration is None:
                return None, "duration_h is required"
            return normalized_duration, None

        if field.value_type == "string":
            if raw_value is None:
                return None, "value is required"
            normalized = str(raw_value).strip()
            if required and normalized == "":
                return None, "value is required"
            return normalized, None
        if field.value_type == "number":
            try:
                number_value = float(raw_value)
            except (TypeError, ValueError):
                return None, "number expected"
            if field.minimum is not None and number_value < field.minimum:
                return None, f"value must be >= {field.minimum}"
            if field.maximum is not None and number_value > field.maximum:
                return None, f"value must be <= {field.maximum}"
            return number_value, None
        if field.value_type == "select":
            normalized = str(raw_value).strip()
            options = self._resolve_options(field, payload=payload)
            if options and normalized not in options:
                return None, "value not in allowed options"
            if required and normalized == "":
                return None, "value is required"
            return normalized, None
        if field.value_type == "string_list":
            if isinstance(raw_value, list):
                values = [str(item).strip() for item in raw_value if str(item).strip() != ""]
            else:
                values = [token.strip() for token in str(raw_value).split(",") if token.strip() != ""]
            unique_values: list[str] = []
            seen = set()
            for value in values:
                if value in seen:
                    continue
                seen.add(value)
                unique_values.append(value)
            if required and not unique_values:
                return None, "list must not be empty"
            return unique_values, None
        return None, "unsupported field value type"

    def _validate_value(
        self,
        field: SetupFieldDef,
        value: Any,
        options: list[str],
        *,
        required: bool,
    ) -> tuple[bool, str | None]:
        if _is_home_appliance_window_start_field(field.field_id):
            if _is_missing(value):
                return (not required), ("missing value" if required else None)
            _, error = _normalize_home_appliance_window_start(value)
            if error is not None:
                return False, error
            return True, None

        if _is_home_appliance_window_duration_field(field.field_id):
            if _is_missing(value):
                return (not required), ("missing value" if required else None)
            _, error = _normalize_home_appliance_window_duration(value)
            if error is not None:
                return False, error
            return True, None

        if _is_missing(value):
            return (not required), ("missing value" if required else None)
        if field.value_type == "number":
            try:
                number_value = float(value)
            except (TypeError, ValueError):
                return False, "number expected"
            if field.minimum is not None and number_value < field.minimum:
                return False, f"value < {field.minimum}"
            if field.maximum is not None and number_value > field.maximum:
                return False, f"value > {field.maximum}"
            return True, None
        if field.value_type == "string":
            text_value = str(value).strip()
            if required and text_value == "":
                return False, "value required"
            return True, None
        if field.value_type == "select":
            text_value = str(value).strip()
            if required and text_value == "":
                return False, "value required"
            if options and text_value not in options:
                return False, "invalid option"
            return True, None
        if field.value_type == "string_list":
            values = value if isinstance(value, list) else [value]
            tokens = [str(item).strip() for item in values if str(item).strip() != ""]
            if required and not tokens:
                return False, "list required"
            return True, None
        return False, "unsupported type"

    def _load_current_payload(self, db: Session) -> dict[str, Any]:
        active = self._ensure_active_profile(db)
        if active is None:
            return {}
        draft = get_current_draft_revision(db, profile_id=active.id)
        if draft is None or not isinstance(draft.payload_json, dict):
            return {}
        return copy.deepcopy(draft.payload_json)

    def _ensure_active_profile(self, db: Session):
        active = get_active_parameter_profile(db)
        if active is not None:
            return active

        self._parameter_profile_service.ensure_bootstrap_profile(db)
        active = get_active_parameter_profile(db)
        if active is not None:
            return active

        profiles = list_parameter_profiles(db)
        if not profiles:
            return None

        try:
            set_active_parameter_profile(db, profiles[0].id)
        except Exception:
            return profiles[0]
        return get_active_parameter_profile(db) or profiles[0]

    def _is_effectively_required(self, field: SetupFieldDef, payload: dict[str, Any]) -> bool:
        if not field.required:
            return False

        if field.field_id == "param.feedintariff.provider_settings.FeedInTariffFixed.feed_in_tariff_kwh":
            provider = _get_payload_path(payload, ("feedintariff", "provider"))
            return provider == "FeedInTariffFixed"

        if field.field_id == self._FIXED_IMPORT_PRICE_FIELD_ID:
            provider = _get_payload_path(payload, ("elecprice", "provider"))
            return provider == "ElecPriceImport"

        if field.field_id == "param.load.provider_settings.LoadAkkudoktor.loadakkudoktor_year_energy_kwh":
            provider = _get_payload_path(payload, ("load", "provider"))
            return provider in {"LoadAkkudoktor", "LoadAkkudoktorAdjusted"}

        return True

    def _load_signal_state(self, db: Session, keys: list[str]) -> dict[str, dict[str, Any]]:
        rows = list_latest_by_signal_keys(db, signal_keys=keys)
        return {
            str(row.get("signal_key")): row
            for row in rows
            if isinstance(row.get("signal_key"), str)
        }

    def _load_signal_values_for_export(self, db: Session) -> dict[str, Any]:
        all_signal_keys = [
            field.signal_key
            for field in self._field_defs(self._load_current_payload(db))
            if field.signal_key is not None
        ]
        rows = list_latest_by_signal_keys(db, signal_keys=[key for key in all_signal_keys if key is not None])
        signal_values: dict[str, Any] = {}
        for row in rows:
            key = row.get("signal_key")
            if not isinstance(key, str):
                continue
            raw_value = _signal_row_value(row)
            field_id = _signal_field_id_from_key(key)
            export_key = _signal_export_key_from_internal(key)
            export_value = (
                _to_ui_numeric_if_needed(
                    field_id=field_id,
                    value=raw_value,
                    factors=self._UI_TO_STORAGE_FACTORS,
                    pattern_factors=self._UI_TO_STORAGE_FACTOR_PATTERNS,
                )
                if field_id is not None
                else raw_value
            )
            signal_values[export_key] = export_value
        return signal_values

    def _ingest_signal(self, *, signal_key: str, value: Any, source: str, ts: datetime) -> None:
        with self._session_factory() as signal_db:
            ingest_signal_measurement(
                signal_db,
                signal_key=signal_key,
                label=signal_key,
                value_type=infer_value_type(value),
                canonical_unit=_signal_unit(signal_key),
                value=value,
                ts=ts,
                quality_status="ok",
                source_type="http_input" if source == "http" else "derived",
                tags_json={
                    "source": source,
                    "namespace": "setup_signal",
                },
            )

        if self._emr_pipeline_service is not None:
            self._emr_pipeline_service.process_signal_value(
                signal_key=signal_key,
                value=value,
                source_ts=ts,
                source=source,
                raw_payload=_raw_value_text(value),
            )


def _profile_source_for_updates(sources: list[str]) -> str:
    if any(source == "import" for source in sources):
        return "import"
    if any(source == "http" for source in sources):
        return "dynamic_input"
    return "manual"


def _signal_unit(signal_key: str) -> str | None:
    if signal_key.endswith("_w"):
        return "W"
    if signal_key.endswith("_pct") or signal_key.endswith("_percentage"):
        return "%"
    return None


def _get_payload_path(payload: dict[str, Any], path: tuple[str | int, ...]) -> Any:
    current: Any = payload
    for token in path:
        if isinstance(token, int):
            if not isinstance(current, list) or token < 0 or token >= len(current):
                return None
            current = current[token]
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(token)
    return current


def _payload_has_path(payload: dict[str, Any], path: tuple[str | int, ...]) -> bool:
    current: Any = payload
    for token in path:
        if isinstance(token, int):
            if not isinstance(current, list) or token < 0 or token >= len(current):
                return False
            current = current[token]
            continue
        if not isinstance(current, dict) or token not in current:
            return False
        current = current[token]
    return True


def _set_payload_path(payload: dict[str, Any], path: tuple[str | int, ...], value: Any) -> None:
    current: Any = payload
    for index, token in enumerate(path):
        is_last = index == len(path) - 1
        next_token = None if is_last else path[index + 1]

        if isinstance(token, str):
            if not isinstance(current, dict):
                raise ValueError("Invalid payload path")
            if is_last:
                current[token] = value
                return
            child = current.get(token)
            if isinstance(next_token, int):
                if not isinstance(child, list):
                    child = []
                    current[token] = child
            else:
                if not isinstance(child, dict):
                    child = {}
                    current[token] = child
            current = child
            continue

        if not isinstance(current, list):
            raise ValueError("Invalid payload list path")
        while len(current) <= token:
            current.append({})
        if is_last:
            current[token] = value
            return
        child = current[token]
        if isinstance(next_token, int):
            if not isinstance(child, list):
                child = []
                current[token] = child
        else:
            if not isinstance(child, dict):
                child = {}
                current[token] = child
        current = child


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, list):
        return len(value) == 0
    return False


def _signal_row_value(row: dict[str, Any]) -> Any:
    if row.get("last_value_num") is not None:
        return float(row["last_value_num"])
    if row.get("last_value_text") is not None:
        return row["last_value_text"]
    if row.get("last_value_bool") is not None:
        return bool(row["last_value_bool"])
    return row.get("last_value_json")


def _raw_value_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _extract_fixed_elecprice_import_value_eur_per_kwh(raw_value: Any) -> float | None:
    payload = _coerce_json_dict(raw_value)
    if payload is None:
        return None

    wh_series = payload.get("elecprice_marketprice_wh")
    if isinstance(wh_series, list) and wh_series:
        first = _coerce_float(wh_series[0])
        if first is not None:
            return first * 1000.0

    kwh_series = payload.get("elecprice_marketprice_kwh")
    if isinstance(kwh_series, list) and kwh_series:
        first = _coerce_float(kwh_series[0])
        if first is not None:
            return first

    direct_value = _coerce_float(payload.get("value"))
    if direct_value is not None:
        return direct_value

    return None


def _build_fixed_elecprice_import_json(*, payload: dict[str, Any], eur_per_kwh: float) -> str:
    prediction_hours_value = _get_payload_path(payload, ("prediction", "hours"))
    prediction_hours = _coerce_float(prediction_hours_value) or 48.0
    slot_count = max(1, int(round(prediction_hours * 4.0)))
    value_wh = eur_per_kwh / 1000.0
    series = [value_wh] * slot_count
    ct_per_kwh = eur_per_kwh * 100.0
    import_payload = {
        "elecprice_marketprice_wh": series,
        "note": f"constant {ct_per_kwh} ct/kWh expanded to {int(round(prediction_hours))}h x 15min",
    }
    return json.dumps(import_payload, ensure_ascii=True, separators=(",", ":"))


def _coerce_json_dict(raw_value: Any) -> dict[str, Any] | None:
    if isinstance(raw_value, dict):
        return raw_value
    if not isinstance(raw_value, str):
        return None
    text = raw_value.strip()
    if text == "":
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_utc_if_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _to_utc(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return _to_utc(datetime.fromisoformat(raw))
        except ValueError:
            return None
    return None


def _coerce_datetime(value: str | int | float | None) -> datetime | None:
    if value is None:
        return None
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
        return _to_utc(datetime.fromisoformat(raw))
    return None


def _epoch_to_datetime(value: float) -> datetime:
    seconds = value / 1000.0 if abs(value) > 1_000_000_000_000 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _battery_selector(payload: dict[str, Any]) -> str:
    device_id = _get_payload_path(payload, ("devices", "batteries", 0, "device_id"))
    if isinstance(device_id, str) and device_id.strip():
        return device_id.strip()
    return "0"


def _inverter_selector(payload: dict[str, Any]) -> str:
    device_id = _get_payload_path(payload, ("devices", "inverters", 0, "device_id"))
    if isinstance(device_id, str) and device_id.strip():
        return device_id.strip()
    return "0"


def _string_or_none(field: SetupFieldResponse | None) -> str | None:
    if field is None:
        return None
    value = field.current_value
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _string_list_or_empty(field: SetupFieldResponse | None) -> list[str]:
    if field is None:
        return []
    value = field.current_value
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip() != ""]
    if isinstance(value, str):
        return [token.strip() for token in value.split(",") if token.strip() != ""]
    return []


def _signal_field_id_from_key(signal_key: str) -> str | None:
    field_id, _ = _resolve_signal_field_id_and_input_scale(signal_key)
    return field_id


def _resolve_signal_field_id_and_input_scale(signal_key: str) -> tuple[str | None, float]:
    key = signal_key.strip().lower()
    mapping: dict[str, tuple[str, float]] = {
        "house_load_kw": ("signal.house_load_w", 1.0),
        "house_load_w": ("signal.house_load_w", 0.001),
        "pv_power_kw": ("signal.pv_power_w", 1.0),
        "pv_power_w": ("signal.pv_power_w", 0.001),
        "grid_import_kw": ("signal.grid_import_w", 1.0),
        "grid_import_w": ("signal.grid_import_w", 0.001),
        "grid_export_kw": ("signal.grid_export_w", 1.0),
        "grid_export_w": ("signal.grid_export_w", 0.001),
        "battery_power_kw": ("signal.battery_power_w", 1.0),
        "battery_power_w": ("signal.battery_power_w", 0.001),
        "battery_soc_pct": ("signal.battery_soc_pct", 1.0),
        "battery_soc_percent": ("signal.battery_soc_pct", 1.0),
    }
    resolved = mapping.get(key)
    if resolved is None:
        return None, 1.0
    return resolved


def _signal_export_key_from_internal(signal_key: str) -> str:
    mapping = {
        "house_load_w": "house_load_kw",
        "pv_power_w": "pv_power_kw",
        "grid_import_w": "grid_import_kw",
        "grid_export_w": "grid_export_kw",
        "battery_power_w": "battery_power_kw",
        "battery_soc_pct": "battery_soc_pct",
    }
    return mapping.get(signal_key, signal_key)


def _resolve_http_set_field_id(*, raw_path: str, payload: dict[str, Any], value: Any) -> tuple[str, Any]:
    path = raw_path.strip("/")
    if path == "":
        raise ValueError("path is required")
    if path.startswith("signal/"):
        signal_key = path[len("signal/") :]
        normalized_key, storage_value = _normalize_signal_http_key(signal_key=signal_key, value=value)
        field_id = _signal_field_id_from_key(normalized_key)
        if field_id is None:
            raise ValueError(f"unsupported signal key '{signal_key}'")
        ui_value = _to_ui_numeric_if_needed(
            field_id=field_id,
            value=storage_value,
            factors=SetupFieldService._UI_TO_STORAGE_FACTORS,
            pattern_factors=SetupFieldService._UI_TO_STORAGE_FACTOR_PATTERNS,
        )
        return field_id, ui_value

    if not path.startswith("param/"):
        raise ValueError("path must start with signal/ or param/")
    param_path = path[len("param/") :]
    field_id, input_scale_to_ui = _param_path_to_field_id(param_path=param_path, payload=payload)
    value_for_patch = value
    if input_scale_to_ui != 1.0:
        scaled_value, error = _scale_numeric_value(value=value, factor=input_scale_to_ui)
        if error is not None:
            raise ValueError(error)
        value_for_patch = scaled_value
    return field_id, value_for_patch


def _normalize_signal_http_key(*, signal_key: str, value: Any) -> tuple[str, Any]:
    key = signal_key.strip().lower()
    key_map = {
        "house_load_w": ("house_load_w", 1.0),
        "house_load_kw": ("house_load_w", 1000.0),
        "pv_power_w": ("pv_power_w", 1.0),
        "pv_power_kw": ("pv_power_w", 1000.0),
        "grid_import_w": ("grid_import_w", 1.0),
        "grid_import_kw": ("grid_import_w", 1000.0),
        "grid_export_w": ("grid_export_w", 1.0),
        "grid_export_kw": ("grid_export_w", 1000.0),
        "grid_power_consumption_kw": ("grid_import_w", 1000.0),
        "battery_power_w": ("battery_power_w", 1.0),
        "battery_power_kw": ("battery_power_w", 1000.0),
        "battery_power_charge_w": ("battery_power_w", 1.0),
        "battery_power_charge_kw": ("battery_power_w", 1000.0),
        "battery_soc_pct": ("battery_soc_pct", 1.0),
        "battery_soc_percent": ("battery_soc_pct", 1.0),
    }
    if key not in key_map:
        raise ValueError(f"unsupported signal key '{signal_key}'")
    canonical_key, multiplier = key_map[key]
    numeric = float(value)
    return canonical_key, numeric * multiplier


def _param_path_to_field_id(*, param_path: str, payload: dict[str, Any]) -> tuple[str, float]:
    raw = param_path.strip("/")
    if raw == "":
        raise ValueError("param path is empty")
    parts = raw.split("/")

    if raw == "optimization/horizon_hours":
        if not _payload_has_path(payload, ("optimization", "horizon_hours")):
            raise ValueError("optimization.horizon_hours is not available in current payload")
        return "param.optimization.horizon_hours", 1.0
    if raw == "optimization/hours":
        if not _payload_has_path(payload, ("optimization", "hours")):
            raise ValueError("optimization.hours is not available in current payload")
        return "param.optimization.hours", 1.0

    if len(parts) == 4 and parts[:2] == ["pvforecast", "planes"]:
        selector = parts[2]
        field_name = parts[3]
        index = _resolve_plane_index(payload, selector)
        field_to_ui_scale = {
            "peakpower": 1.0,
            "surface_azimuth": 1.0,
            "surface_tilt": 1.0,
            "inverter_paco_kw": 1.0,
            "inverter_paco": 0.001,  # legacy compatibility: W -> kW
            "loss": 1.0,
            "trackingtype": 1.0,
        }
        if field_name not in field_to_ui_scale:
            raise ValueError("Unsupported pv plane field")
        canonical_field_name = {
            "inverter_paco_kw": "inverter_paco",
            "inverter_paco": "inverter_paco",
        }.get(field_name, field_name)
        return f"param.pvforecast.planes.{index}.{canonical_field_name}", field_to_ui_scale[field_name]

    static_paths: dict[str, tuple[str, float]] = {
        "general/latitude": ("param.general.latitude", 1.0),
        "general/longitude": ("param.general.longitude", 1.0),
        "pvforecast/provider": ("param.pvforecast.provider", 1.0),
        "elecprice/provider": ("param.elecprice.provider", 1.0),
        "elecprice/elecpriceimport/import_json/value_ct_per_kwh": ("param.elecprice.elecpriceimport.import_json.value", 1.0),
        # Legacy compatibility: old path used EUR/kWh.
        "elecprice/elecpriceimport/import_json/value": ("param.elecprice.elecpriceimport.import_json.value", 100.0),
        "elecprice/charges_ct_per_kwh": ("param.elecprice.charges_kwh", 1.0),
        # Legacy compatibility: old path used EUR/kWh.
        "elecprice/charges_kwh": ("param.elecprice.charges_kwh", 100.0),
        "elecprice/vat_rate": ("param.elecprice.vat_rate", 1.0),
        "elecprice/energycharts/bidding_zone": ("param.elecprice.energycharts.bidding_zone", 1.0),
        "feedintariff/provider": ("param.feedintariff.provider", 1.0),
        "feedintariff/provider_settings/FeedInTariffFixed/feed_in_tariff_ct_per_kwh": (
            "param.feedintariff.provider_settings.FeedInTariffFixed.feed_in_tariff_kwh",
            1.0,
        ),
        # Legacy compatibility: old path used EUR/kWh.
        "feedintariff/provider_settings/FeedInTariffFixed/feed_in_tariff_kwh": (
            "param.feedintariff.provider_settings.FeedInTariffFixed.feed_in_tariff_kwh",
            100.0,
        ),
        "load/provider": ("param.load.provider", 1.0),
        "load/provider_settings/LoadAkkudoktor/loadakkudoktor_year_energy_kwh": (
            "param.load.provider_settings.LoadAkkudoktor.loadakkudoktor_year_energy_kwh",
            1.0,
        ),
        "prediction/hours": ("param.prediction.hours", 1.0),
        "prediction/historic_hours": ("param.prediction.historic_hours", 1.0),
        "measurement/keys": ("param.measurement.keys", 1.0),
        "measurement/load_emr_keys": ("param.measurement.load_emr_keys", 1.0),
        "measurement/grid_import_emr_keys": ("param.measurement.grid_import_emr_keys", 1.0),
        "measurement/grid_export_emr_keys": ("param.measurement.grid_export_emr_keys", 1.0),
        "measurement/pv_production_emr_keys": ("param.measurement.pv_production_emr_keys", 1.0),
    }
    if raw in static_paths:
        return static_paths[raw]

    if len(parts) == 4 and parts[:2] == ["devices", "batteries"]:
        selector = parts[2]
        field_name = parts[3]
        index = _resolve_device_index(payload, "batteries", selector)
        if index != 0:
            raise ValueError("Only battery #1 is supported in the unified setup view")
        field_to_ui_scale = {
            "device_id": 1.0,
            "capacity_kwh": 1.0,
            "capacity_wh": 0.001,  # legacy compatibility
            "min_charge_power_kw": 1.0,
            "min_charge_power_w": 0.001,  # legacy compatibility
            "max_charge_power_kw": 1.0,
            "max_charge_power_w": 0.001,  # legacy compatibility
            "min_soc_percentage": 1.0,
            "max_soc_percentage": 1.0,
        }
        if field_name not in field_to_ui_scale:
            raise ValueError("Unsupported battery field")
        canonical_field_name = {
            "capacity_kwh": "capacity_wh",
            "capacity_wh": "capacity_wh",
            "min_charge_power_kw": "min_charge_power_w",
            "min_charge_power_w": "min_charge_power_w",
            "max_charge_power_kw": "max_charge_power_w",
            "max_charge_power_w": "max_charge_power_w",
        }.get(field_name, field_name)
        if canonical_field_name not in {
            "device_id",
            "capacity_wh",
            "min_charge_power_w",
            "max_charge_power_w",
            "min_soc_percentage",
            "max_soc_percentage",
        }:
            raise ValueError("Unsupported battery field")
        return f"param.devices.batteries.{index}.{canonical_field_name}", field_to_ui_scale[field_name]

    if len(parts) == 4 and parts[:2] == ["devices", "inverters"]:
        selector = parts[2]
        field_name = parts[3]
        index = _resolve_device_index(payload, "inverters", selector)
        if index != 0:
            raise ValueError("Only inverter #1 is supported in the unified setup view")
        field_to_ui_scale = {
            "device_id": 1.0,
            "max_power_kw": 1.0,
            "max_power_w": 0.001,  # legacy compatibility
            "battery_id": 1.0,
        }
        if field_name not in field_to_ui_scale:
            raise ValueError("Unsupported inverter field")
        canonical_field_name = "max_power_w" if field_name in {"max_power_kw", "max_power_w"} else field_name
        if canonical_field_name not in {"device_id", "max_power_w", "battery_id"}:
            raise ValueError("Unsupported inverter field")
        return f"param.devices.inverters.{index}.{canonical_field_name}", field_to_ui_scale[field_name]

    if len(parts) == 4 and parts[:2] == ["devices", "electric_vehicles"]:
        selector = parts[2]
        field_name = parts[3]
        index = _resolve_device_index(payload, "electric_vehicles", selector)
        field_to_ui_scale = {
            "device_id": 1.0,
            "capacity_kwh": 1.0,
            "capacity_wh": 0.001,  # legacy compatibility
            "min_charge_power_kw": 1.0,
            "min_charge_power_w": 0.001,  # legacy compatibility
            "max_charge_power_kw": 1.0,
            "max_charge_power_w": 0.001,  # legacy compatibility
            "min_soc_percentage": 1.0,
            "max_soc_percentage": 1.0,
            "charging_efficiency": 1.0,
            "discharging_efficiency": 1.0,
        }
        if field_name not in field_to_ui_scale:
            raise ValueError("Unsupported electric_vehicle field")
        canonical_field_name = {
            "capacity_kwh": "capacity_wh",
            "capacity_wh": "capacity_wh",
            "min_charge_power_kw": "min_charge_power_w",
            "min_charge_power_w": "min_charge_power_w",
            "max_charge_power_kw": "max_charge_power_w",
            "max_charge_power_w": "max_charge_power_w",
        }.get(field_name, field_name)
        return f"param.devices.electric_vehicles.{index}.{canonical_field_name}", field_to_ui_scale[field_name]

    if len(parts) == 4 and parts[:2] == ["devices", "home_appliances"]:
        selector = parts[2]
        field_name = parts[3]
        index = _resolve_device_index(payload, "home_appliances", selector)
        field_to_ui_scale = {
            "device_id": 1.0,
            "consumption_kwh": 1.0,
            "consumption_wh": 0.001,  # legacy compatibility
            "duration_h": 1.0,
            "measurement_keys": 1.0,
        }
        if field_name not in field_to_ui_scale:
            raise ValueError("Unsupported home_appliance field")
        if field_name == "measurement_keys" and not _payload_has_path(
            payload,
            ("devices", "home_appliances", index, "measurement_keys"),
        ):
            raise ValueError("measurement_keys is not available for this home_appliance")
        canonical_field_name = {
            "consumption_kwh": "consumption_wh",
            "consumption_wh": "consumption_wh",
        }.get(field_name, field_name)
        return f"param.devices.home_appliances.{index}.{canonical_field_name}", field_to_ui_scale[field_name]

    if len(parts) == 7 and parts[:2] == ["devices", "home_appliances"] and parts[3:5] == ["time_windows", "windows"]:
        selector = parts[2]
        appliance_index = _resolve_device_index(payload, "home_appliances", selector)
        window_selector = parts[5]
        window_index = _resolve_home_appliance_window_index(payload, appliance_index, window_selector)
        field_name = parts[6]
        field_to_ui_scale = {
            "start_time": 1.0,
            "duration_h": 1.0,
            "duration": 1.0,  # legacy compatibility
        }
        if field_name not in field_to_ui_scale:
            raise ValueError("Unsupported home_appliance_window field")
        canonical_field_name = "duration_h" if field_name == "duration" else field_name
        return (
            f"param.devices.home_appliances.{appliance_index}.time_windows.windows.{window_index}.{canonical_field_name}",
            field_to_ui_scale[field_name],
        )

    raise ValueError(f"unsupported param path '{param_path}'")


def _scale_numeric_value(*, value: Any, factor: float) -> tuple[Any, str | None]:
    if isinstance(value, bool):
        return None, "number expected"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None, "number expected"
    return numeric * factor, None


def _to_storage_numeric_if_needed(
    *,
    field_id: str,
    value: Any,
    factors: dict[str, float],
    pattern_factors: tuple[tuple[re.Pattern[str], float], ...] = (),
) -> Any:
    factor = _unit_factor_for_field_id(field_id=field_id, factors=factors, pattern_factors=pattern_factors)
    if factor is None:
        return value
    scaled, error = _scale_numeric_value(value=value, factor=factor)
    if error is not None:
        return value
    return scaled


def _to_ui_numeric_if_needed(
    *,
    field_id: str,
    value: Any,
    factors: dict[str, float],
    pattern_factors: tuple[tuple[re.Pattern[str], float], ...] = (),
) -> Any:
    factor = _unit_factor_for_field_id(field_id=field_id, factors=factors, pattern_factors=pattern_factors)
    if factor is None:
        return value
    scaled, error = _scale_numeric_value(value=value, factor=1.0 / factor)
    if error is not None:
        return value
    return scaled


def _unit_factor_for_field_id(
    *,
    field_id: str,
    factors: dict[str, float],
    pattern_factors: tuple[tuple[re.Pattern[str], float], ...],
) -> float | None:
    exact = factors.get(field_id)
    if exact is not None:
        return exact
    for pattern, factor in pattern_factors:
        if pattern.match(field_id):
            return factor
    return None


def _resolve_device_index(payload: dict[str, Any], collection: str, selector: str) -> int:
    if selector.isdigit():
        return int(selector)
    items = _get_payload_path(payload, ("devices", collection))
    if not isinstance(items, list):
        raise ValueError(f"{collection} not configured")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        device_id = item.get("device_id")
        if isinstance(device_id, str) and device_id == selector:
            return index
    raise ValueError(f"{collection} selector '{selector}' not found")


def _resolve_plane_index(payload: dict[str, Any], selector: str) -> int:
    if not selector.isdigit():
        raise ValueError("pv plane selector must be numeric")
    index = int(selector)
    planes = _get_payload_path(payload, ("pvforecast", "planes"))
    if not isinstance(planes, list):
        raise ValueError("pvforecast.planes not configured")
    if index < 0 or index >= len(planes):
        raise ValueError("pv plane selector not found")
    return index


def _resolve_home_appliance_window_index(payload: dict[str, Any], appliance_index: int, selector: str) -> int:
    if not selector.isdigit():
        raise ValueError("window selector must be numeric")
    window_index = int(selector)
    appliances = _get_payload_path(payload, ("devices", "home_appliances"))
    if not isinstance(appliances, list) or appliance_index < 0 or appliance_index >= len(appliances):
        raise ValueError("home_appliances not configured")
    appliance = appliances[appliance_index]
    windows = _home_appliance_windows_from_item(appliance)
    if window_index < 0 or window_index >= len(windows):
        raise ValueError("window selector not found")
    return window_index


def _category_id_for_field(field_id: str, group: str) -> str:
    if field_id.startswith("param.general."):
        return "location_base"
    if field_id.startswith("param.pvforecast."):
        return "pv_forecast"
    if field_id.startswith("param.elecprice.") or field_id.startswith("param.feedintariff.") or field_id.startswith("param.load.") or field_id.startswith("param.prediction.") or field_id.startswith("param.optimization."):
        return "tariffs_load"
    if field_id.startswith("param.devices.batteries.") or field_id.startswith("param.devices.inverters."):
        return "storage_inverter"
    if field_id.startswith("param.devices.electric_vehicles."):
        return "electric_vehicles"
    if field_id.startswith("param.devices.home_appliances."):
        return "home_appliances"
    if field_id.startswith("param.measurement."):
        return "measurement_emr"
    if field_id.startswith("signal."):
        return "live_signals"
    if group == "mandatory":
        return "location_base"
    if group == "live":
        return "live_signals"
    return "tariffs_load"


def _item_key_for_field(field_id: str, category_id: str) -> str:
    match = re.match(r"^param\.pvforecast\.planes\.(\d+)\.", field_id)
    if match:
        return f"pv_plane:{int(match.group(1))}"
    match = re.match(r"^param\.devices\.batteries\.(\d+)\.", field_id)
    if match:
        return f"battery:{int(match.group(1))}"
    match = re.match(r"^param\.devices\.inverters\.(\d+)\.", field_id)
    if match:
        return f"inverter:{int(match.group(1))}"
    match = re.match(r"^param\.devices\.electric_vehicles\.(\d+)\.", field_id)
    if match:
        return f"electric_vehicle:{int(match.group(1))}"
    match = re.match(r"^param\.devices\.home_appliances\.(\d+)\.time_windows\.windows\.(\d+)\.", field_id)
    if match:
        return f"home_appliance:{int(match.group(1))}:window:{int(match.group(2))}"
    match = re.match(r"^param\.devices\.home_appliances\.(\d+)\.", field_id)
    if match:
        return f"home_appliance:{int(match.group(1))}"
    if category_id == "pv_forecast":
        return "pv_core"
    if category_id == "location_base":
        return "location:base"
    if category_id == "tariffs_load":
        return "tariffs:base"
    if category_id == "measurement_emr":
        return "measurement:base"
    if category_id == "live_signals":
        return "live:base"
    if category_id == "storage_inverter":
        return "storage:base"
    return f"{category_id}:base"


def _is_advanced_field(field_id: str) -> bool:
    if field_id.endswith(".loss") or field_id.endswith(".trackingtype"):
        return True
    if field_id in {
        "param.elecprice.charges_kwh",
        "param.elecprice.vat_rate",
        "param.elecprice.energycharts.bidding_zone",
        "param.prediction.hours",
        "param.prediction.historic_hours",
        "param.optimization.horizon_hours",
        "param.optimization.hours",
    }:
        return True
    if field_id.endswith(".charging_efficiency") or field_id.endswith(".discharging_efficiency"):
        return True
    if field_id.endswith(".measurement_keys") or re.search(r"\.home_appliances\.\d+\.duration_h$", field_id):
        return True
    return False


def _item_sort_key(item_key: str) -> tuple[int, int, int]:
    if item_key == "pv_core":
        return (0, 0, 0)
    plane_match = re.match(r"^pv_plane:(\d+)$", item_key)
    if plane_match:
        return (1, int(plane_match.group(1)), 0)
    battery_match = re.match(r"^battery:(\d+)$", item_key)
    if battery_match:
        return (2, int(battery_match.group(1)), 0)
    inverter_match = re.match(r"^inverter:(\d+)$", item_key)
    if inverter_match:
        return (3, int(inverter_match.group(1)), 0)
    ev_match = re.match(r"^electric_vehicle:(\d+)$", item_key)
    if ev_match:
        return (4, int(ev_match.group(1)), 0)
    app_match = re.match(r"^home_appliance:(\d+)$", item_key)
    if app_match:
        return (5, int(app_match.group(1)), 0)
    win_match = re.match(r"^home_appliance:(\d+):window:(\d+)$", item_key)
    if win_match:
        return (6, int(win_match.group(1)), int(win_match.group(2)))
    return (99, 0, 0)


def _entity_type_for_item_key(item_key: str) -> str | None:
    if item_key.startswith("pv_plane:"):
        return "pv_plane"
    if item_key.startswith("electric_vehicle:"):
        return "electric_vehicle"
    if re.match(r"^home_appliance:\d+$", item_key):
        return "home_appliance"
    if re.match(r"^home_appliance:\d+:window:\d+$", item_key):
        return "home_appliance_window"
    return None


def _parent_item_key_for_item_key(item_key: str) -> str | None:
    match = re.match(r"^home_appliance:(\d+):window:\d+$", item_key)
    if match:
        return f"home_appliance:{int(match.group(1))}"
    return None


def _is_item_deletable(*, item_key: str) -> bool:
    plane_match = re.match(r"^pv_plane:(\d+)$", item_key)
    if plane_match:
        return int(plane_match.group(1)) > 0
    if re.match(r"^electric_vehicle:\d+$", item_key):
        return True
    if re.match(r"^home_appliance:\d+$", item_key):
        return True
    if re.match(r"^home_appliance:\d+:window:\d+$", item_key):
        return True
    return False


def _item_label_for_item(*, item_key: str, fields: list[SetupFieldResponse]) -> str:
    if item_key == "pv_core":
        return "PV Forecast Basis"
    match = re.match(r"^pv_plane:(\d+)$", item_key)
    if match:
        return f"Plane #{int(match.group(1)) + 1}"
    match = re.match(r"^battery:(\d+)$", item_key)
    if match:
        return f"Batterie #{int(match.group(1)) + 1}"
    match = re.match(r"^inverter:(\d+)$", item_key)
    if match:
        return f"Inverter #{int(match.group(1)) + 1}"
    match = re.match(r"^electric_vehicle:(\d+)$", item_key)
    if match:
        device_id = _item_device_id(fields)
        return device_id or f"E-Auto #{int(match.group(1)) + 1}"
    match = re.match(r"^home_appliance:(\d+)$", item_key)
    if match:
        device_id = _item_device_id(fields)
        return device_id or f"Home-Appliance #{int(match.group(1)) + 1}"
    match = re.match(r"^home_appliance:(\d+):window:(\d+)$", item_key)
    if match:
        return f"Zeitfenster #{int(match.group(2)) + 1}"
    if item_key.startswith("location"):
        return "Standort"
    if item_key.startswith("measurement"):
        return "Measurement"
    if item_key.startswith("live"):
        return "Live-Signale"
    if item_key.startswith("tariffs"):
        return "Tarife & Last"
    return item_key


def _item_device_id(fields: list[SetupFieldResponse]) -> str | None:
    for field in fields:
        if field.field_id.endswith(".device_id"):
            value = _string_or_none_from_value(field.current_value)
            if value is not None:
                return value
    return None


def _ensure_payload_list(payload: dict[str, Any], path: tuple[str | int, ...]) -> list[Any]:
    existing = _get_payload_path(payload, path)
    if isinstance(existing, list):
        return existing
    _set_payload_path(payload, path, [])
    created = _get_payload_path(payload, path)
    if isinstance(created, list):
        return created
    raise ValueError("failed to initialize list payload path")


def _select_clone_source(
    *,
    items: list[Any],
    clone_from_item_key: str | None,
    expected_prefix: str,
) -> dict[str, Any] | None:
    if clone_from_item_key is None:
        return None
    index = _parse_item_index(clone_from_item_key, expected_prefix=expected_prefix)
    if index < 0 or index >= len(items):
        raise ValueError("clone_from_item_key not found")
    candidate = items[index]
    if not isinstance(candidate, dict):
        raise ValueError("clone_from_item_key not found")
    return candidate


def _parse_item_index(item_key: str, *, expected_prefix: str) -> int:
    match = re.match(rf"^{re.escape(expected_prefix)}:(\d+)$", item_key)
    if match is None:
        raise ValueError(f"item_key '{item_key}' does not match {expected_prefix}")
    return int(match.group(1))


def _parse_window_item_key(item_key: str) -> tuple[int, int]:
    match = re.match(r"^home_appliance:(\d+):window:(\d+)$", item_key)
    if match is None:
        raise ValueError("item_key does not match home_appliance window format")
    return int(match.group(1)), int(match.group(2))


def _default_pv_plane_template() -> dict[str, Any]:
    return {
        "peakpower": 5.0,
        "surface_azimuth": 180.0,
        "surface_tilt": 30.0,
        "inverter_paco": 5000.0,
        "loss": 14.0,
        "trackingtype": "0",
    }


def _default_ev_template() -> dict[str, Any]:
    return {
        "device_id": "",
        "capacity_wh": 60000.0,
        "min_charge_power_w": 1100.0,
        "max_charge_power_w": 11000.0,
        "min_soc_percentage": 0.0,
        "max_soc_percentage": 100.0,
        "charging_efficiency": 0.9,
        "discharging_efficiency": 1.0,
    }


def _ensure_vehicle_defaults(vehicle: dict[str, Any]) -> None:
    defaults = _default_ev_template()
    for key, default_value in defaults.items():
        if key not in vehicle or vehicle[key] is None:
            vehicle[key] = default_value


def _default_home_appliance_template() -> dict[str, Any]:
    return {
        "device_id": "",
        "consumption_wh": 2000.0,
        "duration_h": 2.0,
        "time_windows": {
            "windows": [
                _default_home_appliance_window(),
            ]
        },
    }


def _default_home_appliance_window() -> dict[str, Any]:
    return {
        "start_time": "08:00:00.000000 UTC",
        "duration": "2 hours",
        "day_of_week": None,
        "date": None,
        "locale": None,
    }


def _ensure_home_appliance_defaults(appliance: dict[str, Any]) -> None:
    defaults = _default_home_appliance_template()
    for key, default_value in defaults.items():
        if key not in appliance or appliance[key] is None:
            appliance[key] = copy.deepcopy(default_value)


def _ensure_home_appliance_windows(appliance: dict[str, Any]) -> list[dict[str, Any]]:
    time_windows = appliance.get("time_windows")
    if not isinstance(time_windows, dict):
        time_windows = {}
        appliance["time_windows"] = time_windows
    windows = time_windows.get("windows")
    if not isinstance(windows, list):
        windows = []
        time_windows["windows"] = windows
    for index, window in enumerate(windows):
        if isinstance(window, dict):
            continue
        windows[index] = copy.deepcopy(_default_home_appliance_window())
    return windows  # type: ignore[return-value]


def _home_appliance_windows_from_item(item: Any) -> list[dict[str, Any]]:
    if not isinstance(item, dict):
        return []
    time_windows = item.get("time_windows")
    if not isinstance(time_windows, dict):
        return []
    windows = time_windows.get("windows")
    if not isinstance(windows, list):
        return []
    return [window for window in windows if isinstance(window, dict)]


def _collect_device_ids(items: list[Any]) -> set[str]:
    existing: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        device_id = _string_or_none_from_value(item.get("device_id"))
        if device_id is None:
            continue
        existing.add(device_id.lower())
    return existing


def _assign_unique_device_id(*, preferred: str | None, existing_ids: set[str], prefix: str) -> str:
    if preferred is not None and preferred.lower() not in existing_ids:
        return preferred
    next_index = 1
    while True:
        candidate = f"{prefix}{next_index}"
        if candidate.lower() not in existing_ids:
            return candidate
        next_index += 1


def _device_selector_for_item(item: Any, index: int) -> str:
    if isinstance(item, dict):
        device_id = _string_or_none_from_value(item.get("device_id"))
        if device_id is not None:
            return device_id
    return str(index)


def _string_or_none_from_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    return text


def _battery_index_for_device_id_field(field_id: str) -> int | None:
    match = re.match(r"^param\.devices\.batteries\.(\d+)\.device_id$", field_id)
    if not match:
        return None
    return int(match.group(1))


def _sync_inverter_battery_references(
    *,
    payload: dict[str, Any],
    battery_index: int,
    old_battery_id: str | None,
    new_battery_id: str | None,
) -> list[int]:
    if new_battery_id is None:
        return []

    batteries = _get_payload_path(payload, ("devices", "batteries"))
    if not isinstance(batteries, list):
        return []
    if battery_index < 0 or battery_index >= len(batteries):
        return []

    inverters = _get_payload_path(payload, ("devices", "inverters"))
    if not isinstance(inverters, list):
        return []

    updated_indices: list[int] = []
    if len(batteries) == 1:
        for inverter_index, inverter in enumerate(inverters):
            if not isinstance(inverter, dict):
                continue
            current_battery_id = _string_or_none_from_value(inverter.get("battery_id"))
            if current_battery_id == new_battery_id:
                continue
            inverter["battery_id"] = new_battery_id
            updated_indices.append(inverter_index)
        return updated_indices

    for inverter_index, inverter in enumerate(inverters):
        if not isinstance(inverter, dict):
            continue
        current_battery_id = _string_or_none_from_value(inverter.get("battery_id"))
        if old_battery_id is None or current_battery_id != old_battery_id:
            continue
        inverter["battery_id"] = new_battery_id
        updated_indices.append(inverter_index)

    return updated_indices


def _is_home_appliance_window_start_field(field_id: str) -> bool:
    return bool(re.search(r"\.home_appliances\.\d+\.time_windows\.windows\.\d+\.start_time$", field_id))


def _is_home_appliance_window_duration_field(field_id: str) -> bool:
    return bool(re.search(r"\.home_appliances\.\d+\.time_windows\.windows\.\d+\.duration_h$", field_id))


def _normalize_home_appliance_window_start(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    text = str(value).strip()
    if text == "":
        return None, None
    hhmm_match = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if hhmm_match:
        hour = int(hhmm_match.group(1))
        minute = int(hhmm_match.group(2))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None, "start_time must be HH:MM"
        return f"{hour:02d}:{minute:02d}:00.000000 UTC", None

    eos_match = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?\s*(?:UTC)?$", text)
    if eos_match:
        hour = int(eos_match.group(1))
        minute = int(eos_match.group(2))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None, "start_time must be HH:MM"
        return f"{hour:02d}:{minute:02d}:00.000000 UTC", None

    return None, "start_time must be HH:MM"


def _home_appliance_window_start_to_ui(value: Any) -> str | None:
    normalized, error = _normalize_home_appliance_window_start(value)
    if error is not None or normalized is None:
        return _string_or_none_from_value(value)
    match = re.match(r"^(\d{2}):(\d{2}):", normalized)
    if match is None:
        return _string_or_none_from_value(value)
    return f"{match.group(1)}:{match.group(2)}"


def _normalize_home_appliance_window_duration(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None, None
        hours_match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*hours?$", text, re.IGNORECASE)
        if hours_match:
            numeric = float(hours_match.group(1))
            if numeric <= 0:
                return None, "duration_h must be > 0"
            return f"{numeric:g} hours", None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None, "duration_h must be numeric"
    if numeric <= 0:
        return None, "duration_h must be > 0"
    return f"{numeric:g} hours", None


def _home_appliance_window_duration_to_ui(value: Any) -> float | None:
    normalized, error = _normalize_home_appliance_window_duration(value)
    if error is not None or normalized is None:
        return _coerce_float(value)
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*hours?$", normalized, re.IGNORECASE)
    if match is None:
        return _coerce_float(value)
    return float(match.group(1))
