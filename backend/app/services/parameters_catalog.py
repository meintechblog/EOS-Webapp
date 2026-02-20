from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.eos_client import EosClient


class ParameterCatalogService:
    def __init__(self, *, eos_client: EosClient):
        self._eos_client = eos_client

    def build_catalog(self) -> dict[str, Any]:
        config = self._safe_config()
        openapi = self._safe_openapi()

        provider_options = {
            "pvforecast.provider": _providers_for_section(config, "pvforecast"),
            "elecprice.provider": _providers_for_section(config, "elecprice"),
            "feedintariff.provider": _providers_for_section(config, "feedintariff"),
            "load.provider": _providers_for_section(config, "load"),
            "weather.provider": _providers_for_section(config, "weather"),
        }

        bidding_zone_options = _enum_from_openapi(openapi, "EnergyChartsBiddingZones")
        tracking_type_options = ["0", "1", "2", "3", "4", "5"]

        sections = [
            {
                "id": "standort",
                "title": "Standort",
                "description": "Geografische Grunddaten der Anlage.",
                "repeatable": False,
                "fields": [
                    {
                        "path": "general.latitude",
                        "label": "Breitengrad",
                        "hint": "Dezimalgrad, Norden positiv.",
                        "value_type": "number",
                        "unit": "°",
                        "minimum": -90,
                        "maximum": 90,
                        "step": 0.0001,
                    },
                    {
                        "path": "general.longitude",
                        "label": "Längengrad",
                        "hint": "Dezimalgrad, Osten positiv.",
                        "value_type": "number",
                        "unit": "°",
                        "minimum": -180,
                        "maximum": 180,
                        "step": 0.0001,
                    },
                ],
            },
            {
                "id": "pvforecast",
                "title": "PV & Forecast",
                "description": "PV-Prognoseanbieter und Planes-Konfiguration.",
                "repeatable": False,
                "fields": [
                    {
                        "path": "pvforecast.provider",
                        "label": "PV Forecast Provider",
                        "hint": "Provider für PV-Prognosedaten.",
                        "value_type": "select",
                        "options": provider_options["pvforecast.provider"],
                    },
                    {
                        "path": "pvforecast.planes[].peakpower",
                        "label": "Plane Peakpower",
                        "hint": "Nennleistung je Plane.",
                        "value_type": "number",
                        "unit": "kW",
                        "minimum": 0.0,
                        "maximum": None,
                        "step": 0.1,
                    },
                    {
                        "path": "pvforecast.planes[].surface_azimuth",
                        "label": "Ausrichtung (Azimuth)",
                        "hint": "0=Norden, 90=Osten, 180=Süden, 270=Westen.",
                        "value_type": "number",
                        "unit": "°",
                        "minimum": 0,
                        "maximum": 360,
                        "step": 1,
                    },
                    {
                        "path": "pvforecast.planes[].surface_tilt",
                        "label": "Neigung",
                        "hint": "Neigung gegen Horizontalebene.",
                        "value_type": "number",
                        "unit": "°",
                        "minimum": 0,
                        "maximum": 90,
                        "step": 1,
                    },
                    {
                        "path": "pvforecast.planes[].loss",
                        "label": "Verlustfaktor",
                        "hint": "Gesamtverluste in Prozent.",
                        "value_type": "number",
                        "unit": "%",
                        "minimum": 0,
                        "maximum": 100,
                        "step": 0.5,
                    },
                    {
                        "path": "pvforecast.planes[].trackingtype",
                        "label": "Trackingtyp",
                        "hint": "0=fixed, 1..5 verschiedene Tracking-Modi.",
                        "value_type": "select",
                        "options": tracking_type_options,
                    },
                    {
                        "path": "pvforecast.planes[].inverter_paco",
                        "label": "Inverter AC Nennleistung",
                        "hint": "Maximale AC-Leistung je Plane-Inverter.",
                        "value_type": "number",
                        "unit": "W",
                        "minimum": 0,
                        "maximum": None,
                        "step": 100,
                    },
                ],
            },
            {
                "id": "speicher_ev_inverter",
                "title": "Speicher, EV, Inverter",
                "description": "Geräte-Stammdaten für Optimierung und Steuerung.",
                "repeatable": True,
                "fields": [
                    {
                        "path": "devices.batteries[].capacity_wh",
                        "label": "Batteriekapazität",
                        "hint": "UI in kWh, intern Wh.",
                        "value_type": "number",
                        "unit": "kWh",
                        "minimum": 0,
                        "maximum": None,
                        "step": 0.1,
                    },
                    {
                        "path": "devices.batteries[].max_charge_power_w",
                        "label": "Max Ladeleistung",
                        "hint": "UI in kW, intern W.",
                        "value_type": "number",
                        "unit": "kW",
                        "minimum": 0,
                        "maximum": None,
                        "step": 0.1,
                    },
                    {
                        "path": "devices.inverters[].max_power_w",
                        "label": "Inverter Max Power",
                        "hint": "UI in kW, intern W.",
                        "value_type": "number",
                        "unit": "kW",
                        "minimum": 0,
                        "maximum": None,
                        "step": 0.1,
                    },
                ],
            },
            {
                "id": "tarife_load",
                "title": "Tarife & Last",
                "description": "Preis- und Lastmodellparameter.",
                "repeatable": False,
                "fields": [
                    {
                        "path": "elecprice.provider",
                        "label": "Strompreis-Provider",
                        "hint": "Quelle für Bezugspreis (z. B. ElecPriceImport für eigene 15-Min-Zeitreihe).",
                        "value_type": "select",
                        "options": provider_options["elecprice.provider"],
                    },
                    {
                        "path": "elecprice.charges_kwh",
                        "label": "Zusatzkosten Bezug",
                        "hint": "Wird auf variablen Preis aufgeschlagen.",
                        "value_type": "number",
                        "unit": "EUR/kWh",
                        "minimum": 0,
                        "maximum": None,
                        "step": 0.0001,
                    },
                    {
                        "path": "elecprice.vat_rate",
                        "label": "MwSt Faktor",
                        "hint": "Beispiel 1.19 für 19%.",
                        "value_type": "number",
                        "unit": "x",
                        "minimum": 1.0,
                        "maximum": None,
                        "step": 0.01,
                    },
                    {
                        "path": "elecprice.energycharts.bidding_zone",
                        "label": "EnergyCharts Bidding Zone",
                        "hint": "Relevant bei EnergyCharts-Provider.",
                        "value_type": "select",
                        "options": bidding_zone_options,
                    },
                    {
                        "path": "feedintariff.provider",
                        "label": "Einspeisevergütung Provider",
                        "hint": "Quelle für Einspeisetarif (in EOS v0.2 aktuell FeedInTariffFixed oder FeedInTariffImport).",
                        "value_type": "select",
                        "options": provider_options["feedintariff.provider"],
                    },
                    {
                        "path": "feedintariff.provider_settings.FeedInTariffFixed.feed_in_tariff_kwh",
                        "label": "Fester Einspeisetarif",
                        "hint": "Konstanter Tarif in EUR/kWh.",
                        "value_type": "number",
                        "unit": "EUR/kWh",
                        "minimum": 0,
                        "maximum": None,
                        "step": 0.0001,
                    },
                    {
                        "path": "load.provider",
                        "label": "Last-Provider",
                        "hint": "Quelle für Lastprognose.",
                        "value_type": "select",
                        "options": provider_options["load.provider"],
                    },
                    {
                        "path": "load.provider_settings.LoadAkkudoktor.loadakkudoktor_year_energy_kwh",
                        "label": "Jahresverbrauch",
                        "hint": "Jahresenergieverbrauch für Lastmodell.",
                        "value_type": "number",
                        "unit": "kWh/a",
                        "minimum": 0,
                        "maximum": None,
                        "step": 1,
                    },
                ],
            },
            {
                "id": "measurement_emr",
                "title": "Messwerte & EMR-Keys",
                "description": "Zuordnung der in EOS gespeicherten Mess-/Zaehler-Keys.",
                "repeatable": False,
                "fields": [
                    {
                        "path": "measurement.keys",
                        "label": "Measurement Keys",
                        "hint": (
                            "Liste aller Measurement-Keys. Muss die konfigurierten W- und EMR-Keys enthalten."
                        ),
                        "value_type": "string_list",
                        "unit": None,
                    },
                    {
                        "path": "measurement.load_emr_keys",
                        "label": "Load EMR Keys",
                        "hint": "Standard: house_load_emr_kwh",
                        "value_type": "string_list",
                        "unit": "kWh",
                    },
                    {
                        "path": "measurement.grid_import_emr_keys",
                        "label": "Grid Import EMR Keys",
                        "hint": "Standard: grid_import_emr_kwh",
                        "value_type": "string_list",
                        "unit": "kWh",
                    },
                    {
                        "path": "measurement.grid_export_emr_keys",
                        "label": "Grid Export EMR Keys",
                        "hint": "Standard: grid_export_emr_kwh",
                        "value_type": "string_list",
                        "unit": "kWh",
                    },
                    {
                        "path": "measurement.pv_production_emr_keys",
                        "label": "PV Production EMR Keys",
                        "hint": "Standard: pv_production_emr_kwh",
                        "value_type": "string_list",
                        "unit": "kWh",
                    },
                ],
            },
        ]

        return {
            "generated_at": datetime.now(timezone.utc),
            "sections": sections,
            "provider_options": provider_options,
            "bidding_zone_options": bidding_zone_options,
        }

    def _safe_config(self) -> dict[str, Any]:
        try:
            config = self._eos_client.get_config()
            if isinstance(config, dict):
                return config
        except Exception:
            pass
        return {}

    def _safe_openapi(self) -> dict[str, Any]:
        try:
            openapi = self._eos_client.get_openapi()
            if isinstance(openapi, dict):
                return openapi
        except Exception:
            pass
        return {}


def _providers_for_section(config: dict[str, Any], section: str) -> list[str]:
    section_payload = config.get(section)
    if not isinstance(section_payload, dict):
        return []
    providers = section_payload.get("providers")
    if not isinstance(providers, list):
        return []
    return [str(value) for value in providers if isinstance(value, str)]


def _enum_from_openapi(openapi: dict[str, Any], schema_name: str) -> list[str]:
    components = openapi.get("components")
    if not isinstance(components, dict):
        return []
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return []
    schema = schemas.get(schema_name)
    if not isinstance(schema, dict):
        return []
    enum_values = schema.get("enum")
    if not isinstance(enum_values, list):
        return []
    return [str(value) for value in enum_values]
