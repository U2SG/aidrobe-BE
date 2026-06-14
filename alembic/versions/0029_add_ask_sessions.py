"""ask_sessions table — M15 Ask Aidrobe

Revision ID: 0029_add_ask_sessions
Revises: 0026_add_object_key_columns
Create Date: 2026-06-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0029_add_ask_sessions"
down_revision: str | None = "0026_add_object_key_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """One Q&A turn per row. FK ON DELETE CASCADE so the PIPL purge worker
    wipes Ask history with the user (the literal plan DDL omits cascade)."""
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "ask_sessions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("item_refs", JSONB(), nullable=True),
        sa.Column("context", JSONB(), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # History reads are per-user, most-recent-first.
    op.create_index(
        "ix_ask_sessions_user_created",
        "ask_sessions",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("ix_ask_sessions_user_created", table_name="ask_sessions")
    op.drop_table("ask_sessions")
