from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import InputMapping
from app.schemas.mappings import MappingCreate, MappingUpdate


@dataclass(frozen=True)
class EnabledMappingSnapshot:
    id: int
    eos_field: str
    mqtt_topic: str
    payload_path: str | None
    unit: str | None


def list_mappings(db: Session) -> list[InputMapping]:
    return list(db.scalars(select(InputMapping).order_by(InputMapping.eos_field)))


def get_mapping_by_id(db: Session, mapping_id: int) -> InputMapping | None:
    return db.get(InputMapping, mapping_id)


def create_mapping(db: Session, payload: MappingCreate) -> InputMapping:
    mapping = InputMapping(
        eos_field=payload.eos_field,
        mqtt_topic=payload.mqtt_topic,
        payload_path=payload.payload_path,
        unit=payload.unit,
        enabled=payload.enabled,
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping


def update_mapping(db: Session, mapping: InputMapping, payload: MappingUpdate) -> InputMapping:
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(mapping, key, value)

    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping


def list_enabled_mapping_snapshots(db: Session) -> list[EnabledMappingSnapshot]:
    mappings = db.scalars(select(InputMapping).where(InputMapping.enabled.is_(True))).all()
    return [
        EnabledMappingSnapshot(
            id=mapping.id,
            eos_field=mapping.eos_field,
            mqtt_topic=mapping.mqtt_topic,
            payload_path=mapping.payload_path,
            unit=mapping.unit,
        )
        for mapping in mappings
    ]

