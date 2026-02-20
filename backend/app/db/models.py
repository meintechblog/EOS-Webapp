from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class InputMapping(Base):
    __tablename__ = "input_mappings"
    __table_args__ = (
        UniqueConstraint("eos_field", name="uq_input_mappings_eos_field"),
        Index(
            "uq_input_mappings_channel_topic",
            "channel_id",
            "mqtt_topic",
            unique=True,
            postgresql_where=text("mqtt_topic IS NOT NULL"),
        ),
        CheckConstraint(
            "sign_convention IN ('canonical','unknown','positive_is_import','positive_is_export')",
            name="ck_input_mappings_sign_convention",
        ),
        CheckConstraint(
            "("
            "(fixed_value IS NULL AND mqtt_topic IS NOT NULL AND channel_id IS NOT NULL)"
            " OR "
            "(fixed_value IS NOT NULL AND mqtt_topic IS NULL AND channel_id IS NULL)"
            ")",
            name="ck_input_mappings_value_source",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    eos_field: Mapped[str] = mapped_column(String(128), nullable=False)
    mqtt_topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("input_channels.id", ondelete="RESTRICT"),
    )
    fixed_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timestamp_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    value_multiplier: Mapped[float] = mapped_column(
        Float,
        default=1.0,
        server_default="1.0",
        nullable=False,
    )
    sign_convention: Mapped[str] = mapped_column(
        String(32),
        default="canonical",
        server_default="canonical",
        nullable=False,
    )
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
    channel: Mapped["InputChannel | None"] = relationship(back_populates="mappings")

    @property
    def input_key(self) -> str | None:
        return self.mqtt_topic

    @property
    def channel_code(self) -> str | None:
        if self.channel is None:
            return None
        return self.channel.code

    @property
    def channel_type(self) -> str | None:
        if self.channel is None:
            return None
        return self.channel.channel_type


class InputChannel(Base):
    __tablename__ = "input_channels"
    __table_args__ = (
        UniqueConstraint("code", name="uq_input_channels_code"),
        CheckConstraint(
            "channel_type IN ('mqtt','http')",
            name="ck_input_channels_type",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    config_json: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="'{}'",
    )
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

    mappings: Mapped[list[InputMapping]] = relationship(back_populates="channel")
    observations: Mapped[list["InputObservation"]] = relationship(
        back_populates="channel",
        cascade="all, delete-orphan",
    )
    parameter_bindings: Mapped[list["ParameterBinding"]] = relationship(
        back_populates="channel",
        cascade="all, delete-orphan",
    )
    parameter_input_events: Mapped[list["ParameterInputEvent"]] = relationship(
        back_populates="channel",
    )


class InputObservation(Base):
    __tablename__ = "input_observations"
    __table_args__ = (
        UniqueConstraint("channel_id", "input_key", name="uq_input_observations_channel_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("input_channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    input_key: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_key: Mapped[str] = mapped_column(String(255), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_count: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="1",
        default=1,
    )
    last_meta_json: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="'{}'",
    )

    channel: Mapped[InputChannel] = relationship(back_populates="observations")


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


class MqttTopicObservation(Base):
    __tablename__ = "mqtt_topic_observations"
    __table_args__ = (UniqueConstraint("mqtt_topic", name="uq_mqtt_topic_observations_topic"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    mqtt_topic: Mapped[str] = mapped_column(String(255), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_count: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="1",
        default=1,
    )
    last_retain: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        default=False,
    )
    last_qos: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default="0",
        default=0,
    )


class EosRun(Base):
    __tablename__ = "eos_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    trigger_source: Mapped[str] = mapped_column(String(32), nullable=False)
    run_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    eos_last_run_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    artifacts: Mapped[list["EosArtifact"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    prediction_points: Mapped[list["EosPredictionPoint"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    plan_instructions: Mapped[list["EosPlanInstruction"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    mqtt_output_events: Mapped[list["EosMqttOutputEvent"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    output_dispatch_events: Mapped[list["OutputDispatchEvent"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class EosArtifact(Base):
    __tablename__ = "eos_artifacts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("eos_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict | list] = mapped_column(JSONB, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    run: Mapped[EosRun] = relationship(back_populates="artifacts")


class EosPredictionPoint(Base):
    __tablename__ = "eos_prediction_points"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("eos_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    prediction_key: Mapped[str] = mapped_column(String(128), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    run: Mapped[EosRun] = relationship(back_populates="prediction_points")


class EosPlanInstruction(Base):
    __tablename__ = "eos_plan_instructions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("eos_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    instruction_index: Mapped[int] = mapped_column(BigInteger, nullable=False)
    instruction_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(128))
    actuator_id: Mapped[str | None] = mapped_column(String(128))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_mode_id: Mapped[str | None] = mapped_column(String(64))
    operation_mode_factor: Mapped[float | None] = mapped_column(Float)
    payload_json: Mapped[dict | list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    run: Mapped[EosRun] = relationship(back_populates="plan_instructions")


class EosMqttOutputEvent(Base):
    __tablename__ = "eos_mqtt_output_events"
    __table_args__ = (
        CheckConstraint(
            "output_kind IN ('plan','solution','command','preview','unknown')",
            name="ck_eos_mqtt_output_events_output_kind",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("eos_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[dict | list] = mapped_column(JSONB, nullable=False)
    qos: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default="1")
    retain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    output_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )
    resource_id: Mapped[str | None] = mapped_column(String(128))
    publish_status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    run: Mapped[EosRun] = relationship(back_populates="mqtt_output_events")


class ControlTarget(Base):
    __tablename__ = "control_targets"
    __table_args__ = (UniqueConstraint("resource_id", name="uq_control_targets_resource_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_topic: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    dry_run_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    qos: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default="1")
    retain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    payload_template_json: Mapped[dict | list | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OutputTarget(Base):
    __tablename__ = "output_targets"
    __table_args__ = (
        UniqueConstraint("resource_id", name="uq_output_targets_resource_id"),
        CheckConstraint(
            "method IN ('POST','PUT','PATCH')",
            name="ck_output_targets_method",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    webhook_url: Mapped[str] = mapped_column(String(512), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False, default="POST", server_default="POST")
    headers_json: Mapped[dict | list] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")
    retry_max: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    payload_template_json: Mapped[dict | list | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OutputDispatchEvent(Base):
    __tablename__ = "output_dispatch_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_output_dispatch_events_idempotency_key"),
        CheckConstraint(
            "dispatch_kind IN ('scheduled','heartbeat','force')",
            name="ck_output_dispatch_events_dispatch_kind",
        ),
        CheckConstraint(
            "status IN ('sent','blocked','failed','retrying','skipped_no_target')",
            name="ck_output_dispatch_events_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("eos_runs.id", ondelete="CASCADE"),
    )
    resource_id: Mapped[str | None] = mapped_column(String(128))
    execution_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_url: Mapped[str | None] = mapped_column(String(512))
    request_payload_json: Mapped[dict | list] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_text: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    run: Mapped[EosRun | None] = relationship(back_populates="output_dispatch_events")


class ParameterProfile(Base):
    __tablename__ = "parameter_profiles"
    __table_args__ = (UniqueConstraint("name", name="uq_parameter_profiles_name"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
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

    revisions: Mapped[list["ParameterProfileRevision"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
    )


class ParameterProfileRevision(Base):
    __tablename__ = "parameter_profile_revisions"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "revision_no",
            name="uq_parameter_profile_revisions_profile_revision",
        ),
        CheckConstraint(
            "source IN ('manual','import','bootstrap','eos_pull','dynamic_input')",
            name="ck_parameter_profile_revisions_source",
        ),
        CheckConstraint(
            "validation_status IN ('valid','invalid','unknown')",
            name="ck_parameter_profile_revisions_validation_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("parameter_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[dict | list] = mapped_column(JSONB, nullable=False)
    validation_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )
    validation_issues_json: Mapped[dict | list | None] = mapped_column(JSONB)
    is_current_draft: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    is_last_applied: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    profile: Mapped[ParameterProfile] = relationship(back_populates="revisions")
    parameter_input_events: Mapped[list["ParameterInputEvent"]] = relationship(
        back_populates="revision",
    )


class ParameterBinding(Base):
    __tablename__ = "parameter_bindings"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "input_key",
            name="uq_parameter_bindings_channel_input_key",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    parameter_key: Mapped[str] = mapped_column(String(160), nullable=False)
    selector_value: Mapped[str | None] = mapped_column(String(128))
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("input_channels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    input_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_path: Mapped[str | None] = mapped_column(String(255))
    timestamp_path: Mapped[str | None] = mapped_column(String(255))
    incoming_unit: Mapped[str | None] = mapped_column(String(32))
    value_multiplier: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default="1.0",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
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

    channel: Mapped[InputChannel] = relationship(back_populates="parameter_bindings")
    events: Mapped[list["ParameterInputEvent"]] = relationship(
        back_populates="binding",
    )


class ParameterInputEvent(Base):
    __tablename__ = "parameter_input_events"
    __table_args__ = (
        CheckConstraint(
            "apply_status IN ('accepted','rejected','applied','apply_failed','ignored_unbound','blocked_no_active_profile')",
            name="ck_parameter_input_events_apply_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    binding_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("parameter_bindings.id", ondelete="SET NULL"),
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("input_channels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    input_key: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_key: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_value_text: Mapped[str | None] = mapped_column(Text)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("parameter_profile_revisions.id", ondelete="SET NULL"),
    )
    apply_status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text)
    meta_json: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="'{}'",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    binding: Mapped[ParameterBinding | None] = relationship(back_populates="events")
    channel: Mapped[InputChannel] = relationship(back_populates="parameter_input_events")
    revision: Mapped[ParameterProfileRevision | None] = relationship(
        back_populates="parameter_input_events"
    )


class SetupFieldEvent(Base):
    __tablename__ = "setup_field_events"
    __table_args__ = (
        CheckConstraint(
            "source IN ('ui','http','import','system')",
            name="ck_setup_field_events_source",
        ),
        CheckConstraint(
            "apply_status IN ('accepted','applied','rejected','failed')",
            name="ck_setup_field_events_apply_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    field_id: Mapped[str] = mapped_column(String(191), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    raw_value_text: Mapped[str | None] = mapped_column(Text)
    normalized_value_json: Mapped[dict | list | None] = mapped_column(JSONB)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    apply_status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class SignalCatalog(Base):
    __tablename__ = "signal_catalog"
    __table_args__ = (
        UniqueConstraint("signal_key", name="uq_signal_catalog_signal_key"),
        CheckConstraint(
            "value_type IN ('number','string','bool','json')",
            name="ck_signal_catalog_value_type",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    signal_key: Mapped[str] = mapped_column(String(160), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_unit: Mapped[str | None] = mapped_column(String(32))
    tags_json: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="'{}'",
    )
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


class SignalMeasurementRaw(Base):
    __tablename__ = "signal_measurements_raw"
    __table_args__ = (
        CheckConstraint(
            "quality_status IN ('ok','derived','invalid','stale','estimated','missing')",
            name="ck_signal_measurements_raw_quality_status",
        ),
        CheckConstraint(
            "source_type IN "
            "('mqtt_input','http_input','param_input','fixed_input','eos_prediction','eos_plan','eos_solution','device_feedback','derived')",
            name="ck_signal_measurements_raw_source_type",
        ),
        {"postgresql_partition_by": "RANGE (ts)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    signal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("signal_catalog.id"),
        nullable=False,
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    value_num: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_bool: Mapped[bool | None] = mapped_column(Boolean)
    value_json: Mapped[dict | list | None] = mapped_column(JSONB)
    quality_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="ok",
        server_default="ok",
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("eos_runs.id", ondelete="SET NULL"),
    )
    mapping_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("input_mappings.id", ondelete="SET NULL"),
    )
    source_ref_id: Mapped[int | None] = mapped_column(BigInteger)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    ingest_lag_ms: Mapped[int | None] = mapped_column(Integer)


class SignalStateLatest(Base):
    __tablename__ = "signal_state_latest"
    __table_args__ = (
        CheckConstraint(
            "last_quality_status IS NULL OR last_quality_status IN ('ok','derived','invalid','stale','estimated','missing')",
            name="ck_signal_state_latest_quality",
        ),
    )

    signal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("signal_catalog.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_value_num: Mapped[float | None] = mapped_column(Float)
    last_value_text: Mapped[str | None] = mapped_column(Text)
    last_value_bool: Mapped[bool | None] = mapped_column(Boolean)
    last_value_json: Mapped[dict | list | None] = mapped_column(JSONB)
    last_quality_status: Mapped[str | None] = mapped_column(String(16))
    last_source_type: Mapped[str | None] = mapped_column(String(32))
    last_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("eos_runs.id", ondelete="SET NULL"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class EosRunInputSnapshot(Base):
    __tablename__ = "eos_run_input_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_eos_run_input_snapshots_run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("eos_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    parameter_profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("parameter_profiles.id", ondelete="SET NULL"),
    )
    parameter_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("parameter_profile_revisions.id", ondelete="SET NULL"),
    )
    parameter_payload_json: Mapped[dict | list] = mapped_column(JSONB, nullable=False)
    mappings_snapshot_json: Mapped[dict | list] = mapped_column(JSONB, nullable=False)
    live_state_snapshot_json: Mapped[dict | list] = mapped_column(JSONB, nullable=False)
    runtime_config_snapshot_json: Mapped[dict | list] = mapped_column(JSONB, nullable=False)
    assembled_eos_input_json: Mapped[dict | list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class SignalRollup5m(Base):
    __tablename__ = "signal_rollup_5m"

    signal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("signal_catalog.id", ondelete="CASCADE"),
        primary_key=True,
    )
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    min_num: Mapped[float | None] = mapped_column(Float)
    max_num: Mapped[float | None] = mapped_column(Float)
    avg_num: Mapped[float | None] = mapped_column(Float)
    sum_num: Mapped[float | None] = mapped_column(Float)
    count_num: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_num: Mapped[float | None] = mapped_column(Float)


class SignalRollup1h(Base):
    __tablename__ = "signal_rollup_1h"

    signal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("signal_catalog.id", ondelete="CASCADE"),
        primary_key=True,
    )
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    min_num: Mapped[float | None] = mapped_column(Float)
    max_num: Mapped[float | None] = mapped_column(Float)
    avg_num: Mapped[float | None] = mapped_column(Float)
    sum_num: Mapped[float | None] = mapped_column(Float)
    count_num: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_num: Mapped[float | None] = mapped_column(Float)


class SignalRollup1d(Base):
    __tablename__ = "signal_rollup_1d"

    signal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("signal_catalog.id", ondelete="CASCADE"),
        primary_key=True,
    )
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    min_num: Mapped[float | None] = mapped_column(Float)
    max_num: Mapped[float | None] = mapped_column(Float)
    avg_num: Mapped[float | None] = mapped_column(Float)
    sum_num: Mapped[float | None] = mapped_column(Float)
    count_num: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_num: Mapped[float | None] = mapped_column(Float)


class RetentionJobRun(Base):
    __tablename__ = "retention_job_runs"
    __table_args__ = (
        CheckConstraint("job_name IN ('rollup','retention')", name="ck_retention_job_runs_name"),
        CheckConstraint("status IN ('ok','error')", name="ck_retention_job_runs_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    affected_rows: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    details_json: Mapped[dict | list | None] = mapped_column(JSONB)
    error_text: Mapped[str | None] = mapped_column(Text)


class PowerSample(Base):
    __tablename__ = "power_samples"
    __table_args__ = (
        CheckConstraint(
            "quality IN ('ok','gap','interpolated')",
            name="ck_power_samples_quality",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value_w: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    quality: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="ok",
        server_default="ok",
    )
    mapping_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("input_mappings.id", ondelete="SET NULL"),
    )
    raw_payload: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class EnergyEmr(Base):
    __tablename__ = "energy_emr"
    __table_args__ = (
        UniqueConstraint("emr_key", "ts", name="uq_energy_emr_key_ts"),
        CheckConstraint(
            "method IN ('integrate','hold','interpolate')",
            name="ck_energy_emr_method",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    emr_key: Mapped[str] = mapped_column(String(64), nullable=False)
    emr_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    last_power_w: Mapped[float | None] = mapped_column(Float)
    last_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class EosMeasurementSyncRun(Base):
    __tablename__ = "eos_measurement_sync_runs"
    __table_args__ = (
        CheckConstraint(
            "trigger_source IN ('periodic','force')",
            name="ck_eos_measurement_sync_runs_trigger_source",
        ),
        CheckConstraint(
            "status IN ('running','ok','partial','error','blocked')",
            name="ck_eos_measurement_sync_runs_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    trigger_source: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    pushed_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    details_json: Mapped[dict | list | None] = mapped_column(JSONB)
    error_text: Mapped[str | None] = mapped_column(Text)
