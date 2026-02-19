"""initial mqtt db api slice

Revision ID: 20260219_0001
Revises:
Create Date: 2026-02-19 15:03:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260219_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "input_mappings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("eos_field", sa.String(length=128), nullable=False),
        sa.Column("mqtt_topic", sa.String(length=255), nullable=False),
        sa.Column("payload_path", sa.String(length=255), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("eos_field", name="uq_input_mappings_eos_field"),
        sa.UniqueConstraint("mqtt_topic", name="uq_input_mappings_mqtt_topic"),
    )

    op.create_table(
        "telemetry_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("mapping_id", sa.BigInteger(), nullable=False),
        sa.Column("eos_field", sa.String(length=128), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("parsed_value", sa.Text(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["mapping_id"], ["input_mappings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.execute(
        "CREATE INDEX ix_telemetry_events_mapping_id_ts_desc ON telemetry_events (mapping_id, ts DESC)"
    )
    op.execute(
        "CREATE INDEX ix_telemetry_events_eos_field_ts_desc ON telemetry_events (eos_field, ts DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_telemetry_events_eos_field_ts_desc")
    op.execute("DROP INDEX IF EXISTS ix_telemetry_events_mapping_id_ts_desc")
    op.drop_table("telemetry_events")
    op.drop_table("input_mappings")

