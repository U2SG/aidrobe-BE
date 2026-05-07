"""add outfit explainability columns: scores, obstacles, prep

Revision ID: 0021_add_outfit_explainability
Revises: 0020_wear_history_idempotency
Create Date: 2026-05-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0021_add_outfit_explainability"
down_revision: str | None = "0020_wear_history_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # All 4 columns are NULLable with no defaults → metadata-only change in
    # Postgres 11+ (catalog ACCESS EXCLUSIVE only, no rewrite).
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        "outfits",
        sa.Column("score_breakdown_json", JSONB(), nullable=True),
    )
    op.add_column(
        "outfits",
        sa.Column("score_rules_version", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "outfits",
        sa.Column("obstacles_json", JSONB(), nullable=True),
    )
    op.add_column(
        "outfits",
        sa.Column("prep_json", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_column("outfits", "prep_json")
    op.drop_column("outfits", "obstacles_json")
    op.drop_column("outfits", "score_rules_version")
    op.drop_column("outfits", "score_breakdown_json")
