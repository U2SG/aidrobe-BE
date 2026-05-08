"""user soft-delete columns + audit_log table

Revision ID: 0025_user_soft_delete_and_audit_log
Revises: 0024_add_recommendation_context
Create Date: 2026-05-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0025_user_soft_delete_and_audit_log"
down_revision: str | None = "0024_add_recommendation_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Phase E1+E2 — PIPL-aware soft delete + audit trail.

    Soft delete model:
    - `deactivated_at`: user requested deletion. Auth blocks login from here.
    - `purged_at`: hard-purge worker finished. Personal data is gone but the
      row stays so dangling references (audit_log.user_id) remain readable.
      Aggregate stats can also derive from this minimal stub if needed.
    Grace period between the two is configurable; default 30d.

    `audit_log` is intentionally FK-less to `users`. A purged user MUST
    leave behind an immutable record of *what was done with their data*,
    so PIPL DSAR responses can show the lifecycle even after the user
    row's PII has been wiped."""
    op.execute("SET lock_timeout = '3s'")

    op.add_column(
        "users",
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_users_deactivated_at",
        "users",
        ["deactivated_at"],
        postgresql_where=sa.text("deactivated_at IS NOT NULL"),
    )

    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Nullable so system / admin actions without a subject still record.
        # No FK on purpose: a purged user's lifecycle audit MUST survive even
        # when the users row is later wiped.
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", UUID(as_uuid=True), nullable=True),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "actor IN ('user','system','admin')", name="ck_audit_log_actor"
        ),
    )
    # Per-user lifecycle queries (DSAR responses), most-recent-first.
    op.create_index(
        "ix_audit_log_user_occurred",
        "audit_log",
        ["user_id", "occurred_at"],
        postgresql_using="btree",
    )
    # Action-driven sweeps (e.g. "all delete events in the last week").
    op.create_index("ix_audit_log_action_occurred", "audit_log", ["action", "occurred_at"])


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("ix_audit_log_action_occurred", table_name="audit_log")
    op.drop_index("ix_audit_log_user_occurred", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_users_deactivated_at", table_name="users")
    op.drop_column("users", "purged_at")
    op.drop_column("users", "deactivated_at")
