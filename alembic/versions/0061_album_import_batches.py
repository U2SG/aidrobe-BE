"""Album scan: batch + capture date on import tasks, cached same-garment verdicts.

Revision ID: 0061_album_import_batches
Revises: 0060_link_imports_to_ask
"""

import sqlalchemy as sa

from alembic import op

revision = "0061_album_import_batches"
down_revision = "0060_link_imports_to_ask"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_tasks", sa.Column("batch_id", sa.Uuid(), nullable=True))
    op.add_column("import_tasks", sa.Column("taken_at", sa.Date(), nullable=True))
    op.create_index("ix_import_tasks_user_batch", "import_tasks", ["user_id", "batch_id"])
    op.create_table(
        "import_batch_verdicts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("image_a", sa.Text(), nullable=False),
        sa.Column("image_b", sa.Text(), nullable=False),
        sa.Column("same", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "batch_id", "image_a", "image_b", name="uq_import_batch_verdicts_user_id"),
    )


def downgrade() -> None:
    op.drop_table("import_batch_verdicts")
    op.drop_index("ix_import_tasks_user_batch", table_name="import_tasks")
    op.drop_column("import_tasks", "taken_at")
    op.drop_column("import_tasks", "batch_id")
