from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


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
    plan_instructions: Mapped[list["EosPlanInstruction"]] = relationship(
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
            "('http_input','param_input','fixed_input','eos_prediction','eos_plan','eos_solution','device_feedback','derived')",
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


class RuntimePreference(Base):
    __tablename__ = "runtime_preferences"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OutputSignalAccessState(Base):
    __tablename__ = "output_signal_access_state"

    signal_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    resource_id: Mapped[str | None] = mapped_column(String(128))
    last_fetch_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fetch_client: Mapped[str | None] = mapped_column(String(128))
    fetch_count: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
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
