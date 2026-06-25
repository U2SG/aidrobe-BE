"""wardrobe item relationships for recommendation ranking

Revision ID: 0038
Revises: 0037
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "wardrobe_item_relationships",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wardrobe_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wardrobe_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relationship_type", sa.String(length=40), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.CheckConstraint(
            "score BETWEEN -1.0 AND 1.0",
            name="ck_wardrobe_item_relationships_score_range",
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0.0 AND 1.0",
            name="ck_wardrobe_item_relationships_confidence_range",
        ),
        sa.CheckConstraint(
            "relationship_type IN "
            "('pairs_well', 'avoid_pairing', 'user_approved_pairing', "
            "'user_rejected_pairing', 'style_clash', 'neutral')",
            name="ck_wardrobe_item_relationships_type_enum",
        ),
        sa.CheckConstraint(
            "source IN ('rule_based', 'user_feedback', 'llm')",
            name="ck_wardrobe_item_relationships_source_enum",
        ),
        sa.UniqueConstraint(
            "source_item_id",
            "target_item_id",
            "relationship_type",
            name="uq_wardrobe_item_relationships_pair_type",
        ),
    )
    op.create_index(
        "ix_wardrobe_item_relationships_source_item_id",
        "wardrobe_item_relationships",
        ["source_item_id"],
    )
    op.create_index(
        "ix_wardrobe_item_relationships_target_item_id",
        "wardrobe_item_relationships",
        ["target_item_id"],
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index(
        "ix_wardrobe_item_relationships_target_item_id",
        table_name="wardrobe_item_relationships",
    )
    op.drop_index(
        "ix_wardrobe_item_relationships_source_item_id",
        table_name="wardrobe_item_relationships",
    )
    op.drop_table("wardrobe_item_relationships")
