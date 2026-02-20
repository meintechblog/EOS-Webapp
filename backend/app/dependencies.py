from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from app.core.config import Settings

if TYPE_CHECKING:
    from app.services.automap import AutomapService
    from app.services.data_pipeline import DataPipelineService
    from app.services.emr_pipeline import EmrPipelineService
    from app.services.eos_catalog import EosFieldCatalogService
    from app.services.eos_measurement_sync import EosMeasurementSyncService
    from app.services.eos_orchestrator import EosOrchestratorService
    from app.services.input_ingest import InputIngestPipelineService
    from app.services.mqtt_ingest import MqttIngestService
    from app.services.parameter_dynamic_catalog import ParameterDynamicCatalogService
    from app.services.parameter_dynamic_ingest import ParameterDynamicIngestService
    from app.services.parameter_profiles import ParameterProfileService
    from app.services.parameters_catalog import ParameterCatalogService
    from app.services.output_dispatch import OutputDispatchService
    from app.services.setup_fields import SetupFieldService
    from app.services.setup_checklist import SetupChecklistService


def get_settings_from_app(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise HTTPException(status_code=503, detail="Application settings are not initialized")
    return settings


def get_mqtt_service(request: Request) -> "MqttIngestService":
    service = getattr(request.app.state, "mqtt_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="MQTT service is not initialized")
    return service


def get_input_ingest_service(request: Request) -> "InputIngestPipelineService":
    service = getattr(request.app.state, "input_ingest_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Input ingest service is not initialized")
    return service


def get_eos_catalog_service(request: Request) -> "EosFieldCatalogService":
    service = getattr(request.app.state, "eos_catalog_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="EOS catalog service is not initialized")
    return service


def get_automap_service(request: Request) -> "AutomapService":
    service = getattr(request.app.state, "automap_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Automap service is not initialized")
    return service


def get_eos_orchestrator_service(request: Request) -> "EosOrchestratorService":
    service = getattr(request.app.state, "eos_orchestrator_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="EOS orchestrator service is not initialized")
    return service


def get_parameter_catalog_service(request: Request) -> "ParameterCatalogService":
    service = getattr(request.app.state, "parameter_catalog_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Parameter catalog service is not initialized")
    return service


def get_parameter_profile_service(request: Request) -> "ParameterProfileService":
    service = getattr(request.app.state, "parameter_profile_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Parameter profile service is not initialized")
    return service


def get_parameter_dynamic_catalog_service(request: Request) -> "ParameterDynamicCatalogService":
    service = getattr(request.app.state, "parameter_dynamic_catalog_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Parameter dynamic catalog service is not initialized")
    return service


def get_parameter_dynamic_service(request: Request) -> "ParameterDynamicIngestService":
    service = getattr(request.app.state, "parameter_dynamic_ingest_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Parameter dynamic ingest service is not initialized")
    return service


def get_setup_checklist_service(request: Request) -> "SetupChecklistService":
    service = getattr(request.app.state, "setup_checklist_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Setup checklist service is not initialized")
    return service


def get_setup_field_service(request: Request) -> "SetupFieldService":
    service = getattr(request.app.state, "setup_field_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Setup field service is not initialized")
    return service


def get_data_pipeline_service(request: Request) -> "DataPipelineService":
    service = getattr(request.app.state, "data_pipeline_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Data pipeline service is not initialized")
    return service


def get_emr_pipeline_service(request: Request) -> "EmrPipelineService":
    service = getattr(request.app.state, "emr_pipeline_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="EMR pipeline service is not initialized")
    return service


def get_eos_measurement_sync_service(request: Request) -> "EosMeasurementSyncService":
    service = getattr(request.app.state, "eos_measurement_sync_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="EOS measurement sync service is not initialized")
    return service


def get_output_dispatch_service(request: Request) -> "OutputDispatchService":
    service = getattr(request.app.state, "output_dispatch_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Output dispatch service is not initialized")
    return service
