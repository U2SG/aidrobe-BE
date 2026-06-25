"""user_preference_memory table — BE#46 persistent preference memory

A durable, user-correctable layer of long-term preferences (disliked fits,
preferred colors, avoided styles, ...). FK ON DELETE CASCADE so the PIPL purge
worker wipes preference memory with the user. The partial unique index
(deleted_at IS NULL) backs the controlled write path's merge/upsert semantics —
one live row per (user_id, preference_type, preference_value).

Revision ID: 0036_user_preference_memory
Revises: 0035_outfit_reasoning
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0036_user_preference_memory"
down_revision: str | None = "0035_outfit_reasoning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "user_preference_memory",
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
        sa.Column("preference_type", sa.String(length=40), nullable=False),
        sa.Column("preference_value", sa.Text(), nullable=False),
        sa.Column("polarity", sa.String(length=8), nullable=False, server_default="neutral"),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False, server_default="0.5"),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_message_id", UUID(as_uuid=True), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_user_preference_memory_confidence_range",
        ),
        sa.CheckConstraint(
            "polarity IN ('positive', 'negative', 'neutral')",
            name="ck_user_preference_memory_polarity_enum",
        ),
        sa.CheckConstraint(
            "char_length(preference_value) <= 120",
            name="ck_user_preference_memory_value_length",
        ),
    )
    op.create_index(
        "ix_user_preference_memory_user_id",
        "user_preference_memory",
        ["user_id"],
    )
    # Partial unique index = the merge key for the controlled write path.
    op.create_index(
        "uq_user_preference_memory_live",
        "user_preference_memory",
        ["user_id", "preference_type", "preference_value"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("uq_user_preference_memory_live", table_name="user_preference_memory")
    op.drop_index("ix_user_preference_memory_user_id", table_name="user_preference_memory")
    op.drop_table("user_preference_memory")
