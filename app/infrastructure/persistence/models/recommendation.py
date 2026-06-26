import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.enums import FeedbackType, RecommendationStatus
from app.infrastructure.persistence.models.base import (
    Base,
    JsonDict,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class DailyRecommendation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "daily_recommendations"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "recommendation_date",
            "revision",
            name="uq_daily_recommendations_user_date_rev",
        ),
        Index(
            "uq_daily_recommendations_current",
            "user_id",
            "recommendation_date",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recommendation_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=RecommendationStatus.READY.value, nullable=False, index=True
    )
    generation_source: Mapped[str] = mapped_column(String(32), nullable=False)
    score_breakdown: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    revision: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Phase D — context snapshot taken at generation time.
    # Shape: {"weather": {...}, "scene": "...", "fetched_at": "ISO-8601"}.
    # Nullable so legacy rows + weather-port-down generations remain valid.
    context_json: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)

    outfits: Mapped[list["Outfit"]] = relationship(
        back_populates="daily_recommendation", cascade="all, delete-orphan"
    )


class Outfit(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "outfits"

    daily_recommendation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("daily_recommendations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    outfit_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    reason_text: Mapped[str] = mapped_column(String(512), nullable=False)
    score_total: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    rank_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    time_sensitivity: Mapped[str] = mapped_column(String(32), default="standard", nullable=False)
    score_breakdown_json: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    score_rules_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    obstacles_json: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    prep_json: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    # BE#38 — grounded reasoning: a JSON array of {"type": ..., "text": ...}.
    reasoning_json: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    daily_recommendation: Mapped[DailyRecommendation] = relationship(back_populates="outfits")
    items: Mapped[list["OutfitItem"]] = relationship(
        back_populates="outfit", cascade="all, delete-orphan"
    )


class OutfitItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "outfit_items"

    outfit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outfits.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wardrobe_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    display_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    outfit: Mapped[Outfit] = relationship(back_populates="items")


class OutfitFeedback(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "outfit_feedback"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    outfit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outfits.id", ondelete="CASCADE"), nullable=False, index=True
    )
    feedback_type: Mapped[str] = mapped_column(
        String(32), default=FeedbackType.LIKE.value, nullable=False
    )
    reason_tags: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutfitFavorite(Base):
    __tablename__ = "outfit_favorites"
    # Composite PK (user_id, outfit_id) already enforces uniqueness — no extra
    # UniqueConstraint needed.

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    outfit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outfits.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutfitPlan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """V4 Outfit Calendar — a planned wear of an existing outfit on a date.

    A day can hold multiple plans (different outfits). `status` soft-cancels
    (planned → cancelled) rather than deleting, so the calendar can distinguish
    a never-planned slot from a cancelled one. Marking a plan worn delegates to
    WearOutfit (which lands WearHistory) and then cancels this row — the
    calendar's worn cells derive from WearHistory, planned cells from
    OutfitPlan(status=planned).
    """

    __tablename__ = "outfit_plan"
    __table_args__ = (
        Index("ix_outfit_plan_user_date", "user_id", "planned_date"),
        UniqueConstraint(
            "user_id",
            "outfit_id",
            "planned_date",
            name="uq_outfit_plan_user_outfit_date",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    outfit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outfits.id", ondelete="CASCADE"), nullable=False, index=True
    )
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="planned", server_default="planned"
    )


class RecommendationEvent(UUIDPrimaryKeyMixin, Base):
    """BE#40 — append-only interaction event log for recommendation learning.

    This is an EVENT STREAM, not current state: it complements (does not replace)
    `OutfitFeedback` (latest outfit rating), `FitFeedback` (item fit), and
    `WearHistory` (which days an outfit was worn). It accumulates the broader
    signal set (liked / disliked / saved / dismissed / worn / shared / …) with
    `recommendation_id` attribution so future ranking / preference learning has
    raw material — no ML now, just durable capture.

    Durability: `outfit_id` / `item_id` carry NO FK (loose coupling, like
    FitFeedback) so an event survives deletion of the outfit/item it referenced;
    `recommendation_id` is SET NULL on daily delete. `user_id` FK CASCADEs — this
    is behavioral data that should die with the user on PIPL purge (the opposite
    of `audit_log`, which must outlive it)."""

    __tablename__ = "recommendation_events"
    __table_args__ = (
        # Time-range queries by user (optionally filtered by type). Declared here
        # too (not only in the migration) because tests build the schema via
        # Base.metadata.create_all, bypassing Alembic.
        Index("ix_recommendation_events_user_created", "user_id", "created_at"),
        Index(
            "ix_recommendation_events_user_type_created",
            "user_id",
            "event_type",
            "created_at",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("daily_recommendations.id", ondelete="SET NULL"), nullable=True
    )
    # No FK on purpose — the event outlives the outfit/item it references.
    outfit_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    item_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    event_metadata: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WearHistory(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "wear_history"
    __table_args__ = (
        # Phase A — concurrent wear-record idempotency: same outfit can
        # only be recorded once per (user, day). Migration 0020 added this
        # at SQL level; declared here too so `Base.metadata.create_all`
        # in tests gets the constraint without going through migrations.
        UniqueConstraint(
            "user_id",
            "outfit_id",
            "worn_date",
            name="uq_wear_history_user_outfit_date",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    outfit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outfits.id", ondelete="CASCADE"), nullable=False, index=True
    )
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    worn_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ootd_image_url: Mapped[str | None] = mapped_column(nullable=True)
