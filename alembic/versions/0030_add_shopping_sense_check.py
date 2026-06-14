"""shopping_sense_checks table (M17 — purchase sense-check history)

Revision ID: 0030_add_shopping_sense_check
Revises: 0026_add_object_key_columns
Create Date: 2026-06-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0030_add_shopping_sense_check"
# Branches off 0026 (parallel V4 worktrees produce a multi-head tree; a later
# merge revision unifies them). down_revision is 0026 per task spec.
down_revision: str | None = "0026_add_object_key_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """M17 — persist every sense-check verdict regardless of the feature flag.

    The flag only gates exposure; storing the row unconditionally means
    flipping ENABLE_SENSE_CHECK back off is a clean rollback with no data
    loss. `payload` holds the full response snapshot (similar_items /
    wardrobe_gap / outfit_potential / identified candidate attributes) so
    /history replays without recomputing against a mutated wardrobe."""
    op.execute("SET lock_timeout = '3s'")

    op.create_table(
        "shopping_sense_checks",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("image_url", sa.String(length=1024), nullable=True),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_shopping_sense_checks_user_id_users",
            ondelete="CASCADE",
        ),
    )
    # /history lists a user's checks most-recent-first.
    op.create_index(
        "ix_shopping_sense_checks_user_id_created_at",
        "shopping_sense_checks",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index(
        "ix_shopping_sense_checks_user_id_created_at",
        table_name="shopping_sense_checks",
    )
    op.drop_table("shopping_sense_checks")
