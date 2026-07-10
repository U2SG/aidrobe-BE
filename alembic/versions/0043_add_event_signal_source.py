"""widen recommendation_events.event_type and add signal_strength / source_surface

Revision ID: 0043_add_event_signal_source
Revises: 0042_add_commute_exposure
Create Date: 2026-07-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0043_add_event_signal_source"
down_revision: str | None = "0042_add_commute_exposure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # BE#81 — passive-learning event types are longer than the legacy 16 chars
    # (e.g. "temperature_mismatch_reported" = 29), and events now carry an
    # intensity + originating UI surface.
    op.alter_column(
        "recommendation_events",
        "event_type",
        type_=sa.String(48),
        existing_type=sa.String(16),
        existing_nullable=False,
    )
    op.add_column(
        "recommendation_events",
        sa.Column("signal_strength", sa.String(8), nullable=True),
    )
    op.add_column(
        "recommendation_events",
        sa.Column("source_surface", sa.String(16), nullable=True),
    )
    op.create_check_constraint(
        "ck_recommendation_events_signal_strength",
        "recommendation_events",
        "signal_strength IS NULL OR signal_strength IN ('weak', 'medium', 'strong')",
    )
    op.create_check_constraint(
        "ck_recommendation_events_source_surface",
        "recommendation_events",
        "source_surface IS NULL OR source_surface IN ('today', 'ask', 'wardrobe', 'profile')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_recommendation_events_source_surface", "recommendation_events", type_="check"
    )
    op.drop_constraint(
        "ck_recommendation_events_signal_strength", "recommendation_events", type_="check"
    )
    op.drop_column("recommendation_events", "source_surface")
    op.drop_column("recommendation_events", "signal_strength")
    op.alter_column(
        "recommendation_events",
        "event_type",
        type_=sa.String(16),
        existing_type=sa.String(48),
        existing_nullable=False,
    )
