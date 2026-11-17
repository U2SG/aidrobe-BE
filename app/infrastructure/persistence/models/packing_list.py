import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Index, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.models.base import (
    Base,
    JsonDict,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class PackingList(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """M16 Travel Packing List — rule-based selection from the real wardrobe.

    A packing list is generated for a trip (destination + days + scenes) by
    picking wardrobe items per day/scene (no LLM). `items` holds the result as
    a JSONB array of {item_id, day, scene, notes}. `status` is draft until the
    user confirms. The list persists regardless of the feature flag; the flag
    only gates endpoint exposure.
    """

    __tablename__ = "packing_lists"
    __table_args__ = (Index("ix_packing_lists_user_conversation", "user_id", "conversation_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ask_conversations.id", ondelete="SET NULL"), nullable=True
    )
    styling_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ask_styling_revisions.id", ondelete="SET NULL"), nullable=True
    )
    destination: Mapped[str | None] = mapped_column(String(120), nullable=True)
    days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    scenes: Mapped[JsonDict | list[str]] = mapped_column(JSONB, nullable=False)
    departure_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # [{item_id, day, scene, notes}]
    items: Mapped[JsonDict | list[JsonDict] | None] = mapped_column(JSONB, nullable=True)
    # BE#68 — rich planner snapshot {summary, weather_summary, packing_items,
    # daily_outfits, missing_items}. NULL for legacy / planner-off lists.
    plan_json: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="draft", server_default="draft"
    )
