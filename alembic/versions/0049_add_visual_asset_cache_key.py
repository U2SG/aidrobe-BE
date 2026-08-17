"""key generated previews so identical inputs never regenerate

V3-017 acceptance 1: "Same avatar version and outfit inputs do not create
duplicate paid jobs." One additive nullable column on `outfit_visual_assets`
plus one unique index. Nothing is dropped, renamed or backfilled, so
`downgrade -1` reverses the whole feature.

**Why the uniqueness lives in the database rather than in the request path.**
`RequestStaticPreview` reads the cache before it enqueues, and that read races:
two requests can both find nothing and both insert. The unique index is what
makes the race resolve to one paid job — the loser catches `23505` and reads the
winner back (contract §6.3's "same key ⇒ same asset"). A pre-check guard in
Python would be the check-then-insert antipattern this repo has already rejected
once, in `decide_purchase`'s SAVEPOINT handling of
`uq_wardrobe_decisions_candidate_id`.

**Nullable and non-partial, deliberately.** Every M10 row (`ootd_poster`,
`outfit_board`) predates the cache key and has none; Postgres unique indexes
admit any number of NULLs, so a plain unique index already applies to exactly
the rows that carry a key, and a `WHERE cache_key IS NOT NULL` clause would add
a partial index whose only effect is to say the same thing less obviously. Same
choice, same reason, as `uq_wardrobe_decisions_candidate_id` (migration 0046).

`String(64)` is the exact width of a SHA-256 hex digest, so the column cannot
silently truncate a key into a collision with a different one.

**Not added here, and by whom:** `avatar_version_id` is V3-018's (V3-011's
`AvatarDeletionPlan` docstring assigned it, and
`test_purge_derived_try_on_scope_is_empty_by_construction` asserts its absence).
`quality_status` / `quality_signals` are V3-018's. `expires_at` reaching the
response is V3-019's.

Revision ID: 0049_add_visual_asset_cache_key
Revises: 0048_add_visual_asset_provenance
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0049_add_visual_asset_cache_key"
down_revision: str | None = "0048_add_visual_asset_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outfit_visual_assets", sa.Column("cache_key", sa.String(length=64), nullable=True)
    )
    op.create_index(
        "uq_outfit_visual_assets_cache_key",
        "outfit_visual_assets",
        ["cache_key"],
        unique=True,
    )


def downgrade() -> None:
    # The index goes first: it is defined over the column, so dropping the
    # column out from under it would fail.
    op.drop_index("uq_outfit_visual_assets_cache_key", table_name="outfit_visual_assets")
    op.drop_column("outfit_visual_assets", "cache_key")
