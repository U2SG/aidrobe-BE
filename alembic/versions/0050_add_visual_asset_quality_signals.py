"""record the quality gate's verdict signals on visual assets

V3-018: one additive nullable JSONB column on `outfit_visual_assets`. Nothing
is dropped, renamed or backfilled, so `downgrade -1` reverses the whole
feature's schema footprint.

**Why a column and not a log line.** The gate's verdict is what decides whether
a user ever sees a render, and this feature's human gate — "final visual
quality threshold requires benchmark review before production" — needs the
per-render record to review against. A structlog line expires with log
retention; the row is the asset it judged. `quality_status` from V3_SPEC
§11.3's suggestion is deliberately NOT added: `generation_status` already
carries ready / failed / rejected_by_quality_gate, and a second column
restating it is the derived-column smell (see the model comment).

**Why nullable.** Every M10 row (`ootd_poster`, `outfit_board`) predates the
gate, and a V3-017 row written before this feature was judged by nothing —
inventing a verdict onto them would be worse than leaving them NULL, the same
reasoning as 0048's conditional provenance CHECK.

**No CHECK constraint on the payload shape.** The shape is enforced where it
is constructed (`visual_quality_rules.validate_render_signals`, an allowlist
plus a deny-list) — a JSONB CHECK would restate a subset of that weaker, and
`avatar_source_assets.quality_signals` (0047) set the no-payload-CHECK
precedent for exactly this kind of column.

Revision ID: 0050_add_visual_asset_quality_signals
Revises: 0049_add_visual_asset_cache_key
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0050_add_visual_asset_quality_signals"
down_revision: str | None = "0049_add_visual_asset_cache_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "outfit_visual_assets"
COLUMN = "quality_signals"


def upgrade() -> None:
    op.add_column(
        TABLE, sa.Column(COLUMN, postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column(TABLE, COLUMN)
