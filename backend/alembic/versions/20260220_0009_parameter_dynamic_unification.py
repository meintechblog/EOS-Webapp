"""parameter dynamic bindings + setup unification

Revision ID: 20260220_0009
Revises: 20260220_0008
Create Date: 2026-02-20 12:15:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260220_0009"
down_revision: Union[str, Sequence[str], None] = "20260220_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE parameter_profile_revisions
        DROP CONSTRAINT IF EXISTS ck_parameter_profile_revisions_source
        """
    )
    op.execute(
        """
        ALTER TABLE parameter_profile_revisions
        DROP CONSTRAINT IF EXISTS parameter_profile_revisions_source_check
        """
    )
    op.create_check_constraint(
        "ck_parameter_profile_revisions_source",
        "parameter_profile_revisions",
        "source IN ('manual','import','bootstrap','eos_pull','dynamic_input')",
    )

    op.create_table(
        "parameter_bindings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("parameter_key", sa.String(length=160), nullable=False),
        sa.Column("selector_value", sa.String(length=128), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("input_key", sa.String(length=255), nullable=False),
        sa.Column("payload_path", sa.String(length=255), nullable=True),
        sa.Column("timestamp_path", sa.String(length=255), nullable=True),
        sa.Column("incoming_unit", sa.String(length=32), nullable=True),
        sa.Column("value_multiplier", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["channel_id"], ["input_channels.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", "input_key", name="uq_parameter_bindings_channel_input_key"),
    )

    op.create_table(
        "parameter_input_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("binding_id", sa.BigInteger(), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("input_key", sa.String(length=255), nullable=False),
        sa.Column("normalized_key", sa.String(length=255), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("parsed_value_text", sa.Text(), nullable=True),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_id", sa.BigInteger(), nullable=True),
        sa.Column("apply_status", sa.String(length=24), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column(
            "meta_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["binding_id"], ["parameter_bindings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["channel_id"], ["input_channels.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["revision_id"], ["parameter_profile_revisions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "apply_status IN ('accepted','rejected','applied','apply_failed','ignored_unbound','blocked_no_active_profile')",
            name="ck_parameter_input_events_apply_status",
        ),
    )
    op.execute("CREATE INDEX ix_parameter_input_events_created_desc ON parameter_input_events (created_at DESC)")
    op.execute(
        "CREATE INDEX ix_parameter_input_events_status_created_desc "
        "ON parameter_input_events (apply_status, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_parameter_input_events_channel_created_desc "
        "ON parameter_input_events (channel_id, created_at DESC)"
    )

    op.execute(
        """
        ALTER TABLE signal_measurements_raw
        DROP CONSTRAINT IF EXISTS ck_signal_measurements_raw_source_type
        """
    )
    op.execute(
        """
        ALTER TABLE signal_measurements_raw
        DROP CONSTRAINT IF EXISTS signal_measurements_raw_source_type_check
        """
    )
    op.create_check_constraint(
        "ck_signal_measurements_raw_source_type",
        "signal_measurements_raw",
        "source_type IN ('mqtt_input','http_input','param_input','fixed_input','eos_prediction','eos_plan','eos_solution','device_feedback','derived')",
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE signal_measurements_raw
        DROP CONSTRAINT IF EXISTS ck_signal_measurements_raw_source_type
        """
    )
    op.execute(
        """
        ALTER TABLE signal_measurements_raw
        DROP CONSTRAINT IF EXISTS signal_measurements_raw_source_type_check
        """
    )
    op.create_check_constraint(
        "ck_signal_measurements_raw_source_type",
        "signal_measurements_raw",
        "source_type IN ('mqtt_input','http_input','fixed_input','eos_prediction','eos_plan','eos_solution','device_feedback','derived')",
    )

    op.execute("DROP INDEX IF EXISTS ix_parameter_input_events_channel_created_desc")
    op.execute("DROP INDEX IF EXISTS ix_parameter_input_events_status_created_desc")
    op.execute("DROP INDEX IF EXISTS ix_parameter_input_events_created_desc")
    op.drop_table("parameter_input_events")
    op.drop_table("parameter_bindings")

    op.execute(
        """
        ALTER TABLE parameter_profile_revisions
        DROP CONSTRAINT IF EXISTS ck_parameter_profile_revisions_source
        """
    )
    op.execute(
        """
        ALTER TABLE parameter_profile_revisions
        DROP CONSTRAINT IF EXISTS parameter_profile_revisions_source_check
        """
    )
    op.create_check_constraint(
        "ck_parameter_profile_revisions_source",
        "parameter_profile_revisions",
        "source IN ('manual','import','bootstrap','eos_pull')",
    )
