import json
import logging
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from app.core.config import Settings


_OPENAPI_FIELD_HELP_OVERRIDES: dict[str, tuple[str, list[str]]] = {
    "gesamtlast": (
        "Zeitreihe des gesamten Hausverbrauchs fuer die EOS-Optimierung.",
        [
            "Erwartet ein JSON-Array mit einem Verbrauchswert pro EOS-Zeitintervall, z. B. [420, 380, 410, ...].",
            "Einheit ist intern W je Intervallwert. Falls die Quelle kW liefert, multiplier im Mapping auf 1000 setzen.",
            "Fuer Live-Einzelwerte aus MQTT ist meistens house_load_w die passendere Feldwahl.",
        ],
    ),
    "pv_prognose_wh": (
        "Zeitreihe der erwarteten PV-Leistung fuer die EOS-Optimierung.",
        [
            "Erwartet ein JSON-Array mit einem Prognosewert pro EOS-Zeitintervall.",
            "Das ist eine Prognose-Zeitreihe, kein einzelner Live-Messwert vom aktuellen Zeitpunkt.",
            "Bei Live-PV-Daten aus MQTT ist meistens pv_power_w die passendere Feldwahl.",
        ],
    ),
    "strompreis_euro_pro_wh": (
        "Zeitreihe der Strombezugspreise fuer die Optimierung.",
        [
            "Erwartet ein JSON-Array mit einem Preiswert pro EOS-Zeitintervall.",
            "EOS arbeitet intern mit EUR/Wh. Bei Eingabe in EUR/kWh den multiplier auf 0.001 setzen.",
        ],
    ),
    "einspeiseverguetung_euro_pro_wh": (
        "Einspeiseverguetung fuer Export ins Netz (konstant oder als Zeitreihe).",
        [
            "Kann als einzelner fixer Wert oder als JSON-Array pro EOS-Zeitintervall gesetzt werden.",
            "Bei konstantem Tarif in der UI 'Fixed value' verwenden.",
            "EOS arbeitet intern mit EUR/Wh. Bei Eingabe in EUR/kWh den multiplier auf 0.001 setzen.",
        ],
    ),
    "preis_euro_pro_wh_akku": (
        "Bewertungskosten je Wh fuer Akku-Energie (z. B. Verschleiss-/Nutzungskosten).",
        [
            "Typischerweise ein fixer Wert statt Live-MQTT-Signal.",
            "EOS arbeitet intern mit EUR/Wh. Bei Eingabe in EUR/kWh den multiplier auf 0.001 setzen.",
        ],
    ),
}


@dataclass
class FieldEntry:
    eos_field: str
    label: str
    description: str | None
    suggested_units: list[str] = field(default_factory=list)
    info_notes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


class EosFieldCatalogService:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._logger = logging.getLogger("app.eos_catalog")

    def list_fields(self) -> list[FieldEntry]:
        by_field: dict[str, FieldEntry] = {}
        self._add_openapi_fields(by_field)
        self._add_measurement_keys(by_field)
        self._add_fallback_fields(by_field)

        return sorted(by_field.values(), key=lambda item: item.eos_field)

    def _add_openapi_fields(self, by_field: dict[str, FieldEntry]) -> None:
        openapi_url = urljoin(self._settings.eos_base_url.rstrip("/") + "/", "openapi.json")
        payload = self._fetch_json(openapi_url)
        if not isinstance(payload, dict):
            self._logger.warning("unexpected EOS openapi format at url=%s", openapi_url)
            return

        try:
            props = (
                payload["components"]["schemas"]["GeneticEnergyManagementParameters"]["properties"]
            )
        except KeyError:
            self._logger.warning("could not find GeneticEnergyManagementParameters in EOS openapi")
            return

        for eos_field, metadata in props.items():
            if not isinstance(metadata, dict):
                continue
            label = str(metadata.get("title") or eos_field)
            description = metadata.get("description")
            if description is not None:
                description = str(description)
            description, info_notes = _resolve_openapi_field_help(
                eos_field=eos_field,
                metadata=metadata,
                fallback_description=description,
            )
            units = _infer_units(eos_field, description)
            _merge_field(
                by_field,
                FieldEntry(
                    eos_field=eos_field,
                    label=label,
                    description=description,
                    suggested_units=units,
                    info_notes=info_notes,
                    sources=["eos-openapi"],
                ),
            )

    def _add_measurement_keys(self, by_field: dict[str, FieldEntry]) -> None:
        keys_url = urljoin(self._settings.eos_base_url.rstrip("/") + "/", "v1/measurement/keys")
        payload = self._fetch_json(keys_url)
        if not isinstance(payload, list):
            return

        for raw_key in payload:
            if not isinstance(raw_key, str):
                continue
            eos_field = raw_key.strip()
            if eos_field == "":
                continue
            _merge_field(
                by_field,
                FieldEntry(
                    eos_field=eos_field,
                    label=eos_field.replace("_", " ").title(),
                    description="Available measurement key from EOS runtime.",
                    suggested_units=_infer_units(eos_field, None),
                    info_notes=[],
                    sources=["measurement-keys"],
                ),
            )

    def _add_fallback_fields(self, by_field: dict[str, FieldEntry]) -> None:
        fallback_definitions = [
            (
                "pv_power_w",
                "PV Power",
                "Current PV power input, typically published as live MQTT telemetry.",
                [],
            ),
            (
                "house_load_w",
                "House Load",
                "Current total household load input, typically published as live MQTT telemetry.",
                [],
            ),
            (
                "grid_power_w",
                "Grid Power",
                "Current grid import/export power input.",
                [
                    "Sign convention is often installation-specific. Verify with your meter before relying on optimization.",
                    "EOS docs currently model grid direction with separate import/export measurement keys instead of one signed grid_power_w field.",
                    "If your input topic is named grid_power_consumption_kw, it is treated as import/consumption: positive=import (Bezug), negative=export (Einspeisung).",
                    "Canonical app convention: positive = grid import (Bezug), negative = export (Einspeisung).",
                ],
            ),
            (
                "battery_soc_pct",
                "Battery SOC",
                "Battery state-of-charge as percent value.",
                [],
            ),
            (
                "battery_soc_percent",
                "Battery SOC",
                "Battery state-of-charge as percent value (alias naming).",
                [
                    "Hinweis: In vielen EOS-Feldern wird auch battery_soc_pct verwendet. Beide bezeichnen denselben Inhalt (SOC in Prozent).",
                ],
            ),
            (
                "battery_power_w",
                "Battery Power",
                "Current battery charge/discharge power input.",
                [],
            ),
            (
                "ev_charging_power_w",
                "EV Charging Power",
                "Current EV charging power input.",
                [],
            ),
            (
                "temperature_c",
                "Temperature",
                "Temperature value in Celsius.",
                [],
            ),
        ]
        for eos_field, label, description, info_notes in fallback_definitions:
            _merge_field(
                by_field,
                FieldEntry(
                    eos_field=eos_field,
                    label=label,
                    description=description,
                    suggested_units=_infer_units(eos_field, description),
                    info_notes=info_notes,
                    sources=["fallback"],
                ),
            )

    def _fetch_json(self, url: str) -> object | None:
        try:
            request = Request(url=url, method="GET")
            with urlopen(request, timeout=5.0) as response:
                if response.status != 200:
                    self._logger.warning("EOS request failed status=%s url=%s", response.status, url)
                    return None
                body = response.read().decode("utf-8")
                return json.loads(body)
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
            self._logger.exception("failed to fetch EOS data url=%s", url)
            return None


def _unique_preserve_order(values: list[str]) -> list[str]:
    unique_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


def _merge_field(by_field: dict[str, FieldEntry], incoming: FieldEntry) -> None:
    current = by_field.get(incoming.eos_field)
    if current is None:
        incoming.sources = _unique_preserve_order(incoming.sources)
        incoming.suggested_units = _unique_preserve_order(incoming.suggested_units)
        incoming.info_notes = _unique_preserve_order(incoming.info_notes)
        by_field[incoming.eos_field] = incoming
        return

    if not current.description and incoming.description:
        current.description = incoming.description

    if current.label == current.eos_field and incoming.label != incoming.eos_field:
        current.label = incoming.label

    current.sources = _unique_preserve_order(current.sources + incoming.sources)
    current.suggested_units = _unique_preserve_order(
        current.suggested_units + incoming.suggested_units
    )
    current.info_notes = _unique_preserve_order(current.info_notes + incoming.info_notes)


def _infer_units(eos_field: str, description: str | None) -> list[str]:
    field_lower = eos_field.lower()
    description_lower = (description or "").lower()

    if "euro_pro_wh" in field_lower or "euros per watt-hour" in description_lower:
        return ["EUR/kWh", "EUR/Wh", "ct/kWh"]

    if (
        field_lower.endswith("_pct")
        or field_lower.endswith("_percent")
        or "percent" in description_lower
    ):
        return ["%"]

    if (
        field_lower.endswith("_c")
        or "celsius" in description_lower
        or "temperature" in field_lower
    ):
        return ["C"]

    if field_lower.endswith("_wh") or "watt-hour" in description_lower:
        return ["Wh", "kWh"]

    if field_lower.endswith("_w") or " watt" in description_lower or "watts" in description_lower:
        return ["W", "kW"]

    return []


def _resolve_openapi_field_help(
    *,
    eos_field: str,
    metadata: dict[str, object],
    fallback_description: str | None,
) -> tuple[str | None, list[str]]:
    description = fallback_description
    notes: list[str] = []

    override = _OPENAPI_FIELD_HELP_OVERRIDES.get(eos_field)
    if override is not None:
        description, override_notes = override
        notes.extend(override_notes)

    field_type = metadata.get("type")
    if field_type == "array":
        notes.append(
            "Array-Feld: Werte als JSON-Liste uebergeben, nicht als einzelnes Skalar-Topic."
        )
    else:
        any_of = metadata.get("anyOf")
        if isinstance(any_of, list):
            has_array = any(
                isinstance(option, dict) and option.get("type") == "array" for option in any_of
            )
            has_number = any(
                isinstance(option, dict) and option.get("type") == "number" for option in any_of
            )
            if has_array and has_number:
                notes.append(
                    "Feld erlaubt sowohl Einzelwert als auch Zeitreihe (JSON-Array)."
                )

    return description, _unique_preserve_order(notes)
