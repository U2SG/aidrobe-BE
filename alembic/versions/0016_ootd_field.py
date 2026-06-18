"""add ootd_image_url to wear_history for V3 M11

Revision ID: 0016_ootd_field
Revises: 0015_outfit_visual_assets
Create Date: 2026-05-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_ootd_field"
down_revision: str | None = "0015_outfit_visual_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("wear_history", sa.Column("ootd_image_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("wear_history", "ootd_image_url")
