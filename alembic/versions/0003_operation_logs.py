"""add operation logs

Revision ID: 0003_operation_logs
Revises: 0002_auth_sessions
Create Date: 2026-05-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_operation_logs"
down_revision: str | None = "0002_auth_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operation_logs",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_operation_logs_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operation_logs")),
    )
    op.create_index(op.f("ix_operation_logs_created_at"), "operation_logs", ["created_at"])
    op.create_index(op.f("ix_operation_logs_event"), "operation_logs", ["event"])
    op.create_index(op.f("ix_operation_logs_user_id"), "operation_logs", ["user_id"])
    op.create_index(
        "ix_operation_logs_user_event_created_at",
        "operation_logs",
        ["user_id", "event", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_operation_logs_user_event_created_at", table_name="operation_logs")
    op.drop_index(op.f("ix_operation_logs_user_id"), table_name="operation_logs")
    op.drop_index(op.f("ix_operation_logs_event"), table_name="operation_logs")
    op.drop_index(op.f("ix_operation_logs_created_at"), table_name="operation_logs")
    op.drop_table("operation_logs")
