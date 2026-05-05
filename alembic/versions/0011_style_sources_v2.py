"""style sources v2: async learning fields

Revision ID: 0011_style_sources_v2
Revises: 0010_fit_feedback
Create Date: 2026-05-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_style_sources_v2"
down_revision: str | None = "0010_fit_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_style_profiles",
        sa.Column("learn_status", sa.String(32), server_default="learned", nullable=False),
    )
    op.add_column(
        "user_style_profiles",
        sa.Column("learned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "user_style_profiles",
        sa.Column("source_url", sa.String(1024), nullable=True),
    )
    op.add_column(
        "user_style_profiles",
        sa.Column("learned_summary", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_style_profiles", "learned_summary")
    op.drop_column("user_style_profiles", "source_url")
    op.drop_column("user_style_profiles", "learned_at")
    op.drop_column("user_style_profiles", "learn_status")
