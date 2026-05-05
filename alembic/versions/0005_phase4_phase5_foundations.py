"""add intelligent recommendation foundations

Revision ID: 0005_phase4_phase5
Revises: 0004_phase3_v11
Create Date: 2026-05-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_phase4_phase5"
down_revision: str | None = "0004_phase3_v11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("wardrobe_items", sa.Column("location", sa.String(length=120), nullable=True))
    op.create_table(
        "item_embeddings",
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("vector", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["wardrobe_items.id"],
            name=op.f("fk_item_embeddings_item_id_wardrobe_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_embeddings")),
    )
    op.create_index(op.f("ix_item_embeddings_item_id"), "item_embeddings", ["item_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_item_embeddings_item_id"), table_name="item_embeddings")
    op.drop_table("item_embeddings")
    op.drop_column("wardrobe_items", "location")
