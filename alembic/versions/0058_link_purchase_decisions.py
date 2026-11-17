"""Link immutable purchase evaluations to designs; store user choices separately.

Revision ID: 0058_link_purchase_decisions
Revises: 0057_link_packing_to_designs
"""

import sqlalchemy as sa

from alembic import op

revision = "0058_link_purchase_decisions"
down_revision = "0057_link_packing_to_designs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("purchase_candidates", sa.Column("client_request_id", sa.Uuid(), nullable=True))
    op.create_index(
        "uq_purchase_candidates_user_request", "purchase_candidates", ["user_id", "client_request_id"], unique=True,
    )
    for column in (
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("design_revision", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
    ):
        op.add_column("wardrobe_decisions", column)
    op.create_check_constraint(
        op.f("ck_wardrobe_decisions_complete_design_context"), "wardrobe_decisions",
        "num_nonnulls(conversation_id, design_revision, request_id, request_fingerprint) IN (0, 4)",
    )
    op.drop_index("uq_wardrobe_decisions_candidate_id", table_name="wardrobe_decisions")
    op.create_index(
        "uq_wardrobe_decisions_legacy_candidate", "wardrobe_decisions", ["candidate_id"],
        unique=True, postgresql_where=sa.text("conversation_id IS NULL"),
    )
    op.create_index(
        "uq_wardrobe_decisions_user_request", "wardrobe_decisions", ["user_id", "request_id"], unique=True,
    )
    op.create_index(
        "ix_wardrobe_decisions_user_conversation", "wardrobe_decisions",
        ["user_id", "conversation_id", "created_at"],
    )
    op.create_table(
        "wardrobe_decision_choices",
        sa.Column("decision_id", sa.Uuid(), sa.ForeignKey("wardrobe_decisions.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("choice", sa.String(8), nullable=True),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("ask_session_id", sa.Uuid(), sa.ForeignKey("ask_sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("choice IS NULL OR choice IN ('buy', 'skip', 'later')", name="choice_enum"),
        sa.CheckConstraint("source IN ('button', 'ask')", name="source_enum"),
    )


def downgrade() -> None:
    # Refuse before changing schema: even one linked result or saved choice
    # contains provenance the previous schema cannot preserve. Flag rollback
    # remains available without dropping the user's history.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM wardrobe_decisions WHERE request_id IS NOT NULL)
             OR EXISTS (SELECT 1 FROM wardrobe_decision_choices)
             OR EXISTS (SELECT 1 FROM purchase_candidates WHERE client_request_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Cannot downgrade 0058: linked purchase history or retry identities exist; preserve data first';
          END IF;
        END $$;
    """)
    op.drop_table("wardrobe_decision_choices")
    op.drop_index("ix_wardrobe_decisions_user_conversation", table_name="wardrobe_decisions")
    op.drop_index("uq_wardrobe_decisions_user_request", table_name="wardrobe_decisions")
    op.drop_index("uq_wardrobe_decisions_legacy_candidate", table_name="wardrobe_decisions")
    op.create_index("uq_wardrobe_decisions_candidate_id", "wardrobe_decisions", ["candidate_id"], unique=True)
    op.drop_constraint(op.f("ck_wardrobe_decisions_complete_design_context"), "wardrobe_decisions", type_="check")
    for name in ("request_fingerprint", "request_id", "design_revision", "conversation_id"):
        op.drop_column("wardrobe_decisions", name)
    op.drop_index("uq_purchase_candidates_user_request", table_name="purchase_candidates")
    op.drop_column("purchase_candidates", "client_request_id")
