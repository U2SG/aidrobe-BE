"""Scope preferences and fence learning after user corrections."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0055_scoped_preference_memory"
down_revision = "0054_designer_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_preference_memory", sa.Column(
        "scope", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
    ))
    op.add_column("user_preference_memory", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_preference_memory", sa.Column("evidence_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("memory_cleared_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("memory_context_reset_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE user_preference_memory SET evidence_at = updated_at")
    op.execute("""
        UPDATE user_preference_memory SET expires_at = updated_at + INTERVAL '14 days'
        WHERE source <> 'explicit_user_statement'
    """)
    op.drop_index("uq_user_preference_memory_live", table_name="user_preference_memory")
    op.create_index(
        "uq_user_preference_memory_live", "user_preference_memory",
        ["user_id", "preference_type", "preference_value", "scope"],
        unique=True, postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    # Older code has one live row per value. Preserve the most recent explicit
    # statement, keeping other scoped rows as history instead of deleting data.
    op.execute("""
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY user_id, preference_type, preference_value
                ORDER BY (source = 'explicit_user_statement') DESC, updated_at DESC, id DESC
            ) AS n FROM user_preference_memory WHERE deleted_at IS NULL
        ) UPDATE user_preference_memory SET deleted_at = now()
          WHERE id IN (SELECT id FROM ranked WHERE n > 1)
    """)
    op.drop_index("uq_user_preference_memory_live", table_name="user_preference_memory")
    op.create_index(
        "uq_user_preference_memory_live", "user_preference_memory",
        ["user_id", "preference_type", "preference_value"],
        unique=True, postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_column("user_preference_memory", "evidence_at")
    op.drop_column("user_preference_memory", "expires_at")
    op.drop_column("user_preference_memory", "scope")
    op.drop_column("users", "memory_context_reset_at")
    op.drop_column("users", "memory_cleared_at")
