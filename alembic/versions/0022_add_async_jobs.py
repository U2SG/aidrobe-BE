"""add async_jobs table

Revision ID: 0022_add_async_jobs
Revises: 0021_add_outfit_explainability
Create Date: 2026-05-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0022_add_async_jobs"
down_revision: str | None = "0021_add_outfit_explainability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "async_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("target_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("cost_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Lease expiry for the running worker. NULL when not currently claimed.
        # Sweeper recovers rows where status='running' AND lease_expires_at < now().
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_async_jobs_idempotency_key"),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','dead')",
            name="ck_async_jobs_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_async_jobs_attempts_nonneg"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_async_jobs_max_attempts_min"),
        sa.CheckConstraint("cost_tokens >= 0", name="ck_async_jobs_cost_tokens_nonneg"),
        sa.CheckConstraint("cost_cents >= 0", name="ck_async_jobs_cost_cents_nonneg"),
    )
    # Worker claim hot path: WHERE status='queued' AND next_run_at <= now()
    # AND job_type = ANY(...). Partial index keeps it tiny once jobs settle.
    op.create_index(
        "ix_async_jobs_claim",
        "async_jobs",
        ["job_type", "next_run_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    # User-facing summaries / cost guard joins.
    op.create_index(
        "ix_async_jobs_user_type_created",
        "async_jobs",
        ["user_id", "job_type", "created_at"],
    )
    # Admin dead-letter view.
    op.create_index(
        "ix_async_jobs_status_updated",
        "async_jobs",
        ["status", "updated_at"],
    )
    # Lease-recovery sweep: WHERE status='running' AND lease_expires_at < now().
    op.create_index(
        "ix_async_jobs_lease_recovery",
        "async_jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("ix_async_jobs_lease_recovery", table_name="async_jobs")
    op.drop_index("ix_async_jobs_status_updated", table_name="async_jobs")
    op.drop_index("ix_async_jobs_user_type_created", table_name="async_jobs")
    op.drop_index("ix_async_jobs_claim", table_name="async_jobs")
    op.drop_table("async_jobs")
