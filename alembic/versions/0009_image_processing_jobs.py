"""add image processing jobs

Revision ID: 0009_image_processing_jobs
Revises: 0008_import_task_source_types
Create Date: 2026-05-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_image_processing_jobs"
down_revision: str | None = "0008_import_task_source_types"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "image_processing_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), server_default="queued", nullable=False),
        sa.Column("source_image_url", sa.Text, nullable=True),
        sa.Column("result_payload", postgresql.JSONB(), nullable=True),
        sa.Column("retry_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name=op.f("fk_image_processing_jobs_user_id_users")),
        sa.ForeignKeyConstraint(["item_id"], ["wardrobe_items.id"], ondelete="SET NULL",
                                name=op.f("fk_image_processing_jobs_item_id_wardrobe_items")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_image_processing_jobs")),
    )
    op.create_index(op.f("ix_image_processing_jobs_user_id"), "image_processing_jobs", ["user_id"])
    op.create_index(op.f("ix_image_processing_jobs_item_id"), "image_processing_jobs", ["item_id"])
    op.create_index(op.f("ix_image_processing_jobs_status"), "image_processing_jobs", ["status", "created_at"])

    # extend wardrobe_item_images
    op.add_column("wardrobe_item_images", sa.Column("quality_score", sa.Float, nullable=True))
    op.add_column("wardrobe_item_images", sa.Column("quality_flags", postgresql.JSONB(), nullable=True))
    op.add_column("wardrobe_item_images", sa.Column("width", sa.Integer, nullable=True))
    op.add_column("wardrobe_item_images", sa.Column("height", sa.Integer, nullable=True))
    op.add_column("wardrobe_item_images", sa.Column("file_size", sa.Integer, nullable=True))
    op.add_column("wardrobe_item_images", sa.Column("is_primary", sa.Boolean, server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("wardrobe_item_images", "is_primary")
    op.drop_column("wardrobe_item_images", "file_size")
    op.drop_column("wardrobe_item_images", "height")
    op.drop_column("wardrobe_item_images", "width")
    op.drop_column("wardrobe_item_images", "quality_flags")
    op.drop_column("wardrobe_item_images", "quality_score")
    op.drop_index(op.f("ix_image_processing_jobs_status"), table_name="image_processing_jobs")
    op.drop_index(op.f("ix_image_processing_jobs_item_id"), table_name="image_processing_jobs")
    op.drop_index(op.f("ix_image_processing_jobs_user_id"), table_name="image_processing_jobs")
    op.drop_table("image_processing_jobs")
