"""parameter profiles and revisions

Revision ID: 20260219_0005
Revises: 20260219_0004
Create Date: 2026-02-19 19:40:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260219_0005"
down_revision: Union[str, Sequence[str], None] = "20260219_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parameter_profiles",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_parameter_profiles_name"),
    )

    op.create_table(
        "parameter_profile_revisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "validation_status",
            sa.String(length=16),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column("validation_issues_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_current_draft", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_last_applied", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["profile_id"], ["parameter_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "revision_no",
            name="uq_parameter_profile_revisions_profile_revision",
        ),
        sa.CheckConstraint(
            "source IN ('manual','import','bootstrap','eos_pull')",
            name="ck_parameter_profile_revisions_source",
        ),
        sa.CheckConstraint(
            "validation_status IN ('valid','invalid','unknown')",
            name="ck_parameter_profile_revisions_validation_status",
        ),
    )

    op.execute(
        "CREATE INDEX ix_parameter_profile_revisions_profile_created_desc "
        "ON parameter_profile_revisions (profile_id, created_at DESC)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_parameter_profile_revisions_current_draft "
        "ON parameter_profile_revisions (profile_id) WHERE is_current_draft"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_parameter_profile_revisions_last_applied "
        "ON parameter_profile_revisions (profile_id) WHERE is_last_applied"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_parameter_profile_revisions_last_applied")
    op.execute("DROP INDEX IF EXISTS uq_parameter_profile_revisions_current_draft")
    op.execute("DROP INDEX IF EXISTS ix_parameter_profile_revisions_profile_created_desc")
    op.drop_table("parameter_profile_revisions")
    op.drop_table("parameter_profiles")
