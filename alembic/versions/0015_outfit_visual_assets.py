"""outfit visual assets table for V3 M10

Revision ID: 0015_outfit_visual_assets
Revises: 0014_user_fit_profiles
Create Date: 2026-05-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0015_outfit_visual_assets"
down_revision: str | None = "0014_user_fit_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outfit_visual_assets",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("outfit_id", sa.UUID(), nullable=True),
        sa.Column("visual_type", sa.String(32), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("template_id", sa.String(64), nullable=True),
        sa.Column("generation_status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("input_snapshot", JSONB(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outfit_visual_assets_user_status",
        "outfit_visual_assets",
        ["user_id", "generation_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_outfit_visual_assets_user_status", table_name="outfit_visual_assets")
    op.drop_table("outfit_visual_assets")
