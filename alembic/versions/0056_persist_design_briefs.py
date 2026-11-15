"""Persist accepted design conditions before a complete outfit exists.

Revision ID: 0056_persist_design_briefs
Revises: 0055_scoped_preference_memory
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0056_persist_design_briefs"
down_revision = "0055_scoped_preference_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ask_conversations", sa.Column("design_constraints", postgresql.JSONB(), nullable=True))
    op.add_column("ask_conversations", sa.Column("design_base_outfit_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_ask_conversations_design_base_outfit_id_outfits",
        "ask_conversations", "outfits", ["design_base_outfit_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    pending = op.get_bind().scalar(sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM ask_conversations AS conversation
            WHERE conversation.styling_revision <> COALESCE((
                SELECT max(revision) FROM ask_styling_revisions
                WHERE conversation_id = conversation.id
            ), 0)
        )
    """))
    if pending:
        raise RuntimeError("Cannot downgrade while accepted design briefs have no matching outfit version")
    op.drop_constraint("fk_ask_conversations_design_base_outfit_id_outfits", "ask_conversations", type_="foreignkey")
    op.drop_column("ask_conversations", "design_base_outfit_id")
    op.drop_column("ask_conversations", "design_constraints")
