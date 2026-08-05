"""add privacy-safe recommendation decision traces

Revision ID: 0045_add_recommendation_traces
Revises: 0044_add_user_timezone
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0045_add_recommendation_traces"
down_revision: str | None = "0044_add_user_timezone"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recommendation_traces",
        sa.Column("daily_recommendation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["daily_recommendation_id"],
            ["daily_recommendations.id"],
            name=op.f(
                "fk_recommendation_traces_daily_recommendation_id_daily_recommendations"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_traces")),
        sa.UniqueConstraint(
            "daily_recommendation_id",
            name=op.f("uq_recommendation_traces_daily_recommendation_id"),
        ),
    )


def downgrade() -> None:
    op.drop_table("recommendation_traces")
