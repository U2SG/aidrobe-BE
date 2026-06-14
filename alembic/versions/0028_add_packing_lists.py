"""add packing_lists table (M16 Travel Packing List)

Revision ID: 0028_add_packing_lists
Revises: 0026_add_object_key_columns
Create Date: 2026-06-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0028_add_packing_lists"
down_revision: str | None = "0026_add_object_key_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """M16 — rule-based travel packing lists generated from the wardrobe.

    `scenes` stores the requested scene tags; `items` the generated selection
    as [{item_id, day, scene, notes}]. `status` is draft until the user
    confirms. Lists persist regardless of the ENABLE_PACKING_LIST flag — the
    flag only gates endpoint exposure, so flipping it back is a clean rollback.
    """
    op.execute("SET lock_timeout = '3s'")

    op.create_table(
        "packing_lists",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("destination", sa.String(length=120), nullable=True),
        sa.Column("days", sa.SmallInteger(), nullable=False),
        sa.Column("scenes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("departure_date", sa.Date(), nullable=True),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="draft",
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
            name="fk_packing_lists_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_packing_lists"),
    )
    op.create_index("ix_packing_lists_user_id", "packing_lists", ["user_id"])


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("ix_packing_lists_user_id", table_name="packing_lists")
    op.drop_table("packing_lists")
