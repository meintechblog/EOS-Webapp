"""automap topic discovery and mapping controls

Revision ID: 20260219_0002
Revises: 20260219_0001
Create Date: 2026-02-19 16:05:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260219_0002"
down_revision: Union[str, Sequence[str], None] = "20260219_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "input_mappings",
        sa.Column(
            "value_multiplier",
            sa.Float(),
            nullable=False,
            server_default=sa.text("1.0"),
        ),
    )
    op.add_column(
        "input_mappings",
        sa.Column(
            "sign_convention",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'canonical'"),
        ),
    )
    op.create_check_constraint(
        "ck_input_mappings_sign_convention",
        "input_mappings",
        "sign_convention IN ('canonical','unknown','positive_is_import','positive_is_export')",
    )
    op.execute("UPDATE input_mappings SET sign_convention='unknown' WHERE eos_field='grid_power_w'")

    op.create_table(
        "mqtt_topic_observations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("mqtt_topic", sa.String(length=255), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_payload", sa.Text(), nullable=True),
        sa.Column("message_count", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_retain", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_qos", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mqtt_topic", name="uq_mqtt_topic_observations_topic"),
    )
    op.execute(
        "CREATE INDEX ix_mqtt_topic_observations_last_seen_desc ON mqtt_topic_observations (last_seen DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_mqtt_topic_observations_last_seen_desc")
    op.drop_table("mqtt_topic_observations")

    op.drop_constraint("ck_input_mappings_sign_convention", "input_mappings", type_="check")
    op.drop_column("input_mappings", "sign_convention")
    op.drop_column("input_mappings", "value_multiplier")

