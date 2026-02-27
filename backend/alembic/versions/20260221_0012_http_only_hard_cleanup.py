"""http-only hard cleanup for legacy mqtt/input-channel stack

Revision ID: 20260221_0012
Revises: 20260221_0011
Create Date: 2026-02-21 13:40:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260221_0012"
down_revision: Union[str, Sequence[str], None] = "20260221_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE signal_measurements_raw
        SET source_type = 'http_input'
        WHERE source_type = 'mqtt_input'
        """
    )
    op.execute("ALTER TABLE signal_measurements_raw DROP CONSTRAINT IF EXISTS ck_signal_measurements_raw_source_type")
    op.execute(
        """
        ALTER TABLE signal_measurements_raw
        ADD CONSTRAINT ck_signal_measurements_raw_source_type
        CHECK (
            source_type IN (
                'http_input',
                'param_input',
                'fixed_input',
                'eos_prediction',
                'eos_plan',
                'eos_solution',
                'device_feedback',
                'derived'
            )
        )
        """
    )

    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ts,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        signal_id,
                        ts,
                        source_type,
                        COALESCE(run_id, 0),
                        COALESCE(source_ref_id, 0)
                    ORDER BY id DESC
                ) AS rn
            FROM signal_measurements_raw
        )
        DELETE FROM signal_measurements_raw target
        USING ranked r
        WHERE target.id = r.id
          AND target.ts = r.ts
          AND r.rn > 1
        """
    )
    op.execute("DROP INDEX IF EXISTS uq_signal_measurements_raw_dedupe")
    op.execute("ALTER TABLE signal_measurements_raw DROP COLUMN IF EXISTS mapping_id")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_signal_measurements_raw_dedupe
        ON signal_measurements_raw (
            signal_id,
            ts,
            source_type,
            COALESCE(run_id, 0),
            COALESCE(source_ref_id, 0)
        )
        """
    )

    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY key, ts, source
                    ORDER BY id DESC
                ) AS rn
            FROM power_samples
        )
        DELETE FROM power_samples target
        USING ranked r
        WHERE target.id = r.id
          AND r.rn > 1
        """
    )
    op.execute("DROP INDEX IF EXISTS uq_power_samples_dedupe")
    op.execute("ALTER TABLE power_samples DROP COLUMN IF EXISTS mapping_id")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_power_samples_dedupe
        ON power_samples (key, ts, source)
        """
    )

    for table_name in (
        "mqtt_topic_observations",
        "eos_mqtt_output_events",
        "control_targets",
        "input_observations",
        "telemetry_events",
        "input_mappings",
        "input_channels",
        "parameter_bindings",
        "parameter_input_events",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")


def downgrade() -> None:
    raise NotImplementedError("Irreversible migration: http-only hard cleanup")
