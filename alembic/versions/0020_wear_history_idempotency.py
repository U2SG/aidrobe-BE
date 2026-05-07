"""wear_history idempotency: dedup + unique index, last_worn_at sync

Revision ID: 0020_wear_history_idempotency
Revises: 0019_add_last_worn_purchase_date
Create Date: 2026-05-06
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_wear_history_idempotency"
down_revision: str | None = "0019_add_last_worn_purchase_date"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Defensive cleanup: keep the earliest wear_history per (user, outfit, worn_date).
    # PG has no MIN() aggregate for uuid; rank by created_at, tiebreak by id::text.
    # Batch the DELETE to avoid a long exclusive lock on large tables.
    conn = op.get_bind()
    batch_sql = sa.text(
        """
        DELETE FROM wear_history
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY user_id, outfit_id, worn_date
                    ORDER BY created_at ASC, id::text ASC
                ) AS rn
                FROM wear_history
                WHERE (user_id, outfit_id, worn_date) IN (
                    SELECT user_id, outfit_id, worn_date
                    FROM wear_history
                    GROUP BY 1, 2, 3
                    HAVING count(*) > 1
                    LIMIT 5000
                )
            ) ranked
            WHERE rn > 1
        )
        """
    )
    while True:
        result = conn.execute(batch_sql)
        if result.rowcount == 0:
            break

    with op.get_context().autocommit_block():
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_wear_history_user_outfit_date "
            "ON wear_history (user_id, outfit_id, worn_date)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_wear_history_user_outfit_date"
        )
