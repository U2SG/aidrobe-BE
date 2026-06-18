"""user fit profiles table for V3 M9

Revision ID: 0014_user_fit_profiles
Revises: 0013_wardrobe_extended_fields
Create Date: 2026-05-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_user_fit_profiles"
down_revision: str | None = "0013_wardrobe_extended_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_fit_profiles",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("preferred_top_fit", sa.String(32), nullable=True),
        sa.Column("preferred_bottom_fit", sa.String(32), nullable=True),
        sa.Column("preferred_waist_rise", sa.String(32), nullable=True),
        sa.Column("preferred_length_tags", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("avoid_fit_tags", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("comfort_priority", sa.String(16), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_fit_profiles")
