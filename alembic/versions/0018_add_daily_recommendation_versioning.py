"""add daily recommendation versioning

Revision ID: 0018_add_daily_recommendation_versioning
Revises: 0017_wardrobe_insights_fields, 0009_image_processing_jobs
Create Date: 2026-05-06
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401  (kept for autogenerate consistency)

from alembic import op

revision: str = "0018_add_daily_recommendation_versioning"
down_revision: str | Sequence[str] | None = (
    "0017_wardrobe_insights_fields",
    "0009_image_processing_jobs",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add new columns. Constant server_default → PG 11+ fast-path
    #    (catalog-only ACCESS EXCLUSIVE, no table rewrite).
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        "daily_recommendations",
        sa.Column("revision", sa.SmallInteger(), nullable=False, server_default="1"),
    )
    op.add_column(
        "daily_recommendations",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default="true"),
    )

    # 2. Build BOTH new indexes CONCURRENTLY while the old uniqueness is still in
    #    place. This closes the window where concurrent writes could insert two
    #    is_current=true rows for the same (user, date).
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
                uq_daily_recommendations_user_date_rev
            ON daily_recommendations (user_id, recommendation_date, revision)
            """
        )
        op.execute(
            """
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
                uq_daily_recommendations_current
            ON daily_recommendations (user_id, recommendation_date)
            WHERE is_current
            """
        )

    # 3. Promote the (user, date, revision) index into a real UNIQUE constraint
    #    via USING INDEX (no second full scan, brief lock only). Drop the old
    #    constraint AFTER the new partial index is live, so there is never a
    #    moment without uniqueness coverage on (user, date) for current rows.
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        """
        ALTER TABLE daily_recommendations
        ADD CONSTRAINT uq_daily_recommendations_user_date_rev
        UNIQUE USING INDEX uq_daily_recommendations_user_date_rev
        """
    )
    op.drop_constraint(
        "uq_daily_recommendations_user_id",
        "daily_recommendations",
        type_="unique",
    )


def downgrade() -> None:
    # 1. Build the legacy unique index CONCURRENTLY while new uniqueness is still
    #    enforced (partial index is sufficient — every row has is_current=true
    #    after a clean upgrade, but defensive: tolerate dups via IF NOT EXISTS).
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
                uq_daily_recommendations_user_id
            ON daily_recommendations (user_id, recommendation_date)
            """
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_daily_recommendations_current")

    # 2. Promote the legacy index into a constraint, drop the new one, drop
    #    the new columns.
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        """
        ALTER TABLE daily_recommendations
        ADD CONSTRAINT uq_daily_recommendations_user_id
        UNIQUE USING INDEX uq_daily_recommendations_user_id
        """
    )
    op.drop_constraint(
        "uq_daily_recommendations_user_date_rev",
        "daily_recommendations",
        type_="unique",
    )
    op.drop_column("daily_recommendations", "is_current")
    op.drop_column("daily_recommendations", "revision")
