from fastapi import APIRouter, Depends

from app.dependencies import get_eos_catalog_service
from app.schemas.eos_fields import EosFieldOption
from app.services.eos_catalog import EosFieldCatalogService


router = APIRouter(prefix="/api", tags=["eos-fields"])


@router.get("/eos-fields", response_model=list[EosFieldOption])
def get_eos_fields(
    catalog_service: EosFieldCatalogService = Depends(get_eos_catalog_service),
) -> list[EosFieldOption]:
    entries = catalog_service.list_fields()
    return [
        EosFieldOption(
            eos_field=entry.eos_field,
            label=entry.label,
            description=entry.description,
            suggested_units=entry.suggested_units,
            info_notes=entry.info_notes,
            sources=entry.sources,
        )
        for entry in entries
    ]
