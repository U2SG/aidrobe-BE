"""Link saved packing lists to the design conversation and outfit revision.

Revision ID: 0057_link_packing_to_designs
Revises: 0056_persist_design_briefs
"""

import sqlalchemy as sa

from alembic import op

revision = "0057_link_packing_to_designs"
down_revision = "0056_persist_design_briefs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("packing_lists", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.add_column("packing_lists", sa.Column("styling_revision_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_packing_lists_conversation_id_ask_conversations",
        "packing_lists", "ask_conversations", ["conversation_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_packing_lists_styling_revision_id_ask_styling_revisions",
        "packing_lists", "ask_styling_revisions", ["styling_revision_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_packing_lists_user_conversation", "packing_lists", ["user_id", "conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_packing_lists_user_conversation", table_name="packing_lists")
    op.drop_constraint("fk_packing_lists_styling_revision_id_ask_styling_revisions", "packing_lists", type_="foreignkey")
    op.drop_constraint("fk_packing_lists_conversation_id_ask_conversations", "packing_lists", type_="foreignkey")
    op.drop_column("packing_lists", "styling_revision_id")
    op.drop_column("packing_lists", "conversation_id")
