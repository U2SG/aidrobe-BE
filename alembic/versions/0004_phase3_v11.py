"""add v11 backend fields

Revision ID: 0004_phase3_v11
Revises: 0003_operation_logs
Create Date: 2026-05-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_phase3_v11"
down_revision: str | None = "0003_operation_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("device_token", sa.String(length=255), nullable=True))
    op.add_column(
        "user_settings",
        sa.Column("push_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column("user_settings", sa.Column("location", sa.String(length=120), nullable=True))
    op.add_column(
        "user_settings",
        sa.Column("preferences_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "outfits",
        sa.Column(
            "time_sensitivity",
            sa.String(length=32),
            nullable=False,
            server_default="standard",
        ),
    )
    op.add_column(
        "outfit_feedback",
        sa.Column("reason_tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outfit_feedback", "reason_tags")
    op.drop_column("outfits", "time_sensitivity")
    op.drop_column("user_settings", "preferences_json")
    op.drop_column("user_settings", "location")
    op.drop_column("user_settings", "push_enabled")
    op.drop_column("user_settings", "device_token")
