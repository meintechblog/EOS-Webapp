from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from sqlalchemy.orm import Session

from app.api.data_backbone import router as data_backbone_router
from app.api.eos_fields import router as eos_fields_router
from app.api.eos_runtime import router as eos_runtime_router
from app.api.legacy_gone import router as legacy_gone_router
from app.api.parameters import router as parameters_router
from app.api.setup_fields import router as setup_fields_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal, check_db_connection, get_db
from app.repositories.mappings import list_mappings
from app.repositories.signal_backbone import infer_value_type, ingest_signal_measurement
from app.services.data_pipeline import DataPipelineService
from app.services.emr_pipeline import EmrPipelineService
from app.services.eos_catalog import EosFieldCatalogService
from app.services.eos_client import EosClient
from app.services.eos_measurement_sync import EosMeasurementSyncService
from app.services.eos_orchestrator import EosOrchestratorService
from app.services.eos_settings_validation import EosSettingsValidationService
from app.services.output_dispatch import OutputDispatchService
from app.services.parameter_profiles import ParameterProfileService
from app.services.parameters_catalog import ParameterCatalogService
from app.services.setup_fields import SetupFieldService

PROGRESS_FILE = Path("/data/progress.log")
WORKLOG_FILE = Path("/data/worklog.md")


def _tail(path: Path, lines: int = 30) -> list[str]:
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return content[-lines:]


def _canonical_unit_for_field(eos_field: str, unit: str | None) -> str | None:
    field = eos_field.strip().lower()
    if field.endswith("_w"):
        return "W"
    if field.endswith("_wh"):
        return "Wh"
    if field.endswith("_pct") or field.endswith("_percentage"):
        return "%"
    if "euro_pro_wh" in field:
        return "EUR/Wh"
    return unit


def _seed_fixed_mapping_signals() -> None:
    with SessionLocal() as db:
        for mapping in list_mappings(db):
            if not mapping.enabled or mapping.fixed_value is None:
                continue
            ingest_signal_measurement(
                db,
                signal_key=mapping.eos_field,
                label=mapping.eos_field,
                value_type=infer_value_type(mapping.fixed_value),
                canonical_unit=_canonical_unit_for_field(mapping.eos_field, mapping.unit),
                value=mapping.fixed_value,
                ts=datetime.now(timezone.utc),
                quality_status="ok",
                source_type="fixed_input",
                run_id=None,
                mapping_id=mapping.id,
                source_ref_id=None,
                tags_json={
                    "eos_field": mapping.eos_field,
                    "source": "fixed",
                },
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    emr_pipeline_service = EmrPipelineService(settings=settings, session_factory=SessionLocal)
    data_pipeline_service = DataPipelineService(settings=settings, session_factory=SessionLocal)
    eos_client = EosClient(base_url=settings.eos_base_url)
    eos_validation_service = EosSettingsValidationService(eos_client=eos_client)
    parameter_catalog_service = ParameterCatalogService(eos_client=eos_client)
    parameter_profile_service = ParameterProfileService(
        settings=settings,
        eos_client=eos_client,
        validation_service=eos_validation_service,
    )
    setup_field_service = SetupFieldService(
        settings=settings,
        session_factory=SessionLocal,
        parameter_profile_service=parameter_profile_service,
        parameter_catalog_service=parameter_catalog_service,
        emr_pipeline_service=emr_pipeline_service,
    )
    eos_catalog_service = EosFieldCatalogService(settings=settings)
    eos_measurement_sync_service = EosMeasurementSyncService(
        settings=settings,
        session_factory=SessionLocal,
        eos_client=eos_client,
    )
    eos_orchestrator_service = EosOrchestratorService(
        settings=settings,
        session_factory=SessionLocal,
        eos_client=eos_client,
        mqtt_service=None,
    )
    output_dispatch_service = OutputDispatchService(
        settings=settings,
        session_factory=SessionLocal,
    )

    app.state.settings = settings
    app.state.emr_pipeline_service = emr_pipeline_service
    app.state.data_pipeline_service = data_pipeline_service
    app.state.eos_client = eos_client
    app.state.eos_measurement_sync_service = eos_measurement_sync_service
    app.state.eos_orchestrator_service = eos_orchestrator_service
    app.state.output_dispatch_service = output_dispatch_service
    app.state.eos_settings_validation_service = eos_validation_service
    app.state.parameter_catalog_service = parameter_catalog_service
    app.state.parameter_profile_service = parameter_profile_service
    app.state.setup_field_service = setup_field_service
    app.state.eos_catalog_service = eos_catalog_service

    try:
        with SessionLocal() as bootstrap_db:
            parameter_profile_service.ensure_bootstrap_profile(bootstrap_db)
    except Exception:
        pass

    try:
        _seed_fixed_mapping_signals()
    except Exception:
        pass

    data_pipeline_service.start()
    eos_orchestrator_service.start()
    eos_measurement_sync_service.start()
    output_dispatch_service.start()
    try:
        yield
    finally:
        output_dispatch_service.stop()
        eos_measurement_sync_service.stop()
        eos_orchestrator_service.stop()
        data_pipeline_service.stop()


app = FastAPI(title="EOS-Webapp Backend", lifespan=lifespan)
app.include_router(eos_fields_router)
app.include_router(eos_runtime_router)
app.include_router(data_backbone_router)
app.include_router(parameters_router)
app.include_router(setup_fields_router)
app.include_router(legacy_gone_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "backend"}


@app.get("/status")
def status(request: Request, db: Session = Depends(get_db)):
    db_ok, db_error = check_db_connection(db)
    eos_orchestrator_service: EosOrchestratorService | None = getattr(
        request.app.state,
        "eos_orchestrator_service",
        None,
    )
    parameter_profile_service: ParameterProfileService | None = getattr(
        request.app.state,
        "parameter_profile_service",
        None,
    )
    data_pipeline_service: DataPipelineService | None = getattr(
        request.app.state,
        "data_pipeline_service",
        None,
    )
    emr_pipeline_service: EmrPipelineService | None = getattr(
        request.app.state,
        "emr_pipeline_service",
        None,
    )
    setup_field_service: SetupFieldService | None = getattr(
        request.app.state,
        "setup_field_service",
        None,
    )
    eos_measurement_sync_service: EosMeasurementSyncService | None = getattr(
        request.app.state,
        "eos_measurement_sync_service",
        None,
    )
    output_dispatch_service: OutputDispatchService | None = getattr(
        request.app.state,
        "output_dispatch_service",
        None,
    )
    settings: Settings | None = getattr(request.app.state, "settings", None)

    db_status: dict[str, object] = {"ok": db_ok}
    if db_error:
        db_status["error"] = db_error

    mqtt_status: dict[str, object] = {
        "enabled": False,
        "input_ingest": "disabled_http_only",
        "output_dispatch": "disabled_http_only",
    }

    eos_status: dict[str, object]
    collector_status: dict[str, object]
    if eos_orchestrator_service is None:
        eos_status = {"available": False, "health_ok": False}
        collector_status = {"running": False, "error": "EOS orchestrator service not initialized"}
    else:
        runtime_snapshot = eos_orchestrator_service.get_runtime_snapshot()
        health_payload = runtime_snapshot.get("health_payload")
        eos_last_run_datetime: str | None = None
        if isinstance(health_payload, dict):
            energy_management = health_payload.get("energy-management")
            if isinstance(energy_management, dict):
                last_run_raw = energy_management.get("last_run_datetime")
                if isinstance(last_run_raw, str):
                    eos_last_run_datetime = last_run_raw
        eos_status = {
            "available": True,
            "base_url": settings.eos_base_url if settings else None,
            "health_ok": bool(runtime_snapshot.get("health_ok", False)),
            "last_run_datetime": eos_last_run_datetime,
        }
        collector_status = runtime_snapshot.get("collector", {})

    if parameter_profile_service is None:
        parameters_status = {
            "single_state": False,
            "error": "Parameter profile service not initialized",
        }
    else:
        parameters_status = parameter_profile_service.get_status_snapshot(db).model_dump()
        parameters_status["single_state"] = True

    if data_pipeline_service is None:
        data_pipeline_status = {
            "running": False,
            "last_error": "Data pipeline service not initialized",
            "last_rollup_run": None,
            "last_retention_run": None,
            "raw_rows_24h": 0,
            "rollup_rows_24h": 0,
            "signal_catalog_count": 0,
        }
    else:
        data_pipeline_status = data_pipeline_service.get_status_snapshot(db)

    if emr_pipeline_service is None:
        emr_status = {
            "enabled": False,
            "tracked_keys": [],
            "last_emr_update_ts": None,
            "last_error": "EMR pipeline service not initialized",
        }
    else:
        emr_status = emr_pipeline_service.get_status_snapshot()

    if eos_measurement_sync_service is None:
        measurement_sync_status = {
            "enabled": False,
            "running": False,
            "sync_seconds": None,
            "next_due_ts": None,
            "force_in_progress": False,
            "last_run_id": None,
            "last_status": None,
            "last_error": "EOS measurement sync service not initialized",
            "last_run": None,
        }
    else:
        measurement_sync_status = eos_measurement_sync_service.get_status_snapshot(db)

    if output_dispatch_service is None:
        output_dispatch_status = {
            "enabled": False,
            "running": False,
            "tick_seconds": None,
            "heartbeat_seconds": None,
            "last_tick_ts": None,
            "last_status": None,
            "last_error": "Output dispatch service not initialized",
            "last_run_id": None,
            "next_heartbeat_ts": None,
            "force_in_progress": False,
        }
    else:
        output_dispatch_status = output_dispatch_service.get_status_snapshot()

    if setup_field_service is None:
        setup_status = {
            "last_check_ts": None,
            "readiness_level": "blocked",
            "blockers_count": 1,
            "warnings_count": 0,
            "items": [],
        }
    else:
        readiness = setup_field_service.readiness(db).model_dump()
        setup_status = {
            "last_check_ts": datetime.now(timezone.utc).isoformat(),
            **readiness,
        }

    return {
        "status": "working",
        "service": "backend",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "progress_tail": _tail(PROGRESS_FILE, 40),
        "worklog_tail": _tail(WORKLOG_FILE, 40),
        "db": db_status,
        "mqtt": mqtt_status,
        "eos": eos_status,
        "collector": collector_status,
        "parameters": parameters_status,
        "setup": setup_status,
        "emr": emr_status,
        "eos_measurement_sync": measurement_sync_status,
        "output_dispatch": output_dispatch_status,
        "data_pipeline": {
            "last_rollup_run": data_pipeline_status.get("last_rollup_run"),
            "last_retention_run": data_pipeline_status.get("last_retention_run"),
            "raw_rows_24h": data_pipeline_status.get("raw_rows_24h"),
            "rollup_rows_24h": data_pipeline_status.get("rollup_rows_24h"),
            "signal_catalog_count": data_pipeline_status.get("signal_catalog_count"),
        },
        "config": {
            "live_stale_seconds": settings.live_stale_seconds if settings else None,
            "setup_check_live_stale_seconds": (
                settings.setup_check_live_stale_seconds if settings else None
            ),
            "http_override_active_seconds": (
                settings.http_override_active_seconds if settings else None
            ),
            "eos_sync_poll_seconds": settings.eos_sync_poll_seconds if settings else None,
            "eos_autoconfig_mode": settings.eos_autoconfig_mode if settings else None,
            "eos_autoconfig_interval_seconds": (
                settings.eos_autoconfig_interval_seconds if settings else None
            ),
            "eos_actuation_enabled": settings.eos_actuation_enabled if settings else None,
            "data_raw_retention_days": settings.data_raw_retention_days if settings else None,
            "data_rollup_5m_retention_days": settings.data_rollup_5m_retention_days if settings else None,
            "data_rollup_1h_retention_days": settings.data_rollup_1h_retention_days if settings else None,
            "data_rollup_1d_retention_days": settings.data_rollup_1d_retention_days if settings else None,
            "eos_artifact_raw_retention_days": settings.eos_artifact_raw_retention_days if settings else None,
            "data_rollup_job_seconds": settings.data_rollup_job_seconds if settings else None,
            "data_retention_job_seconds": settings.data_retention_job_seconds if settings else None,
            "emr_enabled": settings.emr_enabled if settings else None,
            "eos_measurement_sync_enabled": (
                settings.eos_measurement_sync_enabled if settings else None
            ),
            "eos_measurement_sync_seconds": (
                settings.eos_measurement_sync_seconds if settings else None
            ),
            "output_http_dispatch_enabled": (
                settings.output_http_dispatch_enabled if settings else None
            ),
            "output_scheduler_tick_seconds": (
                settings.output_scheduler_tick_seconds if settings else None
            ),
            "output_heartbeat_seconds": (
                settings.output_heartbeat_seconds if settings else None
            ),
        },
    }

