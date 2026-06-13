import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, SmallInteger, String
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

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    destination: Mapped[str | None] = mapped_column(String(120), nullable=True)
    days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    scenes: Mapped[JsonDict | list[str] | None] = mapped_column(JSONB, nullable=True)
    departure_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # [{item_id, day, scene, notes}]
    items: Mapped[JsonDict | list[JsonDict] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="draft", server_default="draft"
    )
