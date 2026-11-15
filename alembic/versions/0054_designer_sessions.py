"""Persist designer outfit versions independently from the adopted daily outfit."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0054_designer_sessions"
down_revision = "0053_harden_import_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("outfits", "daily_recommendation_id", existing_type=sa.UUID(), nullable=True)
    op.add_column("ask_conversations", sa.Column("styling_revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ask_sessions", sa.Column("client_request_id", sa.UUID(), nullable=True))
    op.add_column("ask_sessions", sa.Column("request_fingerprint", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_ask_client_request", "ask_sessions", ["user_id", "client_request_id"])
    op.create_table(
        "ask_styling_revisions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("conversation_id", sa.UUID(), sa.ForeignKey("ask_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("outfit_id", sa.UUID(), sa.ForeignKey("outfits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("constraints", postgresql.JSONB(), nullable=False),
        sa.Column("recommendation_id", sa.UUID(), sa.ForeignKey("daily_recommendations.id", ondelete="SET NULL")),
        sa.Column("recommendation_revision", sa.Integer()),
        sa.Column("recommendation_date", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("conversation_id", "revision", name="uq_ask_styling_revision"),
        sa.UniqueConstraint("user_id", "request_id", name="uq_ask_styling_request"),
        sa.CheckConstraint("status IN ('draft', 'adopted')", name="styling_status"),
    )
    for column in ("conversation_id", "user_id", "outfit_id"):
        op.create_index(f"ix_ask_styling_revisions_{column}", "ask_styling_revisions", [column])


def downgrade() -> None:
    # Restoring the old NOT NULL would otherwise destroy drafts or invent a
    # daily recommendation. An operator must explicitly migrate drafts first.
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM outfits WHERE daily_recommendation_id IS NULL)")):
        raise RuntimeError("Cannot downgrade designer sessions while standalone draft outfits exist")
    op.drop_table("ask_styling_revisions")
    op.drop_constraint("uq_ask_client_request", "ask_sessions", type_="unique")
    op.drop_column("ask_sessions", "request_fingerprint")
    op.drop_column("ask_sessions", "client_request_id")
    op.drop_column("ask_conversations", "styling_revision")
    op.alter_column("outfits", "daily_recommendation_id", existing_type=sa.UUID(), nullable=False)
