from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import InputMapping
from app.db.session import get_db
from app.dependencies import get_mqtt_service
from app.repositories.mappings import create_mapping, get_mapping_by_id, list_mappings, update_mapping
from app.schemas.mappings import MappingCreate, MappingResponse, MappingUpdate
from app.services.mqtt_ingest import MqttIngestService


router = APIRouter(prefix="/api", tags=["mappings"])


def _raise_conflict(exc: IntegrityError) -> None:
    detail = "Mapping violates unique constraints"
    message = str(getattr(exc, "orig", exc)).lower()
    if "eos_field" in message:
        detail = "Mapping with this eos_field already exists"
    elif "mqtt_topic" in message:
        detail = "Mapping with this mqtt_topic already exists"

    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


@router.get("/mappings", response_model=list[MappingResponse])
def get_all_mappings(db: Session = Depends(get_db)) -> list[InputMapping]:
    return list_mappings(db)


@router.post("/mappings", response_model=MappingResponse, status_code=status.HTTP_201_CREATED)
def create_mapping_endpoint(
    payload: MappingCreate,
    db: Session = Depends(get_db),
    mqtt_service: MqttIngestService = Depends(get_mqtt_service),
) -> InputMapping:
    try:
        mapping = create_mapping(db, payload)
    except IntegrityError as exc:
        db.rollback()
        _raise_conflict(exc)

    mqtt_service.sync_subscriptions_from_db()
    return mapping


@router.put("/mappings/{mapping_id}", response_model=MappingResponse)
def update_mapping_endpoint(
    mapping_id: int,
    payload: MappingUpdate,
    db: Session = Depends(get_db),
    mqtt_service: MqttIngestService = Depends(get_mqtt_service),
) -> InputMapping:
    mapping = get_mapping_by_id(db, mapping_id)
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")

    try:
        updated = update_mapping(db, mapping, payload)
    except IntegrityError as exc:
        db.rollback()
        _raise_conflict(exc)

    mqtt_service.sync_subscriptions_from_db()
    return updated

