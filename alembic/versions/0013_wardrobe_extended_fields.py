"""wardrobe extended fields for V3 M14

Revision ID: 0013_wardrobe_extended_fields
Revises: 0012_enable_pgvector
Create Date: 2026-05-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0013_wardrobe_extended_fields"
down_revision: str | None = "0012_enable_pgvector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("wardrobe_items", sa.Column("sub_category", sa.String(32), nullable=True))
    op.add_column("wardrobe_items", sa.Column("color_secondary", sa.String(32), nullable=True))
    op.add_column("wardrobe_items", sa.Column("material", sa.String(64), nullable=True))
    op.add_column(
        "wardrobe_items",
        sa.Column("season_tags", JSONB(), nullable=True),
    )
    op.add_column(
        "wardrobe_items",
        sa.Column("style_tags", JSONB(), nullable=True),
    )
    op.add_column(
        "wardrobe_items", sa.Column("formality_score", sa.SmallInteger(), nullable=True)
    )
    op.add_column(
        "wardrobe_items", sa.Column("time_cost_level", sa.String(16), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("wardrobe_items", "time_cost_level")
    op.drop_column("wardrobe_items", "formality_score")
    op.drop_column("wardrobe_items", "style_tags")
    op.drop_column("wardrobe_items", "season_tags")
    op.drop_column("wardrobe_items", "material")
    op.drop_column("wardrobe_items", "color_secondary")
    op.drop_column("wardrobe_items", "sub_category")
