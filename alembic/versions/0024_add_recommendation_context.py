"""add daily_recommendations.context_json

Revision ID: 0024_add_recommendation_context
Revises: 0023_item_embeddings_unique
Create Date: 2026-05-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0024_add_recommendation_context"
down_revision: str | None = "0023_item_embeddings_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Phase D — daily_recommendation context (weather/scene snapshot at
    generation time). Nullable so existing rows are valid; flag-gated reads
    means clients see the field only when ENABLE_TODAY_CONTEXT=true."""
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        "daily_recommendations",
        sa.Column("context_json", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_column("daily_recommendations", "context_json")
