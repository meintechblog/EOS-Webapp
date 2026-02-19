from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field(
        default="postgresql+psycopg://eos_webapp:eos_webapp_dev@postgres:5432/eos_webapp"
    )
    mqtt_broker_host: str = Field(default="192.168.3.8")
    mqtt_broker_port: int = Field(default=1883, ge=1, le=65535)
    mqtt_client_id: str = Field(default="eos-webapp-backend")
    mqtt_qos: int = Field(default=0, ge=0, le=2)
    live_stale_seconds: int = Field(default=120, ge=1)
    eos_base_url: str = Field(default="http://eos:8503")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
