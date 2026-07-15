"""add user settings timezone

Revision ID: 0044_add_user_timezone
Revises: 0043_add_event_signal_source
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044_add_user_timezone"
down_revision: str | None = "0043_add_event_signal_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("timezone", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "timezone")
