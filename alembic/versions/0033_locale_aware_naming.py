"""locale-aware clothing names (BE#30)

Adds:
- user_settings.locale — UI locale ('zh-CN' | 'en-US' | 'system').
- wardrobe_items.canonical_name / localized_names / name_source — AI naming
  provenance + per-locale display names.

Revision ID: 0033_locale_aware_naming
Revises: 0032_add_item_cutout_status
Create Date: 2026-06-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0033_locale_aware_naming"
down_revision: str | None = "0032_add_item_cutout_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """All columns set unconditionally (no feature flag) with backfilling
    server_defaults so the change is a clean, reversible no-behavior-loss add:
    - user_settings.locale -> 'system' (defers to the zh-CN product default).
    - wardrobe_items.name_source -> 'ai' (existing AI-named imports).
    - canonical_name / localized_names left NULL for existing rows.
    """
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        "user_settings",
        sa.Column(
            "locale",
            sa.String(length=16),
            nullable=False,
            server_default="system",
        ),
    )
    op.add_column(
        "wardrobe_items",
        sa.Column("canonical_name", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "wardrobe_items",
        sa.Column("localized_names", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "wardrobe_items",
        sa.Column(
            "name_source",
            sa.String(length=8),
            nullable=False,
            server_default="ai",
        ),
    )


def downgrade() -> None:
    op.drop_column("wardrobe_items", "name_source")
    op.drop_column("wardrobe_items", "localized_names")
    op.drop_column("wardrobe_items", "canonical_name")
    op.drop_column("user_settings", "locale")
