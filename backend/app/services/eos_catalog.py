import json
import logging
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from app.core.config import Settings


@dataclass
class FieldEntry:
    eos_field: str
    label: str
    description: str | None
    suggested_units: list[str] = field(default_factory=list)
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
            units = _infer_units(eos_field, description)
            _merge_field(
                by_field,
                FieldEntry(
                    eos_field=eos_field,
                    label=label,
                    description=description,
                    suggested_units=units,
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
                    sources=["measurement-keys"],
                ),
            )

    def _add_fallback_fields(self, by_field: dict[str, FieldEntry]) -> None:
        fallback_definitions = [
            (
                "pv_power_w",
                "PV Power",
                "Current PV power input, typically published as live MQTT telemetry.",
            ),
            (
                "house_load_w",
                "House Load",
                "Current total household load input, typically published as live MQTT telemetry.",
            ),
            (
                "grid_power_w",
                "Grid Power",
                "Current grid import/export power input.",
            ),
            (
                "battery_soc_pct",
                "Battery SOC",
                "Battery state-of-charge as percent value.",
            ),
            (
                "battery_power_w",
                "Battery Power",
                "Current battery charge/discharge power input.",
            ),
            (
                "ev_charging_power_w",
                "EV Charging Power",
                "Current EV charging power input.",
            ),
            (
                "temperature_c",
                "Temperature",
                "Temperature value in Celsius.",
            ),
        ]
        for eos_field, label, description in fallback_definitions:
            _merge_field(
                by_field,
                FieldEntry(
                    eos_field=eos_field,
                    label=label,
                    description=description,
                    suggested_units=_infer_units(eos_field, description),
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


def _infer_units(eos_field: str, description: str | None) -> list[str]:
    field_lower = eos_field.lower()
    description_lower = (description or "").lower()

    if "euro_pro_wh" in field_lower or "euros per watt-hour" in description_lower:
        return ["EUR/Wh", "ct/kWh"]

    if field_lower.endswith("_pct") or "percent" in description_lower:
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

