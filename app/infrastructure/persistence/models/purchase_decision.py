"""V3-003 — purchase candidates and immutable wardrobe decision snapshots.

Two additive tables (V3_SPEC §11.2), gated for exposure by
`ENABLE_V3_PURCHASE_DECISION`. Rows persist regardless of the flag — the
`ShoppingSenseCheck` M17 convention — so flipping the flag back is a clean
rollback with no data loss.

**Durable truth, not signed URLs** (V3-003 acceptance 1, V3_SPEC §13.5): a
screenshot / order image is persisted as its *stable* storage object key —
the `object_key` that `POST /v1/uploads/presign` returns, which the frozen
contract §5.2 request body calls `source_asset_id`. There is no generic asset
table in this service, so the object key IS the asset reference. Signed render
URLs are TTL-bound and never persisted; read paths sign on demand through
`ObjectStoragePort.sign_render_url` (the `WardrobeItemImage` /
`ShoppingSenseCheck` precedent). External product links (`source_url`) are
durable by nature and are stored as-is.

**Why the decision table is append-only**: a wardrobe decision must keep
explaining itself the same way after the wardrobe it was computed against has
changed. So `wardrobe_decisions` carries no `updated_at`, the repository
exposes no update path, and the evidence references wardrobe items *inside*
JSONB rather than through foreign keys — deleting an item can therefore never
rewrite or cascade away a historical explanation. `RecommendationTrace`
(migration 0045) is the sibling precedent for the version + JSONB shape.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import (
    DecisionUncertainty,
    DecisionVisualNeed,
    PurchaseCandidateSource,
    PurchaseCandidateStatus,
    PurchaseDecision,
)
from app.infrastructure.persistence.models.base import (
    Base,
    JsonDict,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

# Current shape version of context_snapshot / evidence_snapshot. Bump when the
# snapshot payload changes so a replay can tell which reader to use, instead of
# migrating (and thereby rewriting) historical snapshots.
DECISION_SNAPSHOT_VERSION = 1


def _in_sql(enum_cls: type[StrEnum]) -> str:
    """Render an enum as a SQL `IN (...)` list so CHECKs can't drift from code."""
    return ", ".join(f"'{member.value}'" for member in enum_cls)


class PurchaseCandidate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Something the user is considering buying, plus whatever we parsed of it."""

    __tablename__ = "purchase_candidates"
    __table_args__ = (
        # Listing a user's candidates is most-recent-first (repo `list_for_user`).
        # Mirrors the index migration 0046 creates so create_all (tests) and
        # Alembic describe the same schema.
        Index(
            "ix_purchase_candidates_user_id_created_at",
            "user_id",
            "created_at",
        ),
        CheckConstraint(
            f"source_type IN ({_in_sql(PurchaseCandidateSource)})",
            name="source_type_enum",
        ),
        CheckConstraint(
            f"status IN ({_in_sql(PurchaseCandidateStatus)})",
            name="status_enum",
        ),
        # Contract §5.2: a candidate carries exactly one source reference —
        # a link (share_link / ask_link) or an uploaded object key
        # (screenshot / order_image), never both and never neither.
        CheckConstraint(
            "num_nonnulls(source_url, source_object_key) = 1",
            name="one_source_reference",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Durable external product link (share_link / ask_link).
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Durable internal storage key (screenshot / order_image) — NOT a signed URL.
    source_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PurchaseCandidateStatus.SUBMITTED.value)
    # Frozen minimal shape (contract §5.2), every field nullable:
    # {category, colors[], silhouette, material_hints[], price, scene_tags[]}.
    # NULL while status is `submitted`; a partial dict is the expected result of
    # a degraded parse — V3-004 owns filling it. Raw untrusted page text is not
    # stored here; only normalized attributes.
    parsed_item: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)


class WardrobeDecision(UUIDPrimaryKeyMixin, Base):
    """An immutable answer: what we decided, and the evidence as it stood then.

    Append-only by construction — see the module docstring. Note the absence of
    `TimestampMixin`: an `updated_at` column would imply a row can be revised.
    """

    __tablename__ = "wardrobe_decisions"
    __table_args__ = (
        Index(
            "ix_wardrobe_decisions_user_id_created_at",
            "user_id",
            "created_at",
        ),
        # One decision per candidate: contract §5.3 lets P0 always return the
        # first decision for a candidate, so repeated POSTs are idempotent at
        # the DB level and a race surfaces as 23505 (→ read the winner back)
        # rather than as two paid evaluations. NULL candidate_id (a decision
        # about an outfit rather than a purchase) is exempt — Postgres unique
        # indexes allow many NULLs. Relaxing this later (if a materially
        # changed wardrobe should be re-decidable) is a one-line reversible
        # migration.
        Index(
            "uq_wardrobe_decisions_candidate_id",
            "candidate_id",
            unique=True,
        ),
        CheckConstraint(
            f"decision IN ({_in_sql(PurchaseDecision)})",
            name="decision_enum",
        ),
        CheckConstraint(
            f"uncertainty IN ({_in_sql(DecisionUncertainty)})",
            name="uncertainty_enum",
        ),
        CheckConstraint(
            f"visual_need IN ({_in_sql(DecisionVisualNeed)})",
            name="visual_need_enum",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("purchase_candidates.id", ondelete="CASCADE"), nullable=True
    )
    # Deliberately NOT a foreign key: an outfit referenced by a historical
    # decision may later be deleted, and that must not cascade the explanation
    # away. Same reasoning as `outfit_visual_assets.outfit_id`.
    outfit_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False, default=DECISION_SNAPSHOT_VERSION)
    # What the decision was computed against: the concern asked, the candidate's
    # parsed attributes as of that moment, and the relevant wardrobe state.
    context_snapshot: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
    # What we told the user and why: reasons, closet comparison (item ids +
    # relation), next actions and the display copy (`decision_text`,
    # `boundary_note`) that contract §5.3 forbids the client to synthesize.
    # Item ids are references for re-signing images at read time, not a live join.
    evidence_snapshot: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    uncertainty: Mapped[str] = mapped_column(String(8), nullable=False)
    visual_need: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
