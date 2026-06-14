"""add wear_history (user_id, worn_date) composite index

Revision ID: 0027_add_wear_history_user_worn_date_index
Revises: 0026_add_object_key_columns
Create Date: 2026-06-14
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0027_add_wear_history_user_worn_date_index"
down_revision: str | None = "0026_add_object_key_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_wear_history_user_worn_date",
        "wear_history",
        ["user_id", "worn_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_wear_history_user_worn_date", "wear_history")
