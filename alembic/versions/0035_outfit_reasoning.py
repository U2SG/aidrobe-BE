"""outfit grounded reasoning (BE#37/#38)

Adds `outfits.reasoning_json` — a JSON array of {"type", "text"} grounded
reasons surfaced on the Today recommendation (gated by ENABLE_TODAY_REASONING;
the column is written regardless so flipping the flag is a clean rollback).

Revision ID: 0035_outfit_reasoning
Revises: 0034_recommendation_events
Create Date: 2026-06-24

NOTE: rebased onto 0034 (BE#40, merged first) so the history stays linear —
no merge revision needed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0035_outfit_reasoning"
down_revision: str | None = "0034_recommendation_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outfits",
        sa.Column("reasoning_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outfits", "reasoning_json")
