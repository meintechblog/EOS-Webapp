"""fixed value mappings for static eos fields

Revision ID: 20260219_0003
Revises: 20260219_0002
Create Date: 2026-02-19 16:40:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260219_0003"
down_revision: Union[str, Sequence[str], None] = "20260219_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("input_mappings", sa.Column("fixed_value", sa.Text(), nullable=True))
    op.alter_column(
        "input_mappings",
        "mqtt_topic",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_input_mappings_value_source",
        "input_mappings",
        "(mqtt_topic IS NOT NULL) <> (fixed_value IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_input_mappings_value_source", "input_mappings", type_="check")
    op.execute(
        "UPDATE input_mappings SET mqtt_topic='__fixed__/' || eos_field WHERE mqtt_topic IS NULL"
    )
    op.alter_column(
        "input_mappings",
        "mqtt_topic",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_column("input_mappings", "fixed_value")
