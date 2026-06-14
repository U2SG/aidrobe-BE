"""merge V4 parallel migration heads

Revision ID: 0031_merge_v4_parallel_migrations
Revises: 0027_add_outfit_plan, 0027_add_wear_history_user_worn_date_index,
         0028_add_packing_lists, 0029_add_ask_sessions, 0030_add_shopping_sense_check
Create Date: 2026-06-14

"""

from collections.abc import Sequence

revision: str = "0031_merge_v4_parallel_migrations"
down_revision: tuple[str, ...] = (
    "0027_add_outfit_plan",
    "0027_add_wear_history_user_worn_date_index",
    "0028_add_packing_lists",
    "0029_add_ask_sessions",
    "0030_add_shopping_sense_check",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
