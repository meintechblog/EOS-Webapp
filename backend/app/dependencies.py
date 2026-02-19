from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from app.core.config import Settings

if TYPE_CHECKING:
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

