"""ask_conversations tables - BE#47 conversational refinement

Revision ID: 0037
Revises: 0036_user_preference_memory
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036_user_preference_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "ask_conversations",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_ask_conversations_user_updated",
        "ask_conversations",
        ["user_id", "updated_at"],
    )

    op.create_table(
        "ask_messages",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ask_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ask_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ask_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_ask_messages_role_enum",
        ),
    )
    op.create_index(
        "ix_ask_messages_conversation_created",
        "ask_messages",
        ["conversation_id", "created_at"],
    )
    op.create_index(
        "ix_ask_messages_user_created",
        "ask_messages",
        ["user_id", "created_at"],
    )

    op.create_table(
        "ask_conversation_state",
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ask_conversations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("last_outfit", JSONB(), nullable=True),
        sa.Column("active_constraints", JSONB(), nullable=True),
        sa.Column("rejected_item_ids", JSONB(), nullable=True),
        sa.Column("preferred_item_ids", JSONB(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_ask_conversation_state_user_id",
        "ask_conversation_state",
        ["user_id"],
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("ix_ask_conversation_state_user_id", table_name="ask_conversation_state")
    op.drop_table("ask_conversation_state")
    op.drop_index("ix_ask_messages_user_created", table_name="ask_messages")
    op.drop_index("ix_ask_messages_conversation_created", table_name="ask_messages")
    op.drop_table("ask_messages")
    op.drop_index("ix_ask_conversations_user_updated", table_name="ask_conversations")
    op.drop_table("ask_conversations")
