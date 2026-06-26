"""add outfit_favorites table

Revision ID: 0040_add_outfit_favorites
Revises: 0039_shopping_sense_check_object_key
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0040_add_outfit_favorites"
down_revision: str | None = "0039_shopping_sense_check_object_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "outfit_favorites",
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("outfit_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_outfit_favorites_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["outfit_id"],
            ["outfits.id"],
            name="fk_outfit_favorites_outfit_id_outfits",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "outfit_id", name="pk_outfit_favorites"),
        sa.UniqueConstraint(
            "user_id",
            "outfit_id",
            name="uq_outfit_favorites_user_outfit",
        ),
    )
    op.create_index("ix_outfit_favorites_user_id", "outfit_favorites", ["user_id"])
    op.create_index("ix_outfit_favorites_outfit_id", "outfit_favorites", ["outfit_id"])


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("ix_outfit_favorites_outfit_id", table_name="outfit_favorites")
    op.drop_index("ix_outfit_favorites_user_id", table_name="outfit_favorites")
    op.drop_table("outfit_favorites")

