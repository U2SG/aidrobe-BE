"""add commute_exposure to user_settings

Revision ID: 0042_add_commute_exposure
Revises: 0041_add_packing_plan_json
Create Date: 2026-07-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0042_add_commute_exposure"
down_revision: str | None = "0041_add_packing_plan_json"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column(
            "commute_exposure",
            sa.String(16),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_user_settings_commute_exposure",
        "user_settings",
        "commute_exposure IN ('more_outdoor', 'less_outdoor')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_user_settings_commute_exposure", "user_settings", type_="check"
    )
    op.drop_column("user_settings", "commute_exposure")
