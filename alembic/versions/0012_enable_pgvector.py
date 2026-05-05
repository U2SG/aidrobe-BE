"""enable pgvector extension and convert item embeddings

Revision ID: 0012_enable_pgvector
Revises: 0011_style_sources_v2
Create Date: 2026-05-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_enable_pgvector"
down_revision: str | None = "0011_style_sources_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Convert existing JSONB arrays in place. Invalid or non-512 vectors become
    # zero vectors so the NOT NULL constraint can be preserved during rollout.
    op.execute(
        """
        ALTER TABLE item_embeddings
        ALTER COLUMN vector TYPE vector(512)
        USING (
            CASE
                WHEN jsonb_typeof(vector) = 'array'
                    AND jsonb_array_length(vector) = 512
                THEN vector::text::vector(512)
                ELSE ('[' || rtrim(repeat('0,', 512), ',') || ']')::vector(512)
            END
        )
        """
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_item_embeddings_vector "
        "ON item_embeddings "
        "USING hnsw (vector vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 200)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_item_embeddings_vector")
    op.execute(
        """
        ALTER TABLE item_embeddings
        ALTER COLUMN vector TYPE jsonb
        USING vector::text::jsonb
        """
    )
