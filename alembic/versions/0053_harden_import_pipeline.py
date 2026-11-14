"""Add import run identity and current image source versions."""

import sqlalchemy as sa

from alembic import op

revision = "0053_harden_import_pipeline"
down_revision = "0052_add_paid_generation_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_tasks", sa.Column("run_generation", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("import_tasks", sa.Column("retryable", sa.Boolean(), nullable=True))
    op.add_column("wardrobe_items", sa.Column("embedding_source_version", sa.String(128), nullable=True))
    op.add_column("item_embeddings", sa.Column("source_version", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("item_embeddings", "source_version")
    op.drop_column("wardrobe_items", "embedding_source_version")
    op.drop_column("import_tasks", "retryable")
    op.drop_column("import_tasks", "run_generation")
