"""replace push dispatch tables with output pull access state

Revision ID: 20260222_0015
Revises: 20260222_0014
Create Date: 2026-02-22 18:20:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260222_0015"
down_revision: Union[str, Sequence[str], None] = "20260222_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "output_signal_access_state",
        sa.Column("signal_key", sa.String(length=160), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("last_fetch_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fetch_client", sa.String(length=128), nullable=True),
        sa.Column("fetch_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("signal_key"),
    )

    op.execute("DROP INDEX IF EXISTS ix_output_dispatch_events_resource_execution_desc")
    op.execute("DROP INDEX IF EXISTS ix_output_dispatch_events_run_created_desc")
    op.execute("DROP TABLE IF EXISTS output_dispatch_events")
    op.execute("DROP TABLE IF EXISTS output_targets")


def downgrade() -> None:
    op.create_table(
        "output_targets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("webhook_url", sa.String(length=512), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False, server_default="POST"),
        sa.Column(
            "headers_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("retry_max", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("payload_template_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resource_id", name="uq_output_targets_resource_id"),
        sa.CheckConstraint(
            "method IN ('POST','PUT','PATCH')",
            name="ck_output_targets_method",
        ),
    )

    op.create_table(
        "output_dispatch_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("execution_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_kind", sa.String(length=16), nullable=False),
        sa.Column("target_url", sa.String(length=512), nullable=True),
        sa.Column("request_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["run_id"], ["eos_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_output_dispatch_events_idempotency_key"),
        sa.CheckConstraint(
            "dispatch_kind IN ('scheduled','heartbeat','force')",
            name="ck_output_dispatch_events_dispatch_kind",
        ),
        sa.CheckConstraint(
            "status IN ('sent','blocked','failed','retrying','skipped_no_target')",
            name="ck_output_dispatch_events_status",
        ),
    )
    op.execute(
        "CREATE INDEX ix_output_dispatch_events_run_created_desc ON output_dispatch_events (run_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_output_dispatch_events_resource_execution_desc ON output_dispatch_events (resource_id, execution_time DESC)"
    )

    op.execute("DROP TABLE IF EXISTS output_signal_access_state")
