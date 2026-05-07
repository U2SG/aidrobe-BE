import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.enums import ImageType, ItemStatus
from app.infrastructure.persistence.models.base import Base, JsonDict, TimestampMixin, UUIDPrimaryKeyMixin


class WardrobeItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "wardrobe_items"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    color_main: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=ItemStatus.WEARABLE.value, nullable=False
    )
    cover_image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    original_image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    size_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    size_region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fit_intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fit_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    comfort_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # V3 M14 extended fields
    sub_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    color_secondary: Mapped[str | None] = mapped_column(String(32), nullable=True)
    material: Mapped[str | None] = mapped_column(String(64), nullable=True)
    season_tags: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    style_tags: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    formality_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    time_cost_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # V3 M12 insights fields
    purchase_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    wear_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # A-1: last worn / purchase date
    last_worn_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    images: Mapped[list["WardrobeItemImage"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


class WardrobeItemImage(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "wardrobe_item_images"

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wardrobe_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    image_type: Mapped[str] = mapped_column(
        String(32), default=ImageType.ORIGINAL.value, nullable=False
    )
    image_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    item: Mapped[WardrobeItem] = relationship(back_populates="images")
