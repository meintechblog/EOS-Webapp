from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from jsonschema import Draft202012Validator

from app.repositories.emr_pipeline import DEFAULT_MEASUREMENT_EMR_KEYS
from app.services.eos_client import EosClient

MASKED_SECRET_PLACEHOLDER = "***MASKED***"


@dataclass
class ValidationOutcome:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized_payload: dict[str, Any] | None = None


class EosSettingsValidationService:
    def __init__(self, *, eos_client: EosClient):
        self._eos_client = eos_client
        self._logger = logging.getLogger("app.eos_settings_validation")
        self._openapi_lock = Lock()
        self._openapi_cache: dict[str, Any] | None = None

    def sanitize_payload(
        self,
        payload: dict[str, Any],
        *,
        strict_unknown_fields: bool,
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        openapi = self._get_openapi()
        schemas = _schema_map(openapi)
        root_schema = self._resolve_schema({"$ref": "#/components/schemas/SettingsEOS"}, schemas)
        errors: list[str] = []
        warnings: list[str] = []
        normalized = self._normalize_value(
            value=payload,
            schema=root_schema,
            schemas=schemas,
            path="",
            strict_unknown_fields=strict_unknown_fields,
            errors=errors,
            warnings=warnings,
        )
        if not isinstance(normalized, dict):
            errors.append("Top-level payload must be an object")
            return {}, errors, warnings

        self._normalize_domain_defaults(normalized, warnings)
        return normalized, errors, warnings

    def validate_payload(
        self,
        payload: dict[str, Any],
        *,
        strict_unknown_fields: bool = True,
        fail_on_masked_secrets: bool = False,
    ) -> ValidationOutcome:
        normalized, sanitize_errors, warnings = self.sanitize_payload(
            payload,
            strict_unknown_fields=strict_unknown_fields,
        )
        errors = list(sanitize_errors)

        if fail_on_masked_secrets:
            masked_paths = self.find_masked_secret_paths(normalized)
            for path in masked_paths:
                errors.append(
                    f"Sensitive field at '{path}' still contains masked placeholder; provide a real value."
                )

        openapi = self._get_openapi()
        settings_schema = {
            "$ref": "#/components/schemas/SettingsEOS",
            "components": openapi.get("components", {}),
        }
        validator = Draft202012Validator(settings_schema)
        schema_errors = sorted(validator.iter_errors(normalized), key=lambda item: list(item.path))
        for schema_error in schema_errors:
            path = ".".join(str(part) for part in schema_error.path)
            location = path if path else "$"
            errors.append(f"{location}: {schema_error.message}")

        errors.extend(self._domain_errors(normalized))
        warnings.extend(self._domain_warnings(normalized))
        return ValidationOutcome(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            normalized_payload=normalized,
        )

    def sanitize_eos_config_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized, _errors, _warnings = self.sanitize_payload(
            payload,
            strict_unknown_fields=False,
        )
        return normalized

    def mask_sensitive_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        cloned = copy.deepcopy(payload)
        self._mask_sensitive_in_place(cloned, path="")
        return cloned

    def find_masked_secret_paths(self, payload: dict[str, Any]) -> list[str]:
        masked_paths: list[str] = []
        self._collect_masked_paths(payload, path="", output=masked_paths)
        return masked_paths

    def _get_openapi(self) -> dict[str, Any]:
        with self._openapi_lock:
            if self._openapi_cache is not None:
                return self._openapi_cache
            self._openapi_cache = self._eos_client.get_openapi()
            return self._openapi_cache

    def _resolve_schema(self, schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
        if "$ref" not in schema:
            return schema
        ref = schema["$ref"]
        if not isinstance(ref, str):
            return schema
        if not ref.startswith("#/components/schemas/"):
            return schema
        key = ref.split("/")[-1]
        resolved = schemas.get(key)
        if isinstance(resolved, dict):
            return resolved
        return schema

    def _normalize_value(
        self,
        *,
        value: Any,
        schema: dict[str, Any],
        schemas: dict[str, Any],
        path: str,
        strict_unknown_fields: bool,
        errors: list[str],
        warnings: list[str],
    ) -> Any:
        resolved = self._resolve_schema(schema, schemas)

        any_of = resolved.get("anyOf")
        if isinstance(any_of, list):
            return self._normalize_any_of(
                value=value,
                any_of=any_of,
                schemas=schemas,
                path=path,
                strict_unknown_fields=strict_unknown_fields,
                errors=errors,
                warnings=warnings,
            )

        if value is None:
            return None

        if _is_object_schema(resolved):
            if not isinstance(value, dict):
                errors.append(f"{path or '$'}: expected object")
                return {}
            properties = resolved.get("properties", {})
            if not isinstance(properties, dict):
                properties = {}
            normalized_object: dict[str, Any] = {}
            for key, raw_child in value.items():
                child_path = _join_path(path, key)
                child_schema = properties.get(key)
                if child_schema is None:
                    extension_schema = _extension_schema_for_path(path=path, key=key)
                    if extension_schema is not None:
                        normalized_object[key] = self._normalize_value(
                            value=raw_child,
                            schema=extension_schema,
                            schemas=schemas,
                            path=child_path,
                            strict_unknown_fields=strict_unknown_fields,
                            errors=errors,
                            warnings=warnings,
                        )
                        continue
                    if strict_unknown_fields:
                        errors.append(f"{child_path}: unknown field")
                    else:
                        warnings.append(f"{child_path}: dropped unknown field")
                    continue
                normalized_object[key] = self._normalize_value(
                    value=raw_child,
                    schema=child_schema,
                    schemas=schemas,
                    path=child_path,
                    strict_unknown_fields=strict_unknown_fields,
                    errors=errors,
                    warnings=warnings,
                )
            return normalized_object

        if _is_array_schema(resolved):
            if not isinstance(value, list):
                errors.append(f"{path or '$'}: expected array")
                return []
            item_schema = resolved.get("items", {})
            normalized_items: list[Any] = []
            for idx, raw_item in enumerate(value):
                normalized_items.append(
                    self._normalize_value(
                        value=raw_item,
                        schema=item_schema if isinstance(item_schema, dict) else {},
                        schemas=schemas,
                        path=_join_path(path, str(idx)),
                        strict_unknown_fields=strict_unknown_fields,
                        errors=errors,
                        warnings=warnings,
                    )
                )
            return normalized_items

        return value

    def _normalize_any_of(
        self,
        *,
        value: Any,
        any_of: list[Any],
        schemas: dict[str, Any],
        path: str,
        strict_unknown_fields: bool,
        errors: list[str],
        warnings: list[str],
    ) -> Any:
        if value is None:
            return None

        branch_candidates = [candidate for candidate in any_of if isinstance(candidate, dict)]
        matched = _pick_schema_branch(value, branch_candidates, schemas)
        if matched is None and branch_candidates:
            matched = branch_candidates[0]
        if matched is None:
            return value

        return self._normalize_value(
            value=value,
            schema=matched,
            schemas=schemas,
            path=path,
            strict_unknown_fields=strict_unknown_fields,
            errors=errors,
            warnings=warnings,
        )

    def _normalize_domain_defaults(self, payload: dict[str, Any], warnings: list[str]) -> None:
        pvforecast = payload.get("pvforecast")
        if not isinstance(pvforecast, dict):
            return
        planes = pvforecast.get("planes")
        if isinstance(planes, list):
            expected = len(planes)
            current = pvforecast.get("max_planes")
            if current != expected:
                pvforecast["max_planes"] = expected
                warnings.append(
                    f"pvforecast.max_planes was normalized to {expected} to match configured planes."
                )
        self._normalize_pvforecast_import_series(payload=payload, warnings=warnings)

    def _normalize_pvforecast_import_series(
        self,
        *,
        payload: dict[str, Any],
        warnings: list[str],
    ) -> None:
        pvforecast = payload.get("pvforecast")
        if not isinstance(pvforecast, dict):
            return
        if pvforecast.get("provider") != "PVForecastImport":
            return
        provider_settings = pvforecast.get("provider_settings")
        if not isinstance(provider_settings, dict):
            return
        import_settings = provider_settings.get("PVForecastImport")
        if not isinstance(import_settings, dict):
            return
        import_json = import_settings.get("import_json")
        if not isinstance(import_json, str) or import_json.strip() == "":
            return
        try:
            parsed = json.loads(import_json)
        except json.JSONDecodeError:
            return
        if not isinstance(parsed, dict):
            return

        ac_values = _numeric_list_or_none(parsed.get("pvforecast_ac_power"))
        if ac_values is None or len(ac_values) == 0:
            return
        dc_values = _numeric_list_or_none(parsed.get("pvforecast_dc_power"))
        if dc_values is None:
            parsed["pvforecast_dc_power"] = list(ac_values)
            import_settings["import_json"] = json.dumps(parsed, separators=(",", ":"))
            warnings.append(
                "pvforecast.import_json was normalized: missing `pvforecast_dc_power` copied from `pvforecast_ac_power`."
            )
            return
        if len(dc_values) != len(ac_values):
            parsed["pvforecast_dc_power"] = list(ac_values)
            import_settings["import_json"] = json.dumps(parsed, separators=(",", ":"))
            warnings.append(
                "pvforecast.import_json was normalized: `pvforecast_dc_power` length aligned to `pvforecast_ac_power`."
            )

    def _domain_errors(self, payload: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        devices = payload.get("devices")
        if isinstance(devices, dict):
            batteries = devices.get("batteries")
            if isinstance(batteries, list):
                for idx, battery in enumerate(batteries):
                    if not isinstance(battery, dict):
                        continue
                    min_soc = _float_or_none(battery.get("min_soc_percentage"))
                    max_soc = _float_or_none(battery.get("max_soc_percentage"))
                    if min_soc is not None and max_soc is not None and max_soc < min_soc:
                        errors.append(
                            f"devices.batteries.{idx}: max_soc_percentage must be >= min_soc_percentage"
                        )

            electric_vehicles = devices.get("electric_vehicles")
            if isinstance(electric_vehicles, list):
                for idx, ev in enumerate(electric_vehicles):
                    if not isinstance(ev, dict):
                        continue
                    min_soc = _float_or_none(ev.get("min_soc_percentage"))
                    max_soc = _float_or_none(ev.get("max_soc_percentage"))
                    if min_soc is not None and max_soc is not None and max_soc < min_soc:
                        errors.append(
                            f"devices.electric_vehicles.{idx}: max_soc_percentage must be >= min_soc_percentage"
                        )

            battery_ids: set[str] = set()
            if isinstance(devices.get("batteries"), list):
                for battery in devices["batteries"]:
                    if isinstance(battery, dict):
                        battery_id = battery.get("device_id")
                        if isinstance(battery_id, str) and battery_id.strip() != "":
                            battery_ids.add(battery_id.strip())

            inverters = devices.get("inverters")
            if isinstance(inverters, list):
                for idx, inverter in enumerate(inverters):
                    if not isinstance(inverter, dict):
                        continue
                    battery_id = inverter.get("battery_id")
                    if battery_id is None:
                        continue
                    if not isinstance(battery_id, str):
                        errors.append(f"devices.inverters.{idx}.battery_id must be a string")
                        continue
                    if battery_id.strip() not in battery_ids:
                        errors.append(
                            f"devices.inverters.{idx}.battery_id references unknown battery '{battery_id}'."
                        )

        config = self._safe_get_live_config()
        if config is not None:
            errors.extend(self._provider_errors(payload, config))
        errors.extend(self._pvforecast_errors(payload))
        errors.extend(self._measurement_errors(payload))
        return errors

    def _domain_warnings(self, payload: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        warnings.extend(self._measurement_warnings(payload))
        return warnings

    def _measurement_errors(self, payload: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        measurement = payload.get("measurement")
        if measurement is None:
            errors.append("measurement section must be configured for EMR measurement sync.")
            return errors
        if not isinstance(measurement, dict):
            errors.append("measurement section must be an object.")
            return errors

        measurement_keys_raw = measurement.get("keys")
        if not isinstance(measurement_keys_raw, list):
            errors.append("measurement.keys must be a list.")
            measurement_keys_raw = []
        measurement_keys = {
            key.strip()
            for key in measurement_keys_raw
            if isinstance(key, str) and key.strip() != ""
        }
        if len(measurement_keys) == 0:
            errors.append("measurement.keys must not be empty.")

        for field_name in DEFAULT_MEASUREMENT_EMR_KEYS:
            configured = measurement.get(field_name)
            if not isinstance(configured, list):
                errors.append(f"measurement.{field_name} must be a list.")
                continue
            configured_keys = [
                key.strip() for key in configured if isinstance(key, str) and key.strip() != ""
            ]
            if len(configured_keys) == 0:
                errors.append(f"measurement.{field_name} must not be empty.")
                continue
            for key in configured_keys:
                if key not in measurement_keys:
                    errors.append(
                        f"measurement.{field_name} key '{key}' is missing in measurement.keys."
                    )
        return errors

    def _measurement_warnings(self, payload: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        measurement = payload.get("measurement")
        if not isinstance(measurement, dict):
            return warnings

        for field_name, defaults in DEFAULT_MEASUREMENT_EMR_KEYS.items():
            configured = measurement.get(field_name)
            if not isinstance(configured, list):
                continue
            configured_keys = {
                key.strip() for key in configured if isinstance(key, str) and key.strip() != ""
            }
            if len(configured_keys) == 0:
                continue
            for default_key in defaults:
                if default_key not in configured_keys:
                    warnings.append(
                        f"measurement.{field_name} does not include default pipeline key '{default_key}'."
                    )
        return warnings

    def _provider_errors(self, payload: dict[str, Any], config: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        provider_paths = [
            ("pvforecast.provider", _providers_for_section(config, "pvforecast")),
            ("elecprice.provider", _providers_for_section(config, "elecprice")),
            ("feedintariff.provider", _providers_for_section(config, "feedintariff")),
            ("load.provider", _providers_for_section(config, "load")),
            ("weather.provider", _providers_for_section(config, "weather")),
        ]
        for path, allowed in provider_paths:
            if not allowed:
                continue
            value = _read_path(payload, path)
            if value is None:
                continue
            if not isinstance(value, str):
                continue
            if value not in allowed:
                errors.append(
                    f"{path} uses unsupported provider '{value}'. Allowed: {', '.join(allowed)}."
                )
        return errors

    def _pvforecast_errors(self, payload: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        pvforecast = payload.get("pvforecast")
        if not isinstance(pvforecast, dict):
            return errors

        provider = pvforecast.get("provider")
        planes = pvforecast.get("planes")
        if provider == "PVForecastAkkudoktor" and isinstance(planes, list):
            for idx, plane in enumerate(planes):
                if not isinstance(plane, dict):
                    continue
                tilt = _float_or_none(plane.get("surface_tilt"))
                if tilt is not None and tilt <= 0:
                    errors.append(
                        f"pvforecast.planes.{idx}.surface_tilt must be > 0 for PVForecastAkkudoktor (upstream API rejects tilt=0)."
                    )

        if provider != "PVForecastImport":
            return errors

        provider_settings = pvforecast.get("provider_settings")
        if not isinstance(provider_settings, dict):
            errors.append("pvforecast.provider_settings must be an object for PVForecastImport.")
            return errors
        import_settings = provider_settings.get("PVForecastImport")
        if not isinstance(import_settings, dict):
            errors.append("pvforecast.provider_settings.PVForecastImport must be an object.")
            return errors
        import_json = import_settings.get("import_json")
        if not isinstance(import_json, str) or import_json.strip() == "":
            errors.append(
                "pvforecast.provider_settings.PVForecastImport.import_json must be a non-empty JSON string."
            )
            return errors
        try:
            parsed = json.loads(import_json)
        except json.JSONDecodeError as exc:
            errors.append(
                "pvforecast.provider_settings.PVForecastImport.import_json must be valid JSON "
                f"({exc.msg})."
            )
            return errors
        if not isinstance(parsed, dict):
            errors.append(
                "pvforecast.provider_settings.PVForecastImport.import_json must decode to an object."
            )
            return errors

        ac_values = _numeric_list_or_none(parsed.get("pvforecast_ac_power"))
        dc_values = _numeric_list_or_none(parsed.get("pvforecast_dc_power"))
        if ac_values is None or len(ac_values) == 0:
            errors.append(
                "pvforecast.provider_settings.PVForecastImport.import_json must contain numeric `pvforecast_ac_power` array."
            )
        if dc_values is None or len(dc_values) == 0:
            errors.append(
                "pvforecast.provider_settings.PVForecastImport.import_json must contain numeric `pvforecast_dc_power` array."
            )
        if ac_values and dc_values and len(ac_values) != len(dc_values):
            errors.append(
                "pvforecast.provider_settings.PVForecastImport arrays `pvforecast_ac_power` and `pvforecast_dc_power` must have equal length."
            )
        return errors

    def _safe_get_live_config(self) -> dict[str, Any] | None:
        try:
            config = self._eos_client.get_config()
            if isinstance(config, dict):
                return config
        except Exception as exc:
            self._logger.warning("provider validation fallback: failed to load live eos config: %s", exc)
        return None

    def _mask_sensitive_in_place(self, payload: Any, *, path: str) -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                child_path = _join_path(path, key)
                if _is_sensitive_key(key) and value is not None:
                    payload[key] = MASKED_SECRET_PLACEHOLDER
                    continue
                self._mask_sensitive_in_place(value, path=child_path)
            return
        if isinstance(payload, list):
            for idx, value in enumerate(payload):
                self._mask_sensitive_in_place(value, path=_join_path(path, str(idx)))

    def _collect_masked_paths(self, payload: Any, *, path: str, output: list[str]) -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                child_path = _join_path(path, key)
                if _is_sensitive_key(key) and isinstance(value, str) and value == MASKED_SECRET_PLACEHOLDER:
                    output.append(child_path)
                self._collect_masked_paths(value, path=child_path, output=output)
            return
        if isinstance(payload, list):
            for idx, value in enumerate(payload):
                self._collect_masked_paths(value, path=_join_path(path, str(idx)), output=output)


def _schema_map(openapi_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    components = openapi_payload.get("components")
    if not isinstance(components, dict):
        return {}
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return {}
    return {key: value for key, value in schemas.items() if isinstance(value, dict)}


def _pick_schema_branch(
    value: Any,
    candidates: list[dict[str, Any]],
    schemas: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    for candidate in candidates:
        resolved = candidate
        if "$ref" in candidate:
            ref = candidate["$ref"]
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                resolved = schemas.get(ref.split("/")[-1], candidate)
        if _schema_matches_value(resolved, value):
            return candidate
    return None


def _schema_matches_value(schema: dict[str, Any], value: Any) -> bool:
    if value is None:
        return schema.get("type") == "null"
    if _is_object_schema(schema):
        return isinstance(value, dict)
    if _is_array_schema(schema):
        return isinstance(value, list)
    schema_type = schema.get("type")
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return isinstance(value, bool)
    return True


def _is_object_schema(schema: dict[str, Any]) -> bool:
    schema_type = schema.get("type")
    return schema_type == "object" or "properties" in schema


def _is_array_schema(schema: dict[str, Any]) -> bool:
    schema_type = schema.get("type")
    return schema_type == "array" or "items" in schema


def _join_path(path: str, token: str) -> str:
    if path == "":
        return token
    return f"{path}.{token}"


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _providers_for_section(config: dict[str, Any], section: str) -> list[str]:
    section_payload = config.get(section)
    if not isinstance(section_payload, dict):
        return []
    providers = section_payload.get("providers")
    if not isinstance(providers, list):
        return []
    return [str(item) for item in providers if isinstance(item, str)]


def _extension_schema_for_path(*, path: str, key: str) -> dict[str, Any] | None:
    if path == "measurement" and key == "keys":
        return {
            "type": "array",
            "items": {"type": "string"},
        }
    return None


def _read_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for token in path.split("."):
        if not isinstance(current, dict):
            return None
        if token not in current:
            return None
        current = current[token]
    return current


def _is_sensitive_key(key: str) -> bool:
    key_lower = key.lower()
    return "token" in key_lower or "secret" in key_lower or "password" in key_lower


def _numeric_list_or_none(value: Any) -> list[float] | None:
    if not isinstance(value, list):
        return None
    parsed: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        parsed.append(float(item))
    return parsed
