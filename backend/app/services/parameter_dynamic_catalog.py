from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class DynamicParameterCatalogEntry:
    parameter_key: str
    label: str
    hint: str
    value_type: str
    expected_unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    options: list[str] = field(default_factory=list)
    requires_selector: bool = False
    selector_hint: str | None = None
    examples: list[str] = field(default_factory=list)


class ParameterDynamicCatalogService:
    def __init__(self) -> None:
        self._entries = _default_entries()
        self._by_key = {entry.parameter_key: entry for entry in self._entries}

    def list_entries(self) -> list[DynamicParameterCatalogEntry]:
        return list(self._entries)

    def get_entry(self, parameter_key: str) -> DynamicParameterCatalogEntry | None:
        return self._by_key.get(parameter_key)

    def build_catalog(self) -> dict[str, object]:
        return {
            "generated_at": datetime.now(timezone.utc),
            "items": [
                {
                    "parameter_key": entry.parameter_key,
                    "label": entry.label,
                    "hint": entry.hint,
                    "value_type": entry.value_type,
                    "expected_unit": entry.expected_unit,
                    "minimum": entry.minimum,
                    "maximum": entry.maximum,
                    "options": entry.options,
                    "requires_selector": entry.requires_selector,
                    "selector_hint": entry.selector_hint,
                    "examples": entry.examples,
                }
                for entry in self._entries
            ],
        }


def _default_entries() -> list[DynamicParameterCatalogEntry]:
    selector_hint = "Device-ID (z. B. lfp oder shaby)"
    return [
        DynamicParameterCatalogEntry(
            parameter_key="ems.mode",
            label="EMS Modus",
            hint="Steuert den Ausführungsmodus des Energy-Managements.",
            value_type="enum",
            options=["OPTIMIZATION", "IDLE", "DISABLED"],
            examples=["OPTIMIZATION"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="ems.interval",
            label="EMS Intervall",
            hint="Intervall in Sekunden zwischen automatischen Läufen.",
            value_type="number",
            expected_unit="s",
            minimum=1,
            maximum=86400,
            examples=["900"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="devices.batteries[].min_soc_percentage",
            label="Batterie Min-SOC",
            hint="Untergrenze für Batterie-SOC in Prozent.",
            value_type="number",
            expected_unit="%",
            minimum=0,
            maximum=100,
            requires_selector=True,
            selector_hint=selector_hint,
            examples=["10"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="devices.batteries[].max_soc_percentage",
            label="Batterie Max-SOC",
            hint="Obergrenze für Batterie-SOC in Prozent.",
            value_type="number",
            expected_unit="%",
            minimum=0,
            maximum=100,
            requires_selector=True,
            selector_hint=selector_hint,
            examples=["95"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="devices.batteries[].min_charge_power_w",
            label="Batterie Min Ladeleistung",
            hint="Minimale Ladeleistung in Watt.",
            value_type="number",
            expected_unit="W",
            minimum=0,
            maximum=100000,
            requires_selector=True,
            selector_hint=selector_hint,
            examples=["500"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="devices.batteries[].max_charge_power_w",
            label="Batterie Max Ladeleistung",
            hint="Maximale Ladeleistung in Watt.",
            value_type="number",
            expected_unit="W",
            minimum=0,
            maximum=100000,
            requires_selector=True,
            selector_hint=selector_hint,
            examples=["18000"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="devices.electric_vehicles[].min_soc_percentage",
            label="EV Min-SOC",
            hint="Untergrenze für EV-SOC in Prozent.",
            value_type="number",
            expected_unit="%",
            minimum=0,
            maximum=100,
            requires_selector=True,
            selector_hint=selector_hint,
            examples=["20"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="devices.electric_vehicles[].max_soc_percentage",
            label="EV Max-SOC",
            hint="Obergrenze für EV-SOC in Prozent.",
            value_type="number",
            expected_unit="%",
            minimum=0,
            maximum=100,
            requires_selector=True,
            selector_hint=selector_hint,
            examples=["90"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="devices.electric_vehicles[].min_charge_power_w",
            label="EV Min Ladeleistung",
            hint="Minimale EV-Ladeleistung in Watt.",
            value_type="number",
            expected_unit="W",
            minimum=0,
            maximum=100000,
            requires_selector=True,
            selector_hint=selector_hint,
            examples=["1400"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="devices.electric_vehicles[].max_charge_power_w",
            label="EV Max Ladeleistung",
            hint="Maximale EV-Ladeleistung in Watt.",
            value_type="number",
            expected_unit="W",
            minimum=0,
            maximum=100000,
            requires_selector=True,
            selector_hint=selector_hint,
            examples=["11000"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="devices.inverters[].max_power_w",
            label="Inverter Max-Leistung",
            hint="Maximale Inverterleistung in Watt.",
            value_type="number",
            expected_unit="W",
            minimum=0,
            maximum=100000,
            requires_selector=True,
            selector_hint=selector_hint,
            examples=["30000"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="elecprice.charges_kwh",
            label="Strompreis Zuschlag",
            hint="Zusatzkosten pro kWh in EUR/kWh.",
            value_type="number",
            expected_unit="EUR/kWh",
            minimum=0,
            maximum=10,
            examples=["0.23"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="elecprice.vat_rate",
            label="MwSt Faktor",
            hint="Faktor, z. B. 1.19 für 19%.",
            value_type="number",
            expected_unit="x",
            minimum=0,
            maximum=5,
            examples=["1.19"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="feedintariff.provider_settings.FeedInTariffFixed.feed_in_tariff_kwh",
            label="Einspeisetarif",
            hint="Fester Einspeisetarif in EUR/kWh.",
            value_type="number",
            expected_unit="EUR/kWh",
            minimum=0,
            maximum=10,
            examples=["0.09"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="measurement.keys",
            label="Measurement Keys",
            hint="Kommagetrennte Liste oder JSON-Liste.",
            value_type="string_list",
            examples=["house_load_w,pv_power_w"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="measurement.load_emr_keys",
            label="Load EMR Keys",
            hint="Kommagetrennte Liste oder JSON-Liste.",
            value_type="string_list",
            examples=["house_load_emr_kwh"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="measurement.grid_import_emr_keys",
            label="Grid Import EMR Keys",
            hint="Kommagetrennte Liste oder JSON-Liste.",
            value_type="string_list",
            examples=["grid_import_emr_kwh"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="measurement.grid_export_emr_keys",
            label="Grid Export EMR Keys",
            hint="Kommagetrennte Liste oder JSON-Liste.",
            value_type="string_list",
            examples=["grid_export_emr_kwh"],
        ),
        DynamicParameterCatalogEntry(
            parameter_key="measurement.pv_production_emr_keys",
            label="PV Production EMR Keys",
            hint="Kommagetrennte Liste oder JSON-Liste.",
            value_type="string_list",
            examples=["pv_production_emr_kwh"],
        ),
    ]
