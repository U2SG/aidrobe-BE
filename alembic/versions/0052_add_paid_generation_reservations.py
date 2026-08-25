"""add the paid-generation reservation ledger

V3-026: one additive table. Nothing existing is dropped, renamed, altered or
backfilled — in particular `async_jobs` is untouched — so `alembic downgrade -1`
reverses the entire feature's schema footprint and no destructive-migration
human gate is needed.

No backfill, and that is a decision rather than an omission. The ledger starts
empty, so on the day this ships every user's budget reads as unspent. That is
correct: `ENABLE_V3_STATIC_TRYON` is off in committed configuration, so no paid
try-on has ever run in production, and there is nothing to carry forward.
Backfilling from historical `async_jobs` rows would invent settled reservations
for work whose outcome (quality-rejected vs technically failed) cannot be
reconstructed for rows that predate `quality_signals`.

The two CHECKs and the three indexes are declared identically here and in
`PaidGenerationReservation.__table_args__` — `tests/conftest.py` builds the test
schema with `create_all`, so a constraint declared only in this revision would
be absent in CI (and vice versa). `tests/unit/test_v3_paid_generation_admission
_sql.py` asserts the two descriptions cannot drift.

Revision ID: 0052_add_paid_generation_reservations
Revises: 0051_add_static_preview_feedback
Create Date: 2026-08-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0052_add_paid_generation_reservations"
down_revision: str | None = "0051_add_static_preview_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "paid_generation_reservations"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("usage_day", sa.Date(), nullable=False),
        sa.Column("reservation_key", sa.String(length=255), nullable=False),
        sa.Column("revive_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("attempt_grant", sa.Integer(), nullable=False),
        sa.Column("attempts_used", sa.Integer(), nullable=True),
        sa.Column("quality_regenerations", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_paid_generation_reservations_user_id_users"),
            ondelete="CASCADE",
        ),
        # SET NULL, not CASCADE: `purge_derived=true` must remove the likeness
        # and must NOT remove the record that the day's budget was spent, or a
        # user could reset their spend by deleting the renders it paid for.
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["async_jobs.id"],
            name=op.f("fk_paid_generation_reservations_job_id_async_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["outfit_visual_assets.id"],
            name=op.f("fk_paid_generation_reservations_target_id_outfit_visual_assets"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paid_generation_reservations")),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('succeeded', 'quality_rejected', 'technical_failure', 'abandoned')",
            name=op.f("ck_paid_generation_reservations_outcome_enum"),
        ),
        sa.CheckConstraint(
            "(outcome IS NULL) = (settled_at IS NULL)",
            name=op.f("ck_paid_generation_reservations_settled_pairing"),
        ),
        sa.CheckConstraint(
            "revive_index >= 0",
            name=op.f("ck_paid_generation_reservations_revive_index_non_negative"),
        ),
        sa.CheckConstraint(
            "attempt_grant >= 1",
            name=op.f("ck_paid_generation_reservations_attempt_grant_positive"),
        ),
    )
    # The admission read — the only query on the request path.
    op.create_index(
        "ix_paid_generation_reservations_bucket",
        TABLE,
        ["user_id", "job_type", "usage_day"],
    )
    # The revive-cap read and the settlement sweep's join key.
    op.create_index(
        "ix_paid_generation_reservations_target_id",
        TABLE,
        ["target_id"],
    )
    # One row per unit of purchase, and the unit is (what, which day): budgets
    # are daily, so the same key on a later day is a new purchase, not a replay.
    # A duplicate within one day means the admission lock's namespace is wrong,
    # so it must surface as 23505 rather than be absorbed.
    op.create_index(
        "uq_paid_generation_reservations_reservation_key",
        TABLE,
        ["reservation_key", "usage_day"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_paid_generation_reservations_reservation_key", table_name=TABLE)
    op.drop_index("ix_paid_generation_reservations_target_id", table_name=TABLE)
    op.drop_index("ix_paid_generation_reservations_bucket", table_name=TABLE)
    op.drop_table(TABLE)
