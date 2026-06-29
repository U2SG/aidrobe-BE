"""add packing_lists.plan_json (BE#68 travel packing planner)

Revision ID: 0041_add_packing_plan_json
Revises: 0040_add_outfit_favorites
Create Date: 2026-06-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0041_add_packing_plan_json"
down_revision: str | None = "0040_add_outfit_favorites"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        "packing_lists",
        sa.Column("plan_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_column("packing_lists", "plan_json")
