"""recommendation interaction events (BE#40)

Adds `recommendation_events` — an append-only interaction signal log for
recommendation learning (liked / disliked / saved / dismissed / worn / …),
with `recommendation_id` attribution. Complements (does not replace) the
current-state feedback tables. No feature flag: capture accumulates always.

`outfit_id` / `item_id` carry no FK on purpose (events outlive the rows they
reference); `recommendation_id` is SET NULL on daily delete; `user_id` CASCADEs
(behavioral data dies with the user on PIPL purge).

Revision ID: 0034_recommendation_events
Revises: 0033_locale_aware_naming
Create Date: 2026-06-24

NOTE: a sibling Alembic head off 0033 may land from the Today-reasoning work
(BE#37/#38). When both merge, run `alembic merge heads` (or rebase the later
PR's down_revision onto this one).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0034_recommendation_events"
down_revision: str | None = "0033_locale_aware_naming"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recommendation_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recommendation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("outfit_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=True),
        sa.Column("event_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_recommendation_events_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recommendation_id"],
            ["daily_recommendations.id"],
            name=op.f("fk_recommendation_events_recommendation_id_daily_recommendations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_events")),
    )
    op.create_index(
        "ix_recommendation_events_user_created",
        "recommendation_events",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_recommendation_events_user_type_created",
        "recommendation_events",
        ["user_id", "event_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_recommendation_events_user_type_created", table_name="recommendation_events")
    op.drop_index("ix_recommendation_events_user_created", table_name="recommendation_events")
    op.drop_table("recommendation_events")
