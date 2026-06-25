"""add object_key to shopping_sense_checks

Revision ID: 0039_shopping_sense_check_object_key
Revises: 0036_user_preference_memory
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0039_shopping_sense_check_object_key"
down_revision: str | None = "0036_user_preference_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        "shopping_sense_checks",
        sa.Column("object_key", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_column("shopping_sense_checks", "object_key")
