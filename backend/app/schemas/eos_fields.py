from pydantic import BaseModel, Field


class EosFieldOption(BaseModel):
    eos_field: str
    label: str
    description: str | None = None
    suggested_units: list[str] = Field(default_factory=list)
    info_notes: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
