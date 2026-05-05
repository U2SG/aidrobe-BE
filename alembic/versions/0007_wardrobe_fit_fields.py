"""add wardrobe fit fields

Revision ID: 0007_wardrobe_fit_fields
Revises: 0006_style_profiles
Create Date: 2026-05-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_wardrobe_fit_fields"
down_revision: str | None = "0006_style_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("wardrobe_items", sa.Column("size_label", sa.String(length=32), nullable=True))
    op.add_column("wardrobe_items", sa.Column("fit_intent", sa.String(length=32), nullable=True))
    op.add_column(
        "wardrobe_items",
        sa.Column("fit_status", sa.String(length=32), server_default="unknown", nullable=False),
    )
    op.add_column("wardrobe_items", sa.Column("comfort_score", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("wardrobe_items", "comfort_score")
    op.drop_column("wardrobe_items", "fit_status")
    op.drop_column("wardrobe_items", "fit_intent")
    op.drop_column("wardrobe_items", "size_label")
