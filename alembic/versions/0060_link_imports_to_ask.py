"""Link import tasks to the Ask conversation the photos were attached in.

Revision ID: 0060_link_imports_to_ask
Revises: 0059_track_item_costs_setting
"""

import sqlalchemy as sa

from alembic import op

revision = "0060_link_imports_to_ask"
down_revision = "0059_track_item_costs_setting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_tasks", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_import_tasks_conversation_id_ask_conversations",
        "import_tasks", "ask_conversations", ["conversation_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_import_tasks_user_conversation", "import_tasks", ["user_id", "conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_import_tasks_user_conversation", table_name="import_tasks")
    op.drop_constraint("fk_import_tasks_conversation_id_ask_conversations", "import_tasks", type_="foreignkey")
    op.drop_column("import_tasks", "conversation_id")
