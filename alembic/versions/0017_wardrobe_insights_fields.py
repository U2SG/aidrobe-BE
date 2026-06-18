"""wardrobe insights fields for V3 M12

Revision ID: 0017_wardrobe_insights_fields
Revises: 0016_ootd_field
Create Date: 2026-05-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_wardrobe_insights_fields"
down_revision: str | None = "0016_ootd_field"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "wardrobe_items",
        sa.Column("purchase_price", sa.Numeric(10, 2), nullable=True),
    )
    op.add_column(
        "wardrobe_items",
        sa.Column("wear_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("wardrobe_items", "wear_count")
    op.drop_column("wardrobe_items", "purchase_price")
