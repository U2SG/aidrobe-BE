"""record the provider pipeline on generated visual assets

V3-016 acceptance 3: "Provider/model/prompt version are recorded on assets."
Three additive nullable columns on `outfit_visual_assets` (V3_SPEC §11.3 lists
exactly these three there), plus one conditional CHECK. Nothing is dropped,
renamed or backfilled, so `downgrade -1` reverses the whole feature.

**Nullable, and the CHECK is conditional, on purpose.** M10 rows
(`ootd_poster`, `outfit_board`) predate provenance; a blanket NOT NULL would
require inventing a `provider` for renders no provider produced. The constraint
therefore says only what the acceptance criterion claims — a `static_tryon` row
names its pipeline — and says nothing about the rest of the table.

**This constrains a write path V3-016 does not own, deliberately.** V3-016 adds
no router and writes no `static_tryon` row: V3-017 owns the enqueue that creates
one. The constraint is safe to impose from here because the pipeline identity is
a pure function of the adapter `container.static_tryon()` selects and is
therefore knowable *before* generation — contract §6.3 already requires it at
enqueue time, since `provider_model_version` and `prompt_version` are components
of the strong cache key. Stated plainly as a limit: the only exercise of this
constraint inside V3-016 is a hand-seeded row in
`tests/services/test_v3_static_tryon_provenance.py`, the same shape of declared
limit V3-003 recorded for its flag-gating criterion.

**Not added here, and no longer owed to anyone:** an `avatar_version_id` column
was reserved for V3-018 so that contract §4.2's `purge_derived` could reach
try-on rows. V3-017 made that parameter effective without one — it scopes by
`user_id` + `visual_type` (see `delete_static_tryon_for_user`) and keeps the
per-version linkage in `input_snapshot`, which avoids a `CASCADE`/`SET NULL`
ordering hazard against the version rows the same transaction deletes. A
cache-key column and its unique index are V3-017's (migration 0049).
`quality_status` / `quality_signals` are V3-018's. `created_at` / `expires_at`
already exist on the row and reaching the *response* is V3-019's.

Revision ID: 0048_add_visual_asset_provenance
Revises: 0047_add_avatar_persistence
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0048_add_visual_asset_provenance"
down_revision: str | None = "0047_add_avatar_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRYON_PROVENANCE_SQL = (
    "visual_type <> 'static_tryon' "
    "OR (provider IS NOT NULL AND model_version IS NOT NULL AND prompt_version IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column(
        "outfit_visual_assets", sa.Column("provider", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "outfit_visual_assets", sa.Column("model_version", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "outfit_visual_assets", sa.Column("prompt_version", sa.String(length=64), nullable=True)
    )
    op.create_check_constraint(
        "ck_outfit_visual_assets_tryon_records_provenance",
        "outfit_visual_assets",
        _TRYON_PROVENANCE_SQL,
    )


def downgrade() -> None:
    # The constraint goes first: it references all three columns, so dropping a
    # column out from under it would fail.
    op.drop_constraint(
        "ck_outfit_visual_assets_tryon_records_provenance",
        "outfit_visual_assets",
        type_="check",
    )
    op.drop_column("outfit_visual_assets", "prompt_version")
    op.drop_column("outfit_visual_assets", "model_version")
    op.drop_column("outfit_visual_assets", "provider")
