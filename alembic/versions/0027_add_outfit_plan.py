"""add outfit_plan table (V4 Outfit Calendar)

Revision ID: 0027_add_outfit_plan
Revises: 0026_add_object_key_columns
Create Date: 2026-06-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_add_outfit_plan"
down_revision: str | None = "0026_add_object_key_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """V4 Outfit Calendar — planned wears of existing outfits.

    A day can hold multiple plans; `status` soft-cancels (planned → cancelled).
    Unique (user_id, outfit_id, planned_date) stops the same outfit being
    double-planned on the same day (different outfits still allowed). Worn
    cells of the calendar derive from wear_history; planned cells from this
    table with status='planned'."""
    op.execute("SET lock_timeout = '3s'")

    op.create_table(
        "outfit_plan",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("outfit_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("planned_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="planned",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_outfit_plan_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["outfit_id"],
            ["outfits.id"],
            name="fk_outfit_plan_outfit_id_outfits",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outfit_plan"),
        sa.UniqueConstraint(
            "user_id",
            "outfit_id",
            "planned_date",
            name="uq_outfit_plan_user_outfit_date",
        ),
    )
    op.create_index("ix_outfit_plan_user_id", "outfit_plan", ["user_id"])
    op.create_index("ix_outfit_plan_outfit_id", "outfit_plan", ["outfit_id"])
    op.create_index(
        "ix_outfit_plan_user_date", "outfit_plan", ["user_id", "planned_date"]
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("ix_outfit_plan_user_date", table_name="outfit_plan")
    op.drop_index("ix_outfit_plan_outfit_id", table_name="outfit_plan")
    op.drop_index("ix_outfit_plan_user_id", table_name="outfit_plan")
    op.drop_table("outfit_plan")
