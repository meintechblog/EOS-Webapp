"""eos orchestration persistence and control tables

Revision ID: 20260219_0004
Revises: 20260219_0003
Create Date: 2026-02-19 18:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260219_0004"
down_revision: Union[str, Sequence[str], None] = "20260219_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "eos_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("trigger_source", sa.String(length=32), nullable=False),
        sa.Column("run_mode", sa.String(length=32), nullable=False),
        sa.Column("eos_last_run_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("CREATE INDEX ix_eos_runs_created_at_desc ON eos_runs (created_at DESC)")

    op.create_table(
        "eos_artifacts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("artifact_key", sa.String(length=128), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["eos_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("CREATE INDEX ix_eos_artifacts_run_id ON eos_artifacts (run_id)")
    op.execute("CREATE INDEX ix_eos_artifacts_type_key ON eos_artifacts (artifact_type, artifact_key)")

    op.create_table(
        "eos_prediction_points",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("prediction_key", sa.String(length=128), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["eos_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "CREATE INDEX ix_eos_prediction_points_run_key_ts ON eos_prediction_points (run_id, prediction_key, ts)"
    )

    op.create_table(
        "eos_plan_instructions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("instruction_index", sa.BigInteger(), nullable=False),
        sa.Column("instruction_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("actuator_id", sa.String(length=128), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operation_mode_id", sa.String(length=64), nullable=True),
        sa.Column("operation_mode_factor", sa.Float(), nullable=True),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["eos_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("CREATE INDEX ix_eos_plan_instructions_run_id ON eos_plan_instructions (run_id)")
    op.execute(
        "CREATE INDEX ix_eos_plan_instructions_resource_id ON eos_plan_instructions (resource_id)"
    )

    op.create_table(
        "eos_mqtt_output_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("qos", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("retain", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("publish_status", sa.String(length=32), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["eos_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "CREATE INDEX ix_eos_mqtt_output_events_run_published ON eos_mqtt_output_events (run_id, published_at DESC)"
    )

    op.create_table(
        "control_targets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("command_topic", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("dry_run_only", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("qos", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("retain", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("payload_template_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resource_id", name="uq_control_targets_resource_id"),
    )


def downgrade() -> None:
    op.drop_table("control_targets")

    op.execute("DROP INDEX IF EXISTS ix_eos_mqtt_output_events_run_published")
    op.drop_table("eos_mqtt_output_events")

    op.execute("DROP INDEX IF EXISTS ix_eos_plan_instructions_resource_id")
    op.execute("DROP INDEX IF EXISTS ix_eos_plan_instructions_run_id")
    op.drop_table("eos_plan_instructions")

    op.execute("DROP INDEX IF EXISTS ix_eos_prediction_points_run_key_ts")
    op.drop_table("eos_prediction_points")

    op.execute("DROP INDEX IF EXISTS ix_eos_artifacts_type_key")
    op.execute("DROP INDEX IF EXISTS ix_eos_artifacts_run_id")
    op.drop_table("eos_artifacts")

    op.execute("DROP INDEX IF EXISTS ix_eos_runs_created_at_desc")
    op.drop_table("eos_runs")
