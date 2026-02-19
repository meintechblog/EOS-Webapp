from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from app.core.config import Settings

if TYPE_CHECKING:
    from app.services.eos_catalog import EosFieldCatalogService
    from app.services.mqtt_ingest import MqttIngestService


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


def get_eos_catalog_service(request: Request) -> "EosFieldCatalogService":
    service = getattr(request.app.state, "eos_catalog_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="EOS catalog service is not initialized")
    return service
