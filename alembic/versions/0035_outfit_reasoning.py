"""outfit grounded reasoning (BE#37/#38)

Adds `outfits.reasoning_json` — a JSON array of {"type", "text"} grounded
reasons surfaced on the Today recommendation (gated by ENABLE_TODAY_REASONING;
the column is written regardless so flipping the flag is a clean rollback).

Revision ID: 0035_outfit_reasoning
Revises: 0033_locale_aware_naming
Create Date: 2026-06-24

NOTE: sibling Alembic head off 0033 (BE#40 adds 0034_recommendation_events off
the same parent). Whichever BE PR merges second should run `alembic merge
heads` (or rebase its down_revision). Kept off 0033 so either can merge first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0035_outfit_reasoning"
down_revision: str | None = "0033_locale_aware_naming"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outfits",
        sa.Column("reasoning_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outfits", "reasoning_json")
