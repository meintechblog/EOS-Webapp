from __future__ import annotations

import copy
import json
from dataclasses import dataclass
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
    SetupExportResponse,
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


class SetupFieldService:
    _REQUIRED_SIGNAL_KEYS = ("house_load_w", "pv_power_w", "grid_import_w", "grid_export_w")
    _FIXED_IMPORT_PRICE_FIELD_ID = "param.elecprice.elecpriceimport.import_json.value"
    _UI_TO_STORAGE_FACTORS: dict[str, float] = {
        "param.pvforecast.planes.0.inverter_paco": 1000.0,  # kW -> W
        _FIXED_IMPORT_PRICE_FIELD_ID: 0.01,  # ct/kWh -> EUR/kWh
        "param.feedintariff.provider_settings.FeedInTariffFixed.feed_in_tariff_kwh": 0.01,  # ct/kWh -> EUR/kWh
        "param.devices.batteries.0.capacity_wh": 1000.0,  # kWh -> Wh
        "param.devices.batteries.0.min_charge_power_w": 1000.0,  # kW -> W
        "param.devices.batteries.0.max_charge_power_w": 1000.0,  # kW -> W
        "param.devices.inverters.0.max_power_w": 1000.0,  # kW -> W
        "param.elecprice.charges_kwh": 0.01,  # ct/kWh -> EUR/kWh
        "signal.house_load_w": 1000.0,  # kW -> W
        "signal.pv_power_w": 1000.0,  # kW -> W
        "signal.grid_import_w": 1000.0,  # kW -> W
        "signal.grid_export_w": 1000.0,  # kW -> W
        "signal.battery_power_w": 1000.0,  # kW -> W
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
            )

            if field.param_path is not None:
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
                    _set_payload_path(mutable_payload, field.param_path, storage_value)
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
                    )
                    if error is not None:
                        warnings.append(f"signal '{key}' ignored: {error}")
                        continue
                    storage_value = _to_storage_numeric_if_needed(
                        field_id=signal_def.field_id,
                        value=normalized,
                        factors=self._UI_TO_STORAGE_FACTORS,
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

    def _field_defs(self, payload: dict[str, Any]) -> list[SetupFieldDef]:
        catalog = self._parameter_catalog_service.build_catalog()
        provider_options = catalog.get("provider_options", {})
        bidding_zones = catalog.get("bidding_zone_options", [])

        battery_selector = _battery_selector(payload)
        inverter_selector = _inverter_selector(payload)

        return [
            SetupFieldDef("param.general.latitude", "mandatory", "Breitengrad", True, "number", "°", None, -90, 90, ("general", "latitude"), None, "/eos/set/param/general/latitude=<value>"),
            SetupFieldDef("param.general.longitude", "mandatory", "Längengrad", True, "number", "°", None, -180, 180, ("general", "longitude"), None, "/eos/set/param/general/longitude=<value>"),
            SetupFieldDef("param.pvforecast.provider", "mandatory", "PV Forecast Provider", True, "select", None, "pvforecast.provider", None, None, ("pvforecast", "provider"), None, "/eos/set/param/pvforecast/provider=<value>"),
            SetupFieldDef("param.pvforecast.planes.0.peakpower", "mandatory", "PV Plane #1 Peakpower", True, "number", "kW", None, 0, None, ("pvforecast", "planes", 0, "peakpower"), None, "/eos/set/param/pvforecast/planes/0/peakpower=<value>"),
            SetupFieldDef("param.pvforecast.planes.0.surface_azimuth", "mandatory", "PV Plane #1 Azimuth", True, "number", "°", None, 0, 360, ("pvforecast", "planes", 0, "surface_azimuth"), None, "/eos/set/param/pvforecast/planes/0/surface_azimuth=<value>"),
            SetupFieldDef("param.pvforecast.planes.0.surface_tilt", "mandatory", "PV Plane #1 Tilt", True, "number", "°", None, 0, 90, ("pvforecast", "planes", 0, "surface_tilt"), None, "/eos/set/param/pvforecast/planes/0/surface_tilt=<value>"),
            SetupFieldDef("param.pvforecast.planes.0.inverter_paco", "mandatory", "PV Plane #1 Inverter PACO", True, "number", "kW", None, 0, None, ("pvforecast", "planes", 0, "inverter_paco"), None, "/eos/set/param/pvforecast/planes/0/inverter_paco_kw=<value>"),
            SetupFieldDef("param.elecprice.provider", "mandatory", "Strompreis Provider", True, "select", None, "elecprice.provider", None, None, ("elecprice", "provider"), None, "/eos/set/param/elecprice/provider=<value>"),
            SetupFieldDef(self._FIXED_IMPORT_PRICE_FIELD_ID, "mandatory", "Bezugspreis fix (EOS Import-Serie)", True, "number", "ct/kWh", None, 0, None, ("elecprice", "elecpriceimport", "import_json"), None, "/eos/set/param/elecprice/elecpriceimport/import_json/value_ct_per_kwh=<value>"),
            SetupFieldDef("param.feedintariff.provider", "mandatory", "Einspeisevergütung Provider", True, "select", None, "feedintariff.provider", None, None, ("feedintariff", "provider"), None, "/eos/set/param/feedintariff/provider=<value>"),
            SetupFieldDef("param.feedintariff.provider_settings.FeedInTariffFixed.feed_in_tariff_kwh", "mandatory", "Fester Einspeisetarif", True, "number", "ct/kWh", None, 0, None, ("feedintariff", "provider_settings", "FeedInTariffFixed", "feed_in_tariff_kwh"), None, "/eos/set/param/feedintariff/provider_settings/FeedInTariffFixed/feed_in_tariff_ct_per_kwh=<value>"),
            SetupFieldDef("param.load.provider", "mandatory", "Last Provider", True, "select", None, "load.provider", None, None, ("load", "provider"), None, "/eos/set/param/load/provider=<value>"),
            SetupFieldDef("param.load.provider_settings.LoadAkkudoktor.loadakkudoktor_year_energy_kwh", "mandatory", "Jahresverbrauch", True, "number", "kWh/a", None, 0, None, ("load", "provider_settings", "LoadAkkudoktor", "loadakkudoktor_year_energy_kwh"), None, "/eos/set/param/load/provider_settings/LoadAkkudoktor/loadakkudoktor_year_energy_kwh=<value>"),
            SetupFieldDef("param.devices.batteries.0.device_id", "mandatory", "Batterie #1 Device ID", True, "string", None, None, None, None, ("devices", "batteries", 0, "device_id"), None, "/eos/set/param/devices/batteries/0/device_id=<value>"),
            SetupFieldDef("param.devices.batteries.0.capacity_wh", "mandatory", "Batterie #1 Kapazität", True, "number", "kWh", None, 0, None, ("devices", "batteries", 0, "capacity_wh"), None, f"/eos/set/param/devices/batteries/{battery_selector}/capacity_kwh=<value>"),
            SetupFieldDef("param.devices.batteries.0.min_charge_power_w", "mandatory", "Batterie #1 Min Charge", True, "number", "kW", None, 0, None, ("devices", "batteries", 0, "min_charge_power_w"), None, f"/eos/set/param/devices/batteries/{battery_selector}/min_charge_power_kw=<value>"),
            SetupFieldDef("param.devices.batteries.0.max_charge_power_w", "mandatory", "Batterie #1 Max Charge", True, "number", "kW", None, 0, None, ("devices", "batteries", 0, "max_charge_power_w"), None, f"/eos/set/param/devices/batteries/{battery_selector}/max_charge_power_kw=<value>"),
            SetupFieldDef("param.devices.batteries.0.min_soc_percentage", "mandatory", "Batterie #1 Min SOC", True, "number", "%", None, 0, 100, ("devices", "batteries", 0, "min_soc_percentage"), None, f"/eos/set/param/devices/batteries/{battery_selector}/min_soc_percentage=<value>"),
            SetupFieldDef("param.devices.batteries.0.max_soc_percentage", "mandatory", "Batterie #1 Max SOC", True, "number", "%", None, 0, 100, ("devices", "batteries", 0, "max_soc_percentage"), None, f"/eos/set/param/devices/batteries/{battery_selector}/max_soc_percentage=<value>"),
            SetupFieldDef("param.devices.inverters.0.device_id", "mandatory", "Inverter #1 Device ID", True, "string", None, None, None, None, ("devices", "inverters", 0, "device_id"), None, "/eos/set/param/devices/inverters/0/device_id=<value>"),
            SetupFieldDef("param.devices.inverters.0.max_power_w", "mandatory", "Inverter #1 Max Power", True, "number", "kW", None, 0, None, ("devices", "inverters", 0, "max_power_w"), None, f"/eos/set/param/devices/inverters/{inverter_selector}/max_power_kw=<value>"),
            SetupFieldDef("param.devices.inverters.0.battery_id", "mandatory", "Inverter #1 Battery ID", True, "string", None, None, None, None, ("devices", "inverters", 0, "battery_id"), None, f"/eos/set/param/devices/inverters/{inverter_selector}/battery_id=<value>"),
            SetupFieldDef("param.measurement.keys", "mandatory", "Measurement Keys", True, "string_list", None, None, None, None, ("measurement", "keys"), None, "/eos/set/param/measurement/keys=<csv>"),
            SetupFieldDef("param.measurement.load_emr_keys", "mandatory", "Load EMR Keys", True, "string_list", None, None, None, None, ("measurement", "load_emr_keys"), None, "/eos/set/param/measurement/load_emr_keys=<csv>"),
            SetupFieldDef("param.measurement.grid_import_emr_keys", "mandatory", "Grid Import EMR Keys", True, "string_list", None, None, None, None, ("measurement", "grid_import_emr_keys"), None, "/eos/set/param/measurement/grid_import_emr_keys=<csv>"),
            SetupFieldDef("param.measurement.grid_export_emr_keys", "mandatory", "Grid Export EMR Keys", True, "string_list", None, None, None, None, ("measurement", "grid_export_emr_keys"), None, "/eos/set/param/measurement/grid_export_emr_keys=<csv>"),
            SetupFieldDef("param.measurement.pv_production_emr_keys", "mandatory", "PV Production EMR Keys", True, "string_list", None, None, None, None, ("measurement", "pv_production_emr_keys"), None, "/eos/set/param/measurement/pv_production_emr_keys=<csv>"),
            SetupFieldDef("param.pvforecast.planes.0.loss", "optional", "PV Plane #1 Loss", False, "number", "%", None, 0, 100, ("pvforecast", "planes", 0, "loss"), None, "/eos/set/param/pvforecast/planes/0/loss=<value>"),
            SetupFieldDef("param.pvforecast.planes.0.trackingtype", "optional", "PV Plane #1 Trackingtype", False, "string", None, None, None, None, ("pvforecast", "planes", 0, "trackingtype"), None, "/eos/set/param/pvforecast/planes/0/trackingtype=<value>"),
            SetupFieldDef("param.elecprice.charges_kwh", "optional", "Strompreis Zuschlag", False, "number", "ct/kWh", None, 0, None, ("elecprice", "charges_kwh"), None, "/eos/set/param/elecprice/charges_ct_per_kwh=<value>"),
            SetupFieldDef("param.elecprice.vat_rate", "optional", "MwSt Faktor", False, "number", "x", None, 0, None, ("elecprice", "vat_rate"), None, "/eos/set/param/elecprice/vat_rate=<value>"),
            SetupFieldDef("param.elecprice.energycharts.bidding_zone", "optional", "EnergyCharts Zone", False, "select", None, "elecprice.energycharts.bidding_zone", None, None, ("elecprice", "energycharts", "bidding_zone"), None, "/eos/set/param/elecprice/energycharts/bidding_zone=<value>"),
            SetupFieldDef("param.prediction.hours", "optional", "Vorschau-Horizont", False, "number", "h", None, 1, 192, ("prediction", "hours"), None, "/eos/set/param/prediction/hours=<value>"),
            SetupFieldDef("param.prediction.historic_hours", "optional", "Prediction Historie", False, "number", "h", None, 1, 336, ("prediction", "historic_hours"), None, "/eos/set/param/prediction/historic_hours=<value>"),
            *(
                [
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
                    )
                ]
                if _payload_has_path(payload, ("optimization", "horizon_hours"))
                else []
            ),
            *(
                [
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
                    )
                ]
                if _payload_has_path(payload, ("optimization", "hours"))
                else []
            ),
            SetupFieldDef("signal.house_load_w", "live", "Hauslast", True, "number", "kW", None, 0, 100, None, "house_load_w", "/eos/set/signal/house_load_kw=<value>"),
            SetupFieldDef("signal.pv_power_w", "live", "PV Leistung", True, "number", "kW", None, 0, 100, None, "pv_power_w", "/eos/set/signal/pv_power_kw=<value>"),
            SetupFieldDef("signal.grid_import_w", "live", "Netzbezug", True, "number", "kW", None, 0, 100, None, "grid_import_w", "/eos/set/signal/grid_import_kw=<value>"),
            SetupFieldDef("signal.grid_export_w", "live", "Netzeinspeisung", True, "number", "kW", None, 0, 100, None, "grid_export_w", "/eos/set/signal/grid_export_kw=<value>"),
            SetupFieldDef("signal.battery_power_w", "live", "Batterieleistung", False, "number", "kW", None, -100, 100, None, "battery_power_w", "/eos/set/signal/battery_power_kw=<value>"),
            SetupFieldDef("signal.battery_soc_pct", "live", "Batterie-SOC", False, "number", "%", None, 0, 100, None, "battery_soc_pct", "/eos/set/signal/battery_soc_pct=<value>"),
        ]

    def _build_field_state(
        self,
        *,
        field: SetupFieldDef,
        payload: dict[str, Any],
        signal_state: dict[str, dict[str, Any]],
        latest_event: dict[str, Any] | None,
        now: datetime,
    ) -> SetupFieldResponse:
        options = self._resolve_options(field)
        value: Any = None
        signal_last_ts: datetime | None = None
        if field.param_path is not None:
            value = _get_payload_path(payload, field.param_path)
            if field.field_id == self._FIXED_IMPORT_PRICE_FIELD_ID:
                value = _extract_fixed_elecprice_import_value_eur_per_kwh(value)
        elif field.signal_key is not None:
            signal_row = signal_state.get(field.signal_key)
            if signal_row is not None:
                value = _signal_row_value(signal_row)
                signal_last_ts = _to_utc_if_datetime(signal_row.get("last_ts"))
        value = _to_ui_numeric_if_needed(
            field_id=field.field_id,
            value=value,
            factors=self._UI_TO_STORAGE_FACTORS,
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
            error=final_error,
        )

    def _resolve_options(self, field: SetupFieldDef) -> list[str]:
        if field.options_key is None:
            return []
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
    ) -> tuple[Any, str | None]:
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
            options = self._resolve_options(field)
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

    static_paths: dict[str, tuple[str, float]] = {
        "general/latitude": ("param.general.latitude", 1.0),
        "general/longitude": ("param.general.longitude", 1.0),
        "pvforecast/provider": ("param.pvforecast.provider", 1.0),
        "pvforecast/planes/0/peakpower": ("param.pvforecast.planes.0.peakpower", 1.0),
        "pvforecast/planes/0/surface_azimuth": ("param.pvforecast.planes.0.surface_azimuth", 1.0),
        "pvforecast/planes/0/surface_tilt": ("param.pvforecast.planes.0.surface_tilt", 1.0),
        "pvforecast/planes/0/inverter_paco_kw": ("param.pvforecast.planes.0.inverter_paco", 1.0),
        # Legacy compatibility: old path used W.
        "pvforecast/planes/0/inverter_paco": ("param.pvforecast.planes.0.inverter_paco", 0.001),
        "pvforecast/planes/0/loss": ("param.pvforecast.planes.0.loss", 1.0),
        "pvforecast/planes/0/trackingtype": ("param.pvforecast.planes.0.trackingtype", 1.0),
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

    raise ValueError(f"unsupported param path '{param_path}'")


def _scale_numeric_value(*, value: Any, factor: float) -> tuple[Any, str | None]:
    if isinstance(value, bool):
        return None, "number expected"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None, "number expected"
    return numeric * factor, None


def _to_storage_numeric_if_needed(*, field_id: str, value: Any, factors: dict[str, float]) -> Any:
    factor = factors.get(field_id)
    if factor is None:
        return value
    scaled, error = _scale_numeric_value(value=value, factor=factor)
    if error is not None:
        return value
    return scaled


def _to_ui_numeric_if_needed(*, field_id: str, value: Any, factors: dict[str, float]) -> Any:
    factor = factors.get(field_id)
    if factor is None:
        return value
    scaled, error = _scale_numeric_value(value=value, factor=1.0 / factor)
    if error is not None:
        return value
    return scaled


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
