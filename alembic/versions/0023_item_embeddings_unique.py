"""item_embeddings unique (item_id, model_name)

Revision ID: 0023_item_embeddings_unique
Revises: 0022_add_async_jobs
Create Date: 2026-05-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023_item_embeddings_unique"
down_revision: str | None = "0022_add_async_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Without this constraint, a stale embedding worker that finished its
    embed call after its lease was recovered could persist a second row for
    the same (item_id, model_name) — corrupting search results with a
    duplicate vector that nobody asked for. The new unique index is what
    lets the worker safely upsert / fence its writes."""
    op.execute("SET lock_timeout = '3s'")
    # Drop pre-existing duplicates so the unique index can build. Keep the
    # newest row per (item_id, model_name) — `created_at DESC` is the
    # intended-survivor signal (older vector is stale by definition since
    # the worker only writes a fresh row when re-embedding) and `id DESC`
    # is a deterministic tie-breaker. Audit-friendly RAISE NOTICE so the
    # operator sees how many rows the migration ate.
    op.execute(
        """
        DO $$
        DECLARE
            deleted_count int;
        BEGIN
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY item_id, model_name
                           ORDER BY created_at DESC, id DESC
                       ) AS rn
                FROM item_embeddings
            )
            DELETE FROM item_embeddings
            WHERE id IN (SELECT id FROM ranked WHERE rn > 1);
            GET DIAGNOSTICS deleted_count = ROW_COUNT;
            IF deleted_count > 0 THEN
                RAISE NOTICE 'item_embeddings dedup: removed % stale duplicate row(s)', deleted_count;
            END IF;
        END $$;
        """
    )
    op.create_index(
        "uq_item_embeddings_item_model",
        "item_embeddings",
        ["item_id", "model_name"],
        unique=True,
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("uq_item_embeddings_item_model", table_name="item_embeddings")
