from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


class MappingCreate(BaseModel):
    eos_field: str = Field(min_length=1, max_length=128)
    mqtt_topic: str = Field(min_length=1, max_length=255)
    payload_path: str | None = Field(default=None, max_length=255)
    unit: str | None = Field(default=None, max_length=32)
    enabled: bool = True

    @field_validator("eos_field", "mqtt_topic", "payload_path", "unit", mode="before")
    @classmethod
    def _normalize_text_fields(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class MappingUpdate(BaseModel):
    eos_field: str | None = Field(default=None, max_length=128)
    mqtt_topic: str | None = Field(default=None, max_length=255)
    payload_path: str | None = Field(default=None, max_length=255)
    unit: str | None = Field(default=None, max_length=32)
    enabled: bool | None = None

    @field_validator("eos_field", "mqtt_topic", "payload_path", "unit", mode="before")
    @classmethod
    def _normalize_text_fields(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def _validate_update_payload(self) -> "MappingUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided for update")
        if "enabled" in self.model_fields_set and self.enabled is None:
            raise ValueError("enabled must be true or false")
        return self


class MappingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    eos_field: str
    mqtt_topic: str
    payload_path: str | None
    unit: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime

