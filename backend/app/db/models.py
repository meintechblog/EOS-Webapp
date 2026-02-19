from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class InputMapping(Base):
    __tablename__ = "input_mappings"
    __table_args__ = (
        UniqueConstraint("eos_field", name="uq_input_mappings_eos_field"),
        UniqueConstraint("mqtt_topic", name="uq_input_mappings_mqtt_topic"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    eos_field: Mapped[str] = mapped_column(String(128), nullable=False)
    mqtt_topic: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    telemetry_events: Mapped[list["TelemetryEvent"]] = relationship(
        back_populates="mapping",
        cascade="all, delete-orphan",
    )


class TelemetryEvent(Base):
    __tablename__ = "telemetry_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    mapping_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("input_mappings.id", ondelete="CASCADE"),
        nullable=False,
    )
    eos_field: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    mapping: Mapped[InputMapping] = relationship(back_populates="telemetry_events")

