"""emr power samples + measurement sync

Revision ID: 20260220_0007
Revises: 20260220_0006
Create Date: 2026-02-20 03:10:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260220_0007"
down_revision: Union[str, Sequence[str], None] = "20260220_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "input_mappings",
        sa.Column("timestamp_path", sa.String(length=255), nullable=True),
    )

    op.create_table(
        "power_samples",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value_w", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "quality",
            sa.String(length=16),
            nullable=False,
            server_default="ok",
        ),
        sa.Column("mapping_id", sa.BigInteger(), nullable=True),
        sa.Column("raw_payload", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["mapping_id"], ["input_mappings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "quality IN ('ok','gap','interpolated')",
            name="ck_power_samples_quality",
        ),
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_power_samples_dedupe
        ON power_samples (key, ts, source, COALESCE(mapping_id, 0))
        """
    )
    op.execute("CREATE INDEX ix_power_samples_key_ts_desc ON power_samples (key, ts DESC)")

    op.create_table(
        "energy_emr",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("emr_key", sa.String(length=64), nullable=False),
        sa.Column("emr_kwh", sa.Float(), nullable=False),
        sa.Column("last_power_w", sa.Float(), nullable=True),
        sa.Column("last_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("emr_key", "ts", name="uq_energy_emr_key_ts"),
        sa.CheckConstraint(
            "method IN ('integrate','hold','interpolate')",
            name="ck_energy_emr_method",
        ),
    )
    op.execute("CREATE INDEX ix_energy_emr_key_ts_desc ON energy_emr (emr_key, ts DESC)")

    op.create_table(
        "eos_measurement_sync_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("trigger_source", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("pushed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("details_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "trigger_source IN ('periodic','force')",
            name="ck_eos_measurement_sync_runs_trigger_source",
        ),
        sa.CheckConstraint(
            "status IN ('running','ok','partial','error','blocked')",
            name="ck_eos_measurement_sync_runs_status",
        ),
    )
    op.execute(
        "CREATE INDEX ix_eos_measurement_sync_runs_started_desc ON eos_measurement_sync_runs (started_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_eos_measurement_sync_runs_started_desc")
    op.drop_table("eos_measurement_sync_runs")

    op.execute("DROP INDEX IF EXISTS ix_energy_emr_key_ts_desc")
    op.drop_table("energy_emr")

    op.execute("DROP INDEX IF EXISTS ix_power_samples_key_ts_desc")
    op.execute("DROP INDEX IF EXISTS uq_power_samples_dedupe")
    op.drop_table("power_samples")

    op.drop_column("input_mappings", "timestamp_path")
