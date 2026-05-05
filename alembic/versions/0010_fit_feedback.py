"""add fit feedback

Revision ID: 0010_fit_feedback
Revises: 0008_import_task_source_types
Create Date: 2026-05-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_fit_feedback"
down_revision: str | None = "0008_import_task_source_types"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("wardrobe_items", sa.Column("size_region", sa.String(length=32), nullable=True))
    op.create_table(
        "fit_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("outfit_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("feedback_type", sa.String(length=32), nullable=False),
        sa.Column("scene", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_fit_feedback_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["wardrobe_items.id"],
            name=op.f("fk_fit_feedback_item_id_wardrobe_items"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fit_feedback")),
    )
    op.create_index(op.f("ix_fit_feedback_user_id"), "fit_feedback", ["user_id"])
    op.create_index(op.f("ix_fit_feedback_item_id"), "fit_feedback", ["item_id"])
    op.create_index(op.f("ix_fit_feedback_outfit_id"), "fit_feedback", ["outfit_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_fit_feedback_outfit_id"), table_name="fit_feedback")
    op.drop_index(op.f("ix_fit_feedback_item_id"), table_name="fit_feedback")
    op.drop_index(op.f("ix_fit_feedback_user_id"), table_name="fit_feedback")
    op.drop_table("fit_feedback")
    op.drop_column("wardrobe_items", "size_region")
