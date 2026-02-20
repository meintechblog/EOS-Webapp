"""http-only unified setup + legacy input reset

Revision ID: 20260221_0010
Revises: 20260220_0009
Create Date: 2026-02-21 00:10:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260221_0010"
down_revision: Union[str, Sequence[str], None] = "20260220_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "setup_field_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("field_id", sa.String(length=191), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("raw_value_text", sa.Text(), nullable=True),
        sa.Column("normalized_value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("apply_status", sa.String(length=16), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "source IN ('ui','http','import','system')",
            name="ck_setup_field_events_source",
        ),
        sa.CheckConstraint(
            "apply_status IN ('accepted','applied','rejected','failed')",
            name="ck_setup_field_events_apply_status",
        ),
    )
    op.execute("CREATE INDEX ix_setup_field_events_field_created_desc ON setup_field_events (field_id, created_at DESC)")
    op.execute("CREATE INDEX ix_setup_field_events_status_created_desc ON setup_field_events (apply_status, created_at DESC)")

    # Hard reset legacy input configuration/state tables.
    op.execute(
        """
        TRUNCATE TABLE
            mqtt_topic_observations,
            input_observations,
            parameter_input_events,
            parameter_bindings,
            telemetry_events,
            input_mappings,
            input_channels
        RESTART IDENTITY CASCADE
        """
    )

    # Normalize parameter profile state to a single active profile + one revision.
    bind = op.get_bind()
    keep_profile_row = bind.execute(
        sa.text(
            """
            SELECT id
            FROM parameter_profiles
            ORDER BY is_active DESC, id ASC
            LIMIT 1
            """
        )
    ).first()
    if keep_profile_row is None:
        return

    keep_profile_id = int(keep_profile_row[0])
    bind.execute(
        sa.text(
            """
            DELETE FROM parameter_profiles
            WHERE id <> :keep_profile_id
            """
        ),
        {"keep_profile_id": keep_profile_id},
    )
    bind.execute(
        sa.text(
            """
            UPDATE parameter_profiles
            SET
                name = 'Current',
                description = 'Single internal setup state',
                is_active = true,
                updated_at = now()
            WHERE id = :keep_profile_id
            """
        ),
        {"keep_profile_id": keep_profile_id},
    )

    keep_revision_row = bind.execute(
        sa.text(
            """
            SELECT id
            FROM parameter_profile_revisions
            WHERE profile_id = :keep_profile_id
            ORDER BY revision_no DESC, id DESC
            LIMIT 1
            """
        ),
        {"keep_profile_id": keep_profile_id},
    ).first()
    if keep_revision_row is None:
        return

    keep_revision_id = int(keep_revision_row[0])
    bind.execute(
        sa.text(
            """
            DELETE FROM parameter_profile_revisions
            WHERE profile_id = :keep_profile_id
              AND id <> :keep_revision_id
            """
        ),
        {
            "keep_profile_id": keep_profile_id,
            "keep_revision_id": keep_revision_id,
        },
    )
    bind.execute(
        sa.text(
            """
            UPDATE parameter_profile_revisions
            SET
                source = 'manual',
                validation_status = COALESCE(validation_status, 'unknown'),
                is_current_draft = true,
                is_last_applied = true,
                applied_at = COALESCE(applied_at, now())
            WHERE id = :keep_revision_id
            """
        ),
        {"keep_revision_id": keep_revision_id},
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_setup_field_events_status_created_desc")
    op.execute("DROP INDEX IF EXISTS ix_setup_field_events_field_created_desc")
    op.drop_table("setup_field_events")

