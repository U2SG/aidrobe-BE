"""Opt-in setting for purchase prices and cost per wear.

Revision ID: 0059_track_item_costs_setting
Revises: 0058_link_purchase_decisions
"""

import sqlalchemy as sa

from alembic import op

revision = "0059_track_item_costs_setting"
down_revision = "0058_link_purchase_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("track_item_costs", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "track_item_costs")
