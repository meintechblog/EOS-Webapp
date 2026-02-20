from __future__ import annotations

import copy
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.repositories.parameter_profiles import (
    create_parameter_profile,
    create_profile_revision,
    get_active_parameter_profile,
    get_current_draft_revision,
    get_last_applied_revision,
    get_latest_revision,
    get_parameter_profile_by_id,
    get_parameter_profile_by_name,
    list_parameter_profiles,
    list_profile_revisions,
    mark_revision_as_last_applied,
    set_active_parameter_profile,
    update_parameter_profile,
    update_profile_revision_validation,
)
from app.schemas.parameters import ParameterStatusSnapshot
from app.services.eos_client import EosClient
from app.services.eos_settings_validation import (
    EosSettingsValidationService,
    MASKED_SECRET_PLACEHOLDER,
    ValidationOutcome,
)


class ParameterProfileService:
    EXPORT_FORMAT = "eos-webapp.parameters.v1"

    def __init__(
        self,
        *,
        settings: Settings,
        eos_client: EosClient,
        validation_service: EosSettingsValidationService,
    ):
        self._settings = settings
        self._eos_client = eos_client
        self._validation_service = validation_service
        self._status_lock = Lock()
        self._last_apply_error: str | None = None
        self._last_apply_ts: datetime | None = None

    def ensure_bootstrap_profile(self, db: Session) -> None:
        profiles = list_parameter_profiles(db)
        if profiles:
            return

        eos_config = self._safe_get_eos_config()
        normalized = self._normalize_payload_from_eos_for_profile(
            self._validation_service.sanitize_eos_config_output(eos_config)
        )

        profile = create_parameter_profile(
            db,
            name="Default",
            description="Automatisch aus aktueller EOS-Konfiguration erstellt.",
            is_active=True,
        )
        draft = create_profile_revision(
            db,
            profile_id=profile.id,
            source="bootstrap",
            payload_json=normalized,
            validation_status="valid",
            validation_issues_json={"warnings": []},
            set_current_draft=True,
        )
        mark_revision_as_last_applied(
            db,
            profile_id=profile.id,
            revision_id=draft.id,
            applied_at=datetime.now(timezone.utc),
        )
        self._set_last_apply_status(error_text=None)

    def list_profiles_summary(self, db: Session) -> list[dict[str, Any]]:
        self.ensure_bootstrap_profile(db)
        profiles = list_parameter_profiles(db)
        items: list[dict[str, Any]] = []
        for profile in profiles:
            draft = get_current_draft_revision(db, profile_id=profile.id)
            applied = get_last_applied_revision(db, profile_id=profile.id)
            items.append(
                {
                    "id": profile.id,
                    "name": profile.name,
                    "description": profile.description,
                    "is_active": profile.is_active,
                    "created_at": profile.created_at,
                    "updated_at": profile.updated_at,
                    "current_draft_revision_no": draft.revision_no if draft else None,
                    "last_applied_revision_no": applied.revision_no if applied else None,
                    "last_applied_at": applied.applied_at if applied else None,
                    "current_draft_validation_status": draft.validation_status if draft else None,
                }
            )
        return items

    def create_profile(
        self,
        db: Session,
        *,
        name: str,
        description: str | None,
        clone_from_profile_id: int | None,
    ) -> dict[str, Any]:
        self.ensure_bootstrap_profile(db)
        if get_parameter_profile_by_name(db, name) is not None:
            raise ValueError("Profile name already exists")

        source_payload = self._safe_get_eos_config()
        source_label = "eos_pull"
        if clone_from_profile_id is not None:
            clone_profile = get_parameter_profile_by_id(db, clone_from_profile_id)
            if clone_profile is None:
                raise ValueError("clone_from_profile_id not found")
            clone_draft = get_current_draft_revision(db, profile_id=clone_profile.id)
            if clone_draft is not None:
                source_payload = clone_draft.payload_json
                source_label = "manual"

        normalized = self._validation_service.sanitize_eos_config_output(source_payload)
        profile = create_parameter_profile(
            db,
            name=name,
            description=description,
            is_active=False,
        )
        create_profile_revision(
            db,
            profile_id=profile.id,
            source=(
                source_label
                if source_label in {"manual", "import", "bootstrap", "eos_pull", "dynamic_input"}
                else "manual"
            ),
            payload_json=normalized,
            validation_status="unknown",
            validation_issues_json=None,
            set_current_draft=True,
        )
        return self.get_profile_detail(db, profile.id)

    def update_profile(
        self,
        db: Session,
        *,
        profile_id: int,
        name: str | None,
        description: str | None,
        is_active: bool | None,
    ) -> dict[str, Any]:
        self.ensure_bootstrap_profile(db)
        profile = get_parameter_profile_by_id(db, profile_id)
        if profile is None:
            raise ValueError("Profile not found")

        if name is not None:
            existing = get_parameter_profile_by_name(db, name)
            if existing is not None and existing.id != profile.id:
                raise ValueError("Profile name already exists")

        updated = update_parameter_profile(
            db,
            profile,
            name=name,
            description=description,
            is_active=False if is_active is True else is_active,
        )
        if is_active is True:
            set_active_parameter_profile(db, updated.id)
        return self.get_profile_detail(db, updated.id)

    def get_profile_detail(self, db: Session, profile_id: int) -> dict[str, Any]:
        self.ensure_bootstrap_profile(db)
        profile = get_parameter_profile_by_id(db, profile_id)
        if profile is None:
            raise ValueError("Profile not found")

        draft = get_current_draft_revision(db, profile_id=profile.id)
        applied = get_last_applied_revision(db, profile_id=profile.id)
        revisions = list_profile_revisions(db, profile_id=profile.id, limit=30)

        summary = {
            "id": profile.id,
            "name": profile.name,
            "description": profile.description,
            "is_active": profile.is_active,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
            "current_draft_revision_no": draft.revision_no if draft else None,
            "last_applied_revision_no": applied.revision_no if applied else None,
            "last_applied_at": applied.applied_at if applied else None,
            "current_draft_validation_status": draft.validation_status if draft else None,
        }

        return {
            "profile": summary,
            "current_draft": _revision_to_dict(draft, include_payload=True) if draft else None,
            "last_applied": _revision_to_dict(applied, include_payload=True) if applied else None,
            "revisions": [_revision_to_dict(item, include_payload=False) for item in revisions],
        }

    def save_profile_draft(
        self,
        db: Session,
        *,
        profile_id: int,
        payload_json: dict[str, Any],
        source: str = "manual",
    ) -> dict[str, Any]:
        self.ensure_bootstrap_profile(db)
        profile = get_parameter_profile_by_id(db, profile_id)
        if profile is None:
            raise ValueError("Profile not found")

        normalized, sanitize_errors, sanitize_warnings = self._validation_service.sanitize_payload(
            payload_json,
            strict_unknown_fields=False,
        )
        validation_status = "invalid" if sanitize_errors else "unknown"
        issues = {
            "errors": sanitize_errors,
            "warnings": sanitize_warnings,
        }
        create_profile_revision(
            db,
            profile_id=profile.id,
            source=(
                source
                if source in {"manual", "import", "bootstrap", "eos_pull", "dynamic_input"}
                else "manual"
            ),
            payload_json=normalized,
            validation_status=validation_status,
            validation_issues_json=issues if (sanitize_errors or sanitize_warnings) else None,
            set_current_draft=True,
        )
        return self.get_profile_detail(db, profile.id)

    def validate_profile_draft(self, db: Session, *, profile_id: int) -> ValidationOutcome:
        self.ensure_bootstrap_profile(db)
        profile = get_parameter_profile_by_id(db, profile_id)
        if profile is None:
            raise ValueError("Profile not found")

        draft = get_current_draft_revision(db, profile_id=profile.id)
        if draft is None:
            raise ValueError("No draft revision available")

        outcome = self._validation_service.validate_payload(
            draft.payload_json,
            strict_unknown_fields=True,
            fail_on_masked_secrets=True,
        )
        update_profile_revision_validation(
            db,
            draft,
            validation_status="valid" if outcome.valid else "invalid",
            validation_issues_json={
                "errors": outcome.errors,
                "warnings": outcome.warnings,
            },
        )
        return outcome

    def validate_payload(
        self,
        *,
        payload_json: dict[str, Any],
        strict_unknown_fields: bool = True,
        fail_on_masked_secrets: bool = True,
    ) -> ValidationOutcome:
        return self._validation_service.validate_payload(
            payload_json,
            strict_unknown_fields=strict_unknown_fields,
            fail_on_masked_secrets=fail_on_masked_secrets,
        )

    def apply_profile(self, db: Session, *, profile_id: int, set_active_profile: bool = True) -> ValidationOutcome:
        self.ensure_bootstrap_profile(db)
        profile = get_parameter_profile_by_id(db, profile_id)
        if profile is None:
            raise ValueError("Profile not found")

        draft = get_current_draft_revision(db, profile_id=profile.id)
        if draft is None:
            raise ValueError("No draft revision available")

        outcome = self._validation_service.validate_payload(
            draft.payload_json,
            strict_unknown_fields=True,
            fail_on_masked_secrets=True,
        )
        update_profile_revision_validation(
            db,
            draft,
            validation_status="valid" if outcome.valid else "invalid",
            validation_issues_json={
                "errors": outcome.errors,
                "warnings": outcome.warnings,
            },
        )

        if not outcome.valid or outcome.normalized_payload is None:
            self._set_last_apply_status(error_text="Validation failed before apply")
            return outcome

        try:
            apply_payload, apply_warnings = self._prepare_payload_for_eos_apply(
                outcome.normalized_payload
            )
            self._eos_client.put_config(apply_payload)
            self._eos_client.save_config_file()
        except Exception as exc:
            self._set_last_apply_status(error_text=str(exc))
            return ValidationOutcome(
                valid=False,
                errors=[f"EOS apply failed: {exc}"],
                warnings=outcome.warnings,
                normalized_payload=outcome.normalized_payload,
            )

        effective_warnings = list(outcome.warnings)
        effective_warnings.extend(apply_warnings)

        applied = mark_revision_as_last_applied(
            db,
            profile_id=profile.id,
            revision_id=draft.id,
            applied_at=datetime.now(timezone.utc),
        )
        update_profile_revision_validation(
            db,
            applied,
            validation_status="valid",
            validation_issues_json={"errors": [], "warnings": effective_warnings},
        )
        if set_active_profile:
            set_active_parameter_profile(db, profile.id)
        self._set_last_apply_status(error_text=None)
        return ValidationOutcome(
            valid=True,
            errors=[],
            warnings=effective_warnings,
            normalized_payload=outcome.normalized_payload,
        )

    def export_profile(
        self,
        db: Session,
        *,
        profile_id: int,
        revision_selector: str,
        include_secrets: bool,
    ) -> dict[str, Any]:
        self.ensure_bootstrap_profile(db)
        profile = get_parameter_profile_by_id(db, profile_id)
        if profile is None:
            raise ValueError("Profile not found")

        if revision_selector not in {"draft", "applied"}:
            raise ValueError("revision must be 'draft' or 'applied'")

        revision = (
            get_current_draft_revision(db, profile_id=profile.id)
            if revision_selector == "draft"
            else get_last_applied_revision(db, profile_id=profile.id)
        )
        if revision is None:
            raise ValueError(f"No {revision_selector} revision available")

        payload = copy.deepcopy(revision.payload_json)
        masked = False
        if not include_secrets:
            payload = self._validation_service.mask_sensitive_values(payload)
            masked = True

        return {
            "format": self.EXPORT_FORMAT,
            "exported_at": datetime.now(timezone.utc),
            "profile": {
                "id": profile.id,
                "name": profile.name,
                "description": profile.description,
                "revision_no": revision.revision_no,
                "revision_id": revision.id,
                "revision": revision_selector,
            },
            "masked_secrets": masked,
            "payload": payload,
        }

    def preview_import(
        self,
        db: Session,
        *,
        profile_id: int,
        package_json: dict[str, Any],
    ) -> dict[str, Any]:
        self.ensure_bootstrap_profile(db)
        profile = get_parameter_profile_by_id(db, profile_id)
        if profile is None:
            raise ValueError("Profile not found")

        imported_payload = self._extract_import_payload(package_json)
        outcome = self._validation_service.validate_payload(
            imported_payload,
            strict_unknown_fields=True,
            fail_on_masked_secrets=True,
        )

        current_draft = get_current_draft_revision(db, profile_id=profile.id)
        before_payload = current_draft.payload_json if current_draft else {}
        diff = _dict_diff(before_payload, outcome.normalized_payload or {})
        return {
            "valid": outcome.valid,
            "errors": outcome.errors,
            "warnings": outcome.warnings,
            "diff": diff,
            "normalized_payload": outcome.normalized_payload,
        }

    def apply_import(
        self,
        db: Session,
        *,
        profile_id: int,
        package_json: dict[str, Any],
    ) -> dict[str, Any]:
        preview = self.preview_import(db, profile_id=profile_id, package_json=package_json)
        if not preview["valid"] or preview["normalized_payload"] is None:
            raise ValueError("Import package is invalid; run preview and resolve errors first")

        create_profile_revision(
            db,
            profile_id=profile_id,
            source="import",
            payload_json=preview["normalized_payload"],
            validation_status="valid",
            validation_issues_json={
                "errors": [],
                "warnings": preview["warnings"],
            },
            set_current_draft=True,
        )
        return self.get_profile_detail(db, profile_id)

    def get_status_snapshot(self, db: Session) -> ParameterStatusSnapshot:
        self.ensure_bootstrap_profile(db)
        active = get_active_parameter_profile(db)
        if active is None:
            return ParameterStatusSnapshot()

        draft = get_current_draft_revision(db, profile_id=active.id)
        applied = get_last_applied_revision(db, profile_id=active.id)
        with self._status_lock:
            return ParameterStatusSnapshot(
                active_profile_id=active.id,
                active_profile_name=active.name,
                current_draft_revision=draft.revision_no if draft else None,
                last_applied_revision=applied.revision_no if applied else None,
                last_apply_ts=self._last_apply_ts,
                last_apply_error=self._last_apply_error,
            )

    def _extract_import_payload(self, package_json: dict[str, Any]) -> dict[str, Any]:
        if "format" in package_json:
            fmt = package_json.get("format")
            if fmt != self.EXPORT_FORMAT:
                raise ValueError(
                    f"Unsupported import format '{fmt}'. Expected '{self.EXPORT_FORMAT}'."
                )
            payload = package_json.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("Import package payload must be an object")
            return payload

        if "payload" in package_json and isinstance(package_json["payload"], dict):
            return package_json["payload"]

        return package_json

    def _set_last_apply_status(self, *, error_text: str | None) -> None:
        with self._status_lock:
            self._last_apply_ts = datetime.now(timezone.utc)
            self._last_apply_error = error_text

    def _safe_get_eos_config(self) -> dict[str, Any]:
        try:
            config = self._eos_client.get_config()
            if isinstance(config, dict):
                return config
        except Exception:
            pass
        return {}

    def _prepare_payload_for_eos_apply(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        transformed = copy.deepcopy(payload)
        warnings: list[str] = []
        if not self._settings.eos_pv_akkudoktor_azimuth_workaround_enabled:
            return transformed, warnings

        pvforecast = transformed.get("pvforecast")
        if not isinstance(pvforecast, dict):
            return transformed, warnings
        provider = _safe_str(pvforecast.get("provider"))
        if provider != "PVForecastAkkudoktor":
            return transformed, warnings

        planes = pvforecast.get("planes")
        if not isinstance(planes, list):
            return transformed, warnings

        converted_any = False
        for idx, plane in enumerate(planes):
            if not isinstance(plane, dict):
                continue
            raw_azimuth = _coerce_float(plane.get("surface_azimuth"))
            if raw_azimuth is None:
                continue
            converted = _to_akkudoktor_internal_azimuth(raw_azimuth)
            plane["surface_azimuth"] = converted
            converted_any = True
            warnings.append(
                (
                    "PVForecastAkkudoktor azimuth workaround applied "
                    f"for plane #{idx + 1}: ui={raw_azimuth:g} -> eos={converted:g}"
                )
            )

        if not converted_any:
            return transformed, warnings

        planes_azimuth = pvforecast.get("planes_azimuth")
        if isinstance(planes_azimuth, list):
            for idx, value in enumerate(list(planes_azimuth)):
                numeric = _coerce_float(value)
                if numeric is None:
                    continue
                planes_azimuth[idx] = _to_akkudoktor_internal_azimuth(numeric)

        return transformed, warnings

    def _normalize_payload_from_eos_for_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = copy.deepcopy(payload)
        if not self._settings.eos_pv_akkudoktor_azimuth_workaround_enabled:
            return normalized

        pvforecast = normalized.get("pvforecast")
        if not isinstance(pvforecast, dict):
            return normalized
        provider = _safe_str(pvforecast.get("provider"))
        if provider != "PVForecastAkkudoktor":
            return normalized

        planes = pvforecast.get("planes")
        if isinstance(planes, list):
            for plane in planes:
                if not isinstance(plane, dict):
                    continue
                raw_azimuth = _coerce_float(plane.get("surface_azimuth"))
                if raw_azimuth is None:
                    continue
                plane["surface_azimuth"] = _from_akkudoktor_internal_azimuth(raw_azimuth)

        planes_azimuth = pvforecast.get("planes_azimuth")
        if isinstance(planes_azimuth, list):
            for idx, value in enumerate(list(planes_azimuth)):
                numeric = _coerce_float(value)
                if numeric is None:
                    continue
                planes_azimuth[idx] = _from_akkudoktor_internal_azimuth(numeric)
        return normalized


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_azimuth(value: float) -> float:
    normalized = value % 360.0
    if normalized < 0:
        normalized += 360.0
    return normalized


def _to_akkudoktor_internal_azimuth(user_facing_azimuth: float) -> float:
    normalized = _normalize_azimuth(user_facing_azimuth)
    if abs(normalized - 180.0) < 1e-9:
        # EOS/Akkudoktor currently forwards exact 180.0 as upstream azimuth=0 (invalid).
        # Keep the semantic "south" orientation but avoid the failing edge value.
        return 179.9
    return normalized


def _from_akkudoktor_internal_azimuth(internal_azimuth: float) -> float:
    normalized = _normalize_azimuth(internal_azimuth)
    if abs(normalized - 179.9) < 1e-6:
        return 180.0
    if abs(normalized - 360.0) < 1e-6:
        # Backward compatibility for already-applied workaround values.
        return 180.0
    return normalized


def _revision_to_dict(revision: Any, *, include_payload: bool) -> dict[str, Any]:
    data = {
        "id": revision.id,
        "revision_no": revision.revision_no,
        "source": revision.source,
        "validation_status": revision.validation_status,
        "validation_issues_json": revision.validation_issues_json,
        "is_current_draft": revision.is_current_draft,
        "is_last_applied": revision.is_last_applied,
        "created_at": revision.created_at,
        "applied_at": revision.applied_at,
    }
    if include_payload:
        data["payload_json"] = revision.payload_json
    return data


def _dict_diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    before_flat = _flatten_object(before)
    after_flat = _flatten_object(after)
    paths = sorted(set(before_flat) | set(after_flat))
    diff: list[dict[str, Any]] = []
    for path in paths:
        before_value = before_flat.get(path, _MISSING)
        after_value = after_flat.get(path, _MISSING)
        if before_value is _MISSING:
            diff.append(
                {
                    "path": path,
                    "before": None,
                    "after": after_value,
                    "change_type": "added",
                }
            )
            continue
        if after_value is _MISSING:
            diff.append(
                {
                    "path": path,
                    "before": before_value,
                    "after": None,
                    "change_type": "removed",
                }
            )
            continue
        if before_value != after_value:
            diff.append(
                {
                    "path": path,
                    "before": before_value,
                    "after": after_value,
                    "change_type": "changed",
                }
            )
    return diff


_MISSING = object()


def _flatten_object(payload: Any, *, path: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    if isinstance(payload, dict):
        if not payload and path:
            flattened[path] = {}
            return flattened
        for key, value in payload.items():
            child_path = f"{path}.{key}" if path else key
            flattened.update(_flatten_object(value, path=child_path))
        return flattened
    if isinstance(payload, list):
        if not payload and path:
            flattened[path] = []
            return flattened
        for idx, value in enumerate(payload):
            child_path = f"{path}.{idx}" if path else str(idx)
            flattened.update(_flatten_object(value, path=child_path))
        return flattened
    flattened[path] = payload
    return flattened
