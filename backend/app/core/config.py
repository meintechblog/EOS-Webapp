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
    mqtt_discovery_topic: str = Field(default="eos/#")
    mqtt_discovery_active_seconds: int = Field(default=30, ge=1, le=3600)
    live_stale_seconds: int = Field(default=120, ge=1)
    eos_base_url: str = Field(default="http://eos:8503")
    eos_sync_poll_seconds: int = Field(default=30, ge=5, le=3600)
    eos_autoconfig_enable: bool = Field(default=True)
    eos_autoconfig_mode: str = Field(default="OPTIMIZATION")
    eos_autoconfig_interval_seconds: int = Field(default=900, ge=1, le=86400)
    eos_aligned_scheduler_enabled: bool = Field(default=True)
    eos_aligned_scheduler_minutes: str = Field(default="0,15,30,45")
    eos_aligned_scheduler_delay_seconds: int = Field(default=1, ge=0, le=59)
    eos_aligned_scheduler_base_interval_seconds: int = Field(default=86400, ge=60, le=86400)
    eos_force_run_timeout_seconds: int = Field(default=240, ge=10, le=1800)
    eos_force_run_allow_legacy: bool = Field(default=True)
    eos_force_run_pre_refresh_enabled: bool = Field(default=True)
    eos_force_run_pre_refresh_scope: str = Field(default="all")
    eos_prediction_pv_import_fallback_enabled: bool = Field(default=True)
    eos_prediction_pv_import_provider: str = Field(default="PVForecastImport")
    eos_pv_akkudoktor_azimuth_workaround_enabled: bool = Field(default=True)
    eos_feedin_spot_mirror_enabled: bool = Field(default=True)
    eos_output_mqtt_enabled: bool = Field(default=False)
    eos_output_mqtt_prefix: str = Field(default="eos/output")
    eos_output_mqtt_qos: int = Field(default=1, ge=0, le=2)
    eos_output_mqtt_retain: bool = Field(default=False)
    eos_actuation_enabled: bool = Field(default=False)
    output_http_dispatch_enabled: bool = Field(default=True)
    output_scheduler_tick_seconds: int = Field(default=15, ge=1, le=3600)
    output_heartbeat_seconds: int = Field(default=60, ge=1, le=3600)
    eos_no_grid_charge_guard_enabled: bool = Field(default=True)
    eos_no_grid_charge_guard_threshold_w: float = Field(default=50.0, ge=0.0)
    data_raw_retention_days: int = Field(default=35, ge=1, le=36500)
    data_rollup_5m_retention_days: int = Field(default=400, ge=1, le=36500)
    data_rollup_1h_retention_days: int = Field(default=1825, ge=1, le=36500)
    data_rollup_1d_retention_days: int = Field(default=0, ge=0, le=36500)
    eos_artifact_raw_retention_days: int = Field(default=180, ge=1, le=36500)
    data_rollup_job_seconds: int = Field(default=300, ge=30, le=86400)
    data_retention_job_seconds: int = Field(default=3600, ge=60, le=86400)
    emr_enabled: bool = Field(default=True)
    emr_hold_max_seconds: int = Field(default=300, ge=1, le=86400)
    emr_delta_min_seconds: int = Field(default=1, ge=0, le=3600)
    emr_delta_max_seconds: int = Field(default=3600, ge=1, le=86400)
    emr_power_min_w: float = Field(default=0.0, ge=0.0)
    emr_power_max_w: float = Field(default=50000.0, gt=0.0)
    emr_house_power_max_w: float = Field(default=60000.0, gt=0.0)
    emr_pv_power_max_w: float = Field(default=60000.0, gt=0.0)
    emr_grid_power_max_w: float = Field(default=60000.0, gt=0.0)
    emr_battery_power_min_w: float = Field(default=-25000.0, lt=0.0)
    emr_battery_power_max_w: float = Field(default=25000.0, gt=0.0)
    emr_grid_conflict_threshold_w: float = Field(default=50.0, ge=0.0)
    eos_measurement_sync_enabled: bool = Field(default=True)
    eos_measurement_sync_seconds: int = Field(default=30, ge=5, le=3600)
    eos_measurement_sync_force_timeout_seconds: int = Field(default=20, ge=1, le=600)
    param_dynamic_enabled: bool = Field(default=True)
    param_dynamic_apply_debounce_seconds: int = Field(default=2, ge=1, le=300)
    param_dynamic_allow_http: bool = Field(default=True)
    param_dynamic_allow_mqtt: bool = Field(default=False)
    setup_check_live_stale_seconds: int = Field(default=120, ge=1, le=3600)
    http_override_active_seconds: int = Field(default=120, ge=1, le=3600)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
