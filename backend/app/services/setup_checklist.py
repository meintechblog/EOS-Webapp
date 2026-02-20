from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import EosRun
from app.repositories.mappings import list_mappings
from app.repositories.parameter_profiles import (
    get_active_parameter_profile,
    get_current_draft_revision,
    get_last_applied_revision,
)
from app.repositories.signal_backbone import list_latest_by_signal_keys
from app.services.eos_client import EosClient


class SetupChecklistService:
    _REQUIRED_SIGNAL_KEYS = ["house_load_w", "pv_power_w", "grid_import_w", "grid_export_w"]
    _LEGACY_PARAMETER_FIELDS = {
        "einspeiseverguetung_euro_pro_wh",
        "strompreis_euro_pro_wh",
        "preis_euro_pro_wh_akku",
    }

    def __init__(self, *, settings: Settings, eos_client: EosClient) -> None:
        self._settings = settings
        self._eos_client = eos_client
        self._status_lock = Lock()
        self._last_snapshot: dict[str, Any] | None = None

    def build_checklist(self, db: Session) -> dict[str, Any]:
        items: list[dict[str, Any]] = []

        items.append(self._check_eos_reachable())
        active_profile = get_active_parameter_profile(db)
        if active_profile is None:
            items.append(
                _item(
                    key="active_profile",
                    required=True,
                    status="blocked",
                    message="Kein aktives Parameter-Profil gesetzt.",
                    action_hint="Profil wählen und als aktiv markieren.",
                )
            )
            draft_revision = None
        else:
            items.append(
                _item(
                    key="active_profile",
                    required=True,
                    status="ok",
                    message=f"Aktives Profil: {active_profile.name} (ID {active_profile.id}).",
                )
            )
            draft_revision = get_current_draft_revision(db, profile_id=active_profile.id)

        if draft_revision is None:
            items.append(
                _item(
                    key="draft_valid",
                    required=True,
                    status="blocked",
                    message="Kein aktueller Draft im aktiven Profil vorhanden.",
                    action_hint="Draft speichern und strict validieren.",
                )
            )
            draft_payload: dict[str, Any] = {}
        else:
            draft_payload = draft_revision.payload_json if isinstance(draft_revision.payload_json, dict) else {}
            if draft_revision.validation_status == "valid":
                status = "ok"
                action_hint = None
                message = f"Draft Revision #{draft_revision.revision_no} ist valid."
            elif draft_revision.validation_status == "invalid":
                status = "blocked"
                action_hint = "Draft korrigieren und erneut strict validieren."
                message = f"Draft Revision #{draft_revision.revision_no} ist invalid."
            else:
                status = "warning"
                action_hint = "Draft strict validieren, bevor ein Run gestartet wird."
                message = f"Draft Revision #{draft_revision.revision_no} ist noch nicht strict validiert."
            items.append(
                _item(
                    key="draft_valid",
                    required=True,
                    status=status,
                    message=message,
                    action_hint=action_hint,
                )
            )

        if active_profile is None:
            items.append(
                _item(
                    key="last_applied",
                    required=True,
                    status="blocked",
                    message="Ohne aktives Profil gibt es keinen letzten Apply-Stand.",
                    action_hint="Profil aktiv setzen und auf EOS anwenden.",
                )
            )
        else:
            last_applied = get_last_applied_revision(db, profile_id=active_profile.id)
            if last_applied is None:
                items.append(
                    _item(
                        key="last_applied",
                        required=True,
                        status="warning",
                        message="Es gibt noch keinen erfolgreichen Apply in EOS.",
                        action_hint="Nach erfolgreicher Validierung auf EOS anwenden.",
                    )
                )
            else:
                items.append(
                    _item(
                        key="last_applied",
                        required=True,
                        status="ok",
                        message=(
                            f"Letzte Applied Revision: #{last_applied.revision_no} "
                            f"({last_applied.applied_at.isoformat() if last_applied.applied_at else 'ohne Timestamp'})."
                        ),
                    )
                )

        items.append(self._check_measurement_config(draft_payload))
        items.append(self._check_live_signals(db))
        items.append(self._check_legacy_overlap(db))
        items.append(self._check_last_run_errors(db))

        blockers = len([item for item in items if item["status"] == "blocked" and item["required"]])
        warnings = len([item for item in items if item["status"] == "warning"])
        readiness = "ready"
        if blockers > 0:
            readiness = "blocked"
        elif warnings > 0:
            readiness = "degraded"

        snapshot = {
            "last_check_ts": datetime.now(timezone.utc),
            "readiness_level": readiness,
            "blockers_count": blockers,
            "warnings_count": warnings,
            "items": items,
        }
        with self._status_lock:
            self._last_snapshot = snapshot
        return snapshot

    def get_status_snapshot(self, db: Session) -> dict[str, Any]:
        with self._status_lock:
            if self._last_snapshot is not None:
                return self._last_snapshot
        return self.build_checklist(db)

    def _check_eos_reachable(self) -> dict[str, Any]:
        try:
            health = self._eos_client.get_health()
            if isinstance(health.payload, dict):
                return _item(
                    key="eos_reachable",
                    required=True,
                    status="ok",
                    message="EOS API ist erreichbar.",
                )
            return _item(
                key="eos_reachable",
                required=True,
                status="warning",
                message="EOS API antwortet, liefert aber unerwartetes Payload.",
                action_hint="EOS-Container und Logs prüfen.",
            )
        except Exception as exc:
            return _item(
                key="eos_reachable",
                required=True,
                status="blocked",
                message=f"EOS API nicht erreichbar: {exc}",
                action_hint="EOS-Basis-URL und Container-Status prüfen.",
            )

    def _check_measurement_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        measurement = payload.get("measurement")
        if not isinstance(measurement, dict):
            return _item(
                key="measurement_config",
                required=True,
                status="blocked",
                message="measurement-Abschnitt fehlt im Draft.",
                action_hint="Measurement Keys und EMR Keys im Parameters-Bereich pflegen.",
            )

        keys = measurement.get("keys")
        if not isinstance(keys, list) or len([item for item in keys if isinstance(item, str) and item.strip() != ""]) == 0:
            return _item(
                key="measurement_config",
                required=True,
                status="blocked",
                message="measurement.keys fehlt oder ist leer.",
                action_hint="measurement.keys mit W- und EMR-Keys befüllen.",
            )

        required_emr_fields = [
            "load_emr_keys",
            "grid_import_emr_keys",
            "grid_export_emr_keys",
            "pv_production_emr_keys",
        ]
        for field_name in required_emr_fields:
            field_value = measurement.get(field_name)
            if not isinstance(field_value, list) or len(field_value) == 0:
                return _item(
                    key="measurement_config",
                    required=True,
                    status="warning",
                    message=f"measurement.{field_name} ist nicht gesetzt.",
                    action_hint="EMR-Keylisten prüfen.",
                )

        return _item(
            key="measurement_config",
            required=True,
            status="ok",
            message="Measurement/EMR-Konfiguration ist vorhanden.",
        )

    def _check_live_signals(self, db: Session) -> dict[str, Any]:
        rows = list_latest_by_signal_keys(db, signal_keys=self._REQUIRED_SIGNAL_KEYS)
        now = datetime.now(timezone.utc)
        stale_limit = max(1, self._settings.setup_check_live_stale_seconds)

        seen: dict[str, datetime | None] = {}
        for row in rows:
            key = str(row.get("signal_key"))
            ts = row.get("last_ts")
            seen[key] = ts if isinstance(ts, datetime) else None

        missing = [key for key in self._REQUIRED_SIGNAL_KEYS if key not in seen or seen[key] is None]
        if missing:
            return _item(
                key="live_inputs",
                required=True,
                status="warning",
                message=f"Keine Livewerte für: {', '.join(missing)}.",
                action_hint="Input-Mappings prüfen und aktuelle Werte zuspielen.",
            )

        stale = [
            key
            for key, ts in seen.items()
            if ts is None or (now - _to_utc(ts)).total_seconds() > stale_limit
        ]
        if stale:
            return _item(
                key="live_inputs",
                required=True,
                status="warning",
                message=f"Livewerte veraltet für: {', '.join(stale)}.",
                action_hint=f"Frische Werte innerhalb von {stale_limit}s sicherstellen.",
            )

        return _item(
            key="live_inputs",
            required=True,
            status="ok",
            message="Kern-Live-Signale sind vorhanden und frisch.",
        )

    def _check_legacy_overlap(self, db: Session) -> dict[str, Any]:
        overlaps = [
            mapping.eos_field
            for mapping in list_mappings(db)
            if mapping.eos_field in self._LEGACY_PARAMETER_FIELDS and mapping.enabled
        ]
        if not overlaps:
            return _item(
                key="legacy_overlap",
                required=False,
                status="ok",
                message="Keine aktiven Legacy/Override-Mappings erkannt.",
            )

        return _item(
            key="legacy_overlap",
            required=False,
            status="warning",
            message=f"Legacy/Override-Mappings aktiv: {', '.join(overlaps)}.",
            action_hint="Nur behalten, wenn ein Override bewusst gewünscht ist.",
        )

    def _check_last_run_errors(self, db: Session) -> dict[str, Any]:
        latest_run = db.execute(
            select(EosRun).order_by(EosRun.started_at.desc()).limit(1)
        ).scalar_one_or_none()
        if latest_run is None:
            return _item(
                key="run_health",
                required=False,
                status="warning",
                message="Noch kein EOS-Run protokolliert.",
                action_hint="Force-Run ausführen und Ergebnis prüfen.",
            )

        if latest_run.error_text:
            return _item(
                key="run_health",
                required=False,
                status="warning",
                message=f"Letzter Run #{latest_run.id} enthält Fehler: {latest_run.error_text}",
                action_hint="Run-Details/Artifacts prüfen und fehlende Daten ergänzen.",
            )

        return _item(
            key="run_health",
            required=False,
            status="ok",
            message=f"Letzter Run #{latest_run.id} ohne Fehlertext.",
        )


def _item(
    *,
    key: str,
    required: bool,
    status: str,
    message: str,
    action_hint: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "required": required,
        "status": status,
        "message": message,
        "action_hint": action_hint,
    }


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
