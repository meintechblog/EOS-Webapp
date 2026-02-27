"""prediction storage tuning and legacy prediction-series cleanup

Revision ID: 20260221_0013
Revises: 20260221_0012
Create Date: 2026-02-21 13:45:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260221_0013"
down_revision: Union[str, Sequence[str], None] = "20260221_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PREDICTION_SIGNAL_ALLOWLIST = (
    "prediction.elecprice_marketprice_wh",
    "prediction.elecprice_marketprice_kwh",
    "prediction.pvforecast_ac_power",
    "prediction.pvforecastakkudoktor_ac_power_any",
    "prediction.loadforecast_power_w",
    "prediction.load_mean_adjusted",
    "prediction.load_mean",
    "prediction.loadakkudoktor_mean_power_w",
)


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS eos_prediction_points CASCADE")

    allowlist_sql = ", ".join(f"'{value}'" for value in _PREDICTION_SIGNAL_ALLOWLIST)
    op.execute(
        f"""
        WITH doomed AS (
            SELECT id
            FROM signal_catalog
            WHERE signal_key LIKE 'prediction.%'
              AND signal_key NOT IN ({allowlist_sql})
        )
        DELETE FROM signal_measurements_raw
        WHERE signal_id IN (SELECT id FROM doomed)
        """
    )
    op.execute(
        f"""
        WITH doomed AS (
            SELECT id
            FROM signal_catalog
            WHERE signal_key LIKE 'prediction.%'
              AND signal_key NOT IN ({allowlist_sql})
        )
        DELETE FROM signal_state_latest
        WHERE signal_id IN (SELECT id FROM doomed)
        """
    )
    op.execute(
        f"""
        WITH doomed AS (
            SELECT id
            FROM signal_catalog
            WHERE signal_key LIKE 'prediction.%'
              AND signal_key NOT IN ({allowlist_sql})
        )
        DELETE FROM signal_rollup_5m
        WHERE signal_id IN (SELECT id FROM doomed)
        """
    )
    op.execute(
        f"""
        WITH doomed AS (
            SELECT id
            FROM signal_catalog
            WHERE signal_key LIKE 'prediction.%'
              AND signal_key NOT IN ({allowlist_sql})
        )
        DELETE FROM signal_rollup_1h
        WHERE signal_id IN (SELECT id FROM doomed)
        """
    )
    op.execute(
        f"""
        WITH doomed AS (
            SELECT id
            FROM signal_catalog
            WHERE signal_key LIKE 'prediction.%'
              AND signal_key NOT IN ({allowlist_sql})
        )
        DELETE FROM signal_rollup_1d
        WHERE signal_id IN (SELECT id FROM doomed)
        """
    )
    op.execute(
        f"""
        DELETE FROM signal_catalog
        WHERE signal_key LIKE 'prediction.%'
          AND signal_key NOT IN ({allowlist_sql})
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Irreversible migration: prediction storage tuning cleanup")
