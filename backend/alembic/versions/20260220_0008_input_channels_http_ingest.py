"""input channels + http ingest backbone

Revision ID: 20260220_0008
Revises: 20260220_0007
Create Date: 2026-02-20 06:10:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260220_0008"
down_revision: Union[str, Sequence[str], None] = "20260220_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "input_channels",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("channel_type", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("config_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_input_channels_code"),
        sa.CheckConstraint(
            "channel_type IN ('mqtt','http')",
            name="ck_input_channels_type",
        ),
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_input_channels_default_mqtt
        ON input_channels (is_default)
        WHERE channel_type='mqtt' AND is_default=true
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_input_channels_default_http
        ON input_channels (is_default)
        WHERE channel_type='http' AND is_default=true
        """
    )

    op.add_column("input_mappings", sa.Column("channel_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_input_mappings_channel_id",
        "input_mappings",
        "input_channels",
        ["channel_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint("uq_input_mappings_mqtt_topic", "input_mappings", type_="unique")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_input_mappings_channel_topic
        ON input_mappings (channel_id, mqtt_topic)
        WHERE mqtt_topic IS NOT NULL
        """
    )

    op.drop_constraint("ck_input_mappings_value_source", "input_mappings", type_="check")

    op.create_table(
        "input_observations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("input_key", sa.String(length=255), nullable=False),
        sa.Column("normalized_key", sa.String(length=255), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_payload", sa.Text(), nullable=True),
        sa.Column("message_count", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("last_meta_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(["channel_id"], ["input_channels.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", "input_key", name="uq_input_observations_channel_key"),
    )
    op.execute("CREATE INDEX ix_input_observations_last_seen_desc ON input_observations (last_seen DESC)")

    op.execute(
        """
        INSERT INTO input_channels (code, name, channel_type, enabled, is_default, config_json)
        VALUES ('mqtt-default', 'MQTT Default', 'mqtt', true, true, '{}'::jsonb)
        ON CONFLICT (code) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO input_channels (code, name, channel_type, enabled, is_default, config_json)
        VALUES ('http-default', 'HTTP Default', 'http', true, true, '{}'::jsonb)
        ON CONFLICT (code) DO NOTHING
        """
    )

    op.execute(
        """
        UPDATE input_mappings
        SET channel_id = ic.id
        FROM input_channels ic
        WHERE ic.code = 'mqtt-default'
          AND input_mappings.fixed_value IS NULL
          AND input_mappings.mqtt_topic IS NOT NULL
          AND input_mappings.channel_id IS NULL
        """
    )

    op.execute(
        """
        UPDATE input_mappings
        SET mqtt_topic = NULL
        WHERE fixed_value IS NOT NULL
        """
    )

    op.create_check_constraint(
        "ck_input_mappings_value_source",
        "input_mappings",
        "((fixed_value IS NULL AND mqtt_topic IS NOT NULL AND channel_id IS NOT NULL) "
        "OR (fixed_value IS NOT NULL AND mqtt_topic IS NULL AND channel_id IS NULL))",
    )

    op.execute(
        """
        INSERT INTO input_observations
            (channel_id, input_key, normalized_key, first_seen, last_seen, last_payload, message_count, last_meta_json)
        SELECT
            ic.id,
            o.mqtt_topic,
            CASE
                WHEN o.mqtt_topic LIKE 'eos/input/%' THEN o.mqtt_topic
                WHEN o.mqtt_topic LIKE 'eos/%' THEN 'eos/input/' || SUBSTRING(o.mqtt_topic FROM 5)
                ELSE 'eos/input/' || TRIM(LEADING '/' FROM o.mqtt_topic)
            END AS normalized_key,
            o.first_seen,
            o.last_seen,
            o.last_payload,
            o.message_count,
            jsonb_build_object('retain', o.last_retain, 'qos', o.last_qos, 'source', 'mqtt_backfill')
        FROM mqtt_topic_observations o
        CROSS JOIN input_channels ic
        WHERE ic.code = 'mqtt-default'
        ON CONFLICT (channel_id, input_key)
        DO UPDATE SET
            last_seen = EXCLUDED.last_seen,
            last_payload = EXCLUDED.last_payload,
            message_count = EXCLUDED.message_count,
            last_meta_json = EXCLUDED.last_meta_json
        """
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
        "source_type IN ('mqtt_input','http_input','fixed_input','eos_prediction','eos_plan','eos_solution','device_feedback','derived')",
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
        "source_type IN ('mqtt_input','fixed_input','eos_prediction','eos_plan','eos_solution','device_feedback','derived')",
    )

    op.execute("DROP INDEX IF EXISTS ix_input_observations_last_seen_desc")
    op.drop_table("input_observations")

    op.drop_constraint("ck_input_mappings_value_source", "input_mappings", type_="check")
    op.create_check_constraint(
        "ck_input_mappings_value_source",
        "input_mappings",
        "(mqtt_topic IS NOT NULL) <> (fixed_value IS NOT NULL)",
    )

    op.execute("DROP INDEX IF EXISTS uq_input_mappings_channel_topic")
    op.create_unique_constraint("uq_input_mappings_mqtt_topic", "input_mappings", ["mqtt_topic"])

    op.drop_constraint("fk_input_mappings_channel_id", "input_mappings", type_="foreignkey")
    op.drop_column("input_mappings", "channel_id")

    op.execute("DROP INDEX IF EXISTS uq_input_channels_default_http")
    op.execute("DROP INDEX IF EXISTS uq_input_channels_default_mqtt")
    op.drop_table("input_channels")
