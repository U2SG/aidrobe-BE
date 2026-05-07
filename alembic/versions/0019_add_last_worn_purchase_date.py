"""add last_worn_at and purchase_date to wardrobe_items

Revision ID: 0019_add_last_worn_purchase_date
Revises: 0018_add_daily_recommendation_versioning
Create Date: 2026-05-06
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_add_last_worn_purchase_date"
down_revision: str | None = "0018_add_daily_recommendation_versioning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")

    op.add_column("wardrobe_items", sa.Column("last_worn_at", sa.Date(), nullable=True))
    op.add_column("wardrobe_items", sa.Column("purchase_date", sa.Date(), nullable=True))

    # Cursor-based backfill: advance by id range each iteration so rows without
    # any wear_history don't keep recycling through the WHERE last_worn_at IS NULL
    # filter (which would either spin forever or terminate early and leave later
    # rows unprocessed).
    batch_sql = sa.text(
        """
        WITH batch AS (
            SELECT id FROM wardrobe_items
            WHERE id > :cursor
            ORDER BY id
            LIMIT 5000
        ),
        latest AS (
            SELECT oi.item_id, MAX(wh.worn_date) AS last_worn
            FROM wear_history wh
            JOIN outfit_items oi ON oi.outfit_id = wh.outfit_id
            WHERE oi.item_id IN (SELECT id FROM batch)
            GROUP BY oi.item_id
        )
        UPDATE wardrobe_items w
        SET last_worn_at = latest.last_worn
        FROM latest
        WHERE w.id = latest.item_id AND w.last_worn_at IS NULL
        """
    )
    max_id_sql = sa.text(
        "SELECT id FROM wardrobe_items WHERE id > :cursor "
        "ORDER BY id LIMIT 5000 OFFSET 4999"
    )
    conn = op.get_bind()
    cursor = "00000000-0000-0000-0000-000000000000"
    while True:
        # Find the upper bound of this batch BEFORE updating, so we can advance
        # the cursor regardless of how many rows actually got a wear_history match.
        max_row = conn.execute(max_id_sql, {"cursor": cursor}).first()
        conn.execute(batch_sql, {"cursor": cursor})
        if max_row is None:
            # Fewer than 5000 rows remaining — process them then stop.
            break
        cursor = str(max_row[0])

    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_wardrobe_items_user_last_worn "
            "ON wardrobe_items (user_id, last_worn_at DESC NULLS LAST)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_wardrobe_items_user_last_worn")

    op.drop_column("wardrobe_items", "purchase_date")
    op.drop_column("wardrobe_items", "last_worn_at")
