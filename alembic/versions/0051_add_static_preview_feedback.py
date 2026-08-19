"""add the static-preview fidelity feedback record

V3-024: one additive table. Nothing existing is dropped, renamed, altered or
backfilled, so `alembic downgrade -1` reverses the entire feature's schema
footprint — no human gate for a destructive migration is needed.

Both FKs are `ON DELETE CASCADE` and that is load-bearing rather than
decorative (V3-024 acceptance 5):

- `user_id → users.id` is what `app.application.users.clear_data`'s
  metadata-driven sweep matches on, so clear-user-data covers this table by
  default rather than by remembering to list it.
- `asset_id → outfit_visual_assets.id` is what removes feedback when
  `DELETE /v1/avatar-profile?purge_derived=true` deletes the try-on rows. That
  deletion is a bulk Core `DELETE` (`delete_static_tryon_for_user`), which no
  ORM-level cascade observes — the database has to be the mechanism.

The unique index is on `asset_id` alone: an asset has exactly one owner, so it
is equivalent to `(asset_id, user_id)` while making the "does not double-count"
guarantee structural. It also backs the `ON CONFLICT` target that
`RecordPreviewFeedback` upserts through, so it must exist in **both** this
revision and the model's `__table_args__` — `tests/conftest.py` builds the test
schema with `create_all`, and an index declared only here would make every
feedback request fail in CI with `InvalidColumnReference`.
`tests/services/test_v3_preview_feedback_migration.py` asserts the two
descriptions cannot drift.

Revision ID: 0051_add_static_preview_feedback
Revises: 0050_add_visual_asset_quality_signals
Create Date: 2026-08-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0051_add_static_preview_feedback"
down_revision: str | None = "0050_add_visual_asset_quality_signals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "static_preview_feedback"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("aspects", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            name=op.f("fk_static_preview_feedback_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["outfit_visual_assets.id"],
            name=op.f("fk_static_preview_feedback_asset_id_outfit_visual_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_static_preview_feedback")),
        sa.CheckConstraint(
            "verdict IN ('looks_like_me', 'not_like_me', 'garment_wrong')",
            name=op.f("ck_static_preview_feedback_verdict_enum"),
        ),
    )
    op.create_index(
        "ix_static_preview_feedback_user_id",
        TABLE,
        ["user_id"],
    )
    # One verdict per render — the idempotency guarantee, and the ON CONFLICT
    # target (see the module docstring).
    op.create_index(
        "uq_static_preview_feedback_asset_id",
        TABLE,
        ["asset_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_static_preview_feedback_asset_id", table_name=TABLE)
    op.drop_index("ix_static_preview_feedback_user_id", table_name=TABLE)
    op.drop_table(TABLE)
