"""Keep appreciation, trial intent and evidenced wear feedback distinct.

Revision ID: 0057_styling_experiences
Revises: 0056_persist_design_briefs
"""

import sqlalchemy as sa

from alembic import op

revision = "0057_styling_experiences"
down_revision = "0056_persist_design_briefs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "styling_experiences",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("outfit_id", sa.Uuid(), sa.ForeignKey("outfits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("ask_conversations.id", ondelete="SET NULL")),
        sa.Column("revision", sa.Integer()),
        sa.Column("worn_date", sa.Date()),
        sa.Column("wear_history_id", sa.Uuid(), sa.ForeignKey("wear_history.id", ondelete="CASCADE")),
        sa.Column("feedback", sa.String(16)),
        sa.Column("note", sa.Text()),
        sa.Column("occasion", sa.String(24)),
        sa.Column("supersedes_id", sa.Uuid(), sa.ForeignKey("styling_experiences.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "request_id", name="uq_styling_experience_request"),
        sa.CheckConstraint(
            "kind IN ('appreciated', 'want_to_try', 'wear_feedback')", name="styling_experience_kind"
        ),
        sa.CheckConstraint(
            "(kind = 'wear_feedback' AND worn_date IS NOT NULL AND wear_history_id IS NOT NULL "
            "AND feedback IS NOT NULL AND feedback IN ('like', 'neutral', 'dislike')) OR "
            "(kind <> 'wear_feedback' AND worn_date IS NULL AND wear_history_id IS NULL AND feedback IS NULL)",
            name="styling_experience_wear_evidence",
        ),
    )
    op.create_index("ix_styling_experiences_user_created", "styling_experiences", ["user_id", "created_at"])
    op.create_index("ix_styling_experiences_user_outfit", "styling_experiences", ["user_id", "outfit_id"])
    op.create_index(
        "uq_styling_experience_live_nonwear", "styling_experiences", ["user_id", "outfit_id", "kind"],
        unique=True, postgresql_where=sa.text("deleted_at IS NULL AND kind <> 'wear_feedback'"),
    )
    op.create_index(
        "uq_styling_experience_live_wear", "styling_experiences", ["user_id", "outfit_id", "worn_date"],
        unique=True, postgresql_where=sa.text("deleted_at IS NULL AND kind = 'wear_feedback'"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM styling_experiences)")):
        raise RuntimeError("Cannot downgrade while user styling experiences exist")
    op.drop_table("styling_experiences")
