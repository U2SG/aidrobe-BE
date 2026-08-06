"""add purchase candidates and immutable wardrobe decision snapshots

Additive only (V3-003 human gate: no destructive schema change). Both tables
live in a single revision so `alembic downgrade -1` rolls the whole feature
back in one step.

Revision ID: 0046_add_purchase_decision_persistence
Revises: 0045_add_recommendation_traces
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0046_add_purchase_decision_persistence"
down_revision: str | None = "0045_add_recommendation_traces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "purchase_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("source_object_key", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("parsed_item", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_purchase_candidates_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_candidates")),
        sa.CheckConstraint(
            "source_type IN ('share_link', 'screenshot', 'order_image', 'ask_link')",
            name=op.f("ck_purchase_candidates_source_type_enum"),
        ),
        sa.CheckConstraint(
            "status IN ('submitted', 'parsed', 'parse_failed')",
            name=op.f("ck_purchase_candidates_status_enum"),
        ),
        sa.CheckConstraint(
            "num_nonnulls(source_url, source_object_key) = 1",
            name=op.f("ck_purchase_candidates_one_source_reference"),
        ),
    )
    op.create_index(
        "ix_purchase_candidates_user_id_created_at",
        "purchase_candidates",
        ["user_id", "created_at"],
    )

    op.create_table(
        "wardrobe_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("outfit_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("context_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("uncertainty", sa.String(length=8), nullable=False),
        sa.Column("visual_need", sa.String(length=16), nullable=False),
        # No updated_at: a decision snapshot is written once and replayed
        # forever, so the schema itself denies revision.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_wardrobe_decisions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["purchase_candidates.id"],
            name=op.f("fk_wardrobe_decisions_candidate_id_purchase_candidates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wardrobe_decisions")),
        sa.CheckConstraint(
            "decision IN ('worth_adding', 'consider_alternate', 'wait', 'avoid_duplicate', 'insufficient_information')",
            name=op.f("ck_wardrobe_decisions_decision_enum"),
        ),
        sa.CheckConstraint(
            "uncertainty IN ('low', 'medium', 'high')",
            name=op.f("ck_wardrobe_decisions_uncertainty_enum"),
        ),
        sa.CheckConstraint(
            "visual_need IN ('none', 'helpful', 'recommended')",
            name=op.f("ck_wardrobe_decisions_visual_need_enum"),
        ),
    )
    op.create_index(
        "ix_wardrobe_decisions_user_id_created_at",
        "wardrobe_decisions",
        ["user_id", "created_at"],
    )
    # One decision per candidate — NULL candidate_id rows (non-purchase
    # decisions) are exempt, since Postgres unique indexes allow many NULLs.
    op.create_index(
        "uq_wardrobe_decisions_candidate_id",
        "wardrobe_decisions",
        ["candidate_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_wardrobe_decisions_candidate_id", table_name="wardrobe_decisions")
    op.drop_index("ix_wardrobe_decisions_user_id_created_at", table_name="wardrobe_decisions")
    op.drop_table("wardrobe_decisions")
    op.drop_index("ix_purchase_candidates_user_id_created_at", table_name="purchase_candidates")
    op.drop_table("purchase_candidates")
