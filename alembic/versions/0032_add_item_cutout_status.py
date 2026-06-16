"""wardrobe_items.cutout_status (V4 — gpt-image-2 product-shot cutout)

Revision ID: 0032_add_item_cutout_status
Revises: 0031_merge_v4_parallel_migrations
Create Date: 2026-06-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032_add_item_cutout_status"
down_revision: str | None = "0031_merge_v4_parallel_migrations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Track the async cutout lifecycle on the item.

    server_default 'skipped' backfills existing rows to the rollback state —
    they just keep showing their original photo. The column is set
    unconditionally regardless of ENABLE_ITEM_CUTOUT so flipping the flag is a
    clean rollback with no schema churn."""
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        "wardrobe_items",
        sa.Column(
            "cutout_status",
            sa.String(length=16),
            nullable=False,
            server_default="skipped",
        ),
    )


def downgrade() -> None:
    op.drop_column("wardrobe_items", "cutout_status")
