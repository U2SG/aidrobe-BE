"""V3-024 — the durable answer to "did this render actually look like me".

One additive table (aidrobe-BE#154's suggested `StaticPreviewFeedback`
boundary). It is deliberately a **domain record and not an analytics event**:
`v3_preview_feedback` is a delivery/measurement signal that the FE owns and
that expires with the analytics pipeline's retention, whereas this row is
product-evaluation data that has to outlive it — it is what a provider or
prompt-version decision gets made against (contract §6.4, V3_SPEC §20).

**One row per asset, enforced by the database.**
`uq_static_preview_feedback_asset_id` is unique on `asset_id` alone rather than
on `(asset_id, user_id)`. Every asset has exactly one owner (`user_id` is NOT
NULL on `outfit_visual_assets` and feedback is only accepted from that owner),
so the two are equivalent in extension — but the narrower index makes
double-counting *structurally* impossible instead of contingent on the
ownership WHERE clause staying correct, which is the `uq_wardrobe_decisions_
candidate_id` precedent. `RecordPreviewFeedback` upserts onto it, so a repeated
tap is an idempotent no-op and a race is resolved by Postgres rather than by a
check-then-insert guard.

**`user_id` is carried anyway, and not as a denormalization.** It is the direct
`users` FK that `app.application.users.clear_data` scans for: that sweep is
metadata-driven over columns with an FK to `users.id`, so without this column
the table would only be reached indirectly (through the asset's own cascade)
and would silently drop out of clear-user-data if that path ever changed. With
it, feedback is cleared BY DEFAULT — the privacy-safe direction. The
`outfit_visual_assets` FK is `ON DELETE CASCADE` for the same reason from the
other side: `purge_derived=true` deletes the try-on rows in bulk Core SQL
(`delete_static_tryon_for_user`), which no ORM-level cascade would see, so the
database has to be the thing that removes the feedback with them.

**What is NOT here** (V3-024 acceptance 4, and it is a short list on purpose):
no image bytes, no `image_url`, no signed URL, no object key, no prompt text,
no provider secret, no body/identity inference and no free-text field. The
provenance an evaluation needs — `provider`, `model_version`, `prompt_version`
— is *joined* from `outfit_visual_assets` server-side
(`preview_feedback_metrics`), never copied here and never returned to a client
(contract §6.4). Copying it would also freeze a mislabelled render's provenance
into a second place, where a correction could not reach it.
"""

import uuid
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import PreviewFeedbackVerdict
from app.infrastructure.persistence.models.base import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


def _in_sql(enum_cls: type[StrEnum]) -> str:
    """Render an enum as a SQL `IN (...)` list so CHECKs can't drift from code."""
    return ", ".join(f"'{member.value}'" for member in enum_cls)


class StaticPreviewFeedback(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One user's verdict on one static try-on render."""

    __tablename__ = "static_preview_feedback"
    __table_args__ = (
        # The idempotency guarantee itself — see the module docstring.
        Index("uq_static_preview_feedback_asset_id", "asset_id", unique=True),
        CheckConstraint(
            f"verdict IN ({_in_sql(PreviewFeedbackVerdict)})",
            name="verdict_enum",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outfit_visual_assets.id", ondelete="CASCADE"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    # Optional, and NULL is meaningfully different from `[]`: the frozen §6.1
    # body makes `aspects` optional, and V3-020 omits the key entirely rather
    # than sending an empty list, so "the user did not elaborate" and "the user
    # chose nothing" stay distinguishable in the data.
    #
    # No CHECK on the payload: a JSONB constraint would restate a weaker subset
    # of the `PreviewFeedbackAspect` allowlist that the request schema already
    # enforces at the edge (422), the `avatar_source_assets.quality_signals`
    # (0047) and `outfit_visual_assets.quality_signals` (0050) precedent. What
    # DOES need to hold in the database is the verdict vocabulary above,
    # because rates are computed by grouping on it.
    aspects: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
