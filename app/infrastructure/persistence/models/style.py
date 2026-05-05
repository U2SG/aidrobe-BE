import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.models.base import Base, JsonDict, UUIDPrimaryKeyMixin


class UserStyleProfile(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "user_style_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # inspiration_image | style_tag | wear_history | feedback
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # wardrobe_item_id or import_task_id that produced this signal
    source_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # CLIP embedding stored as JSON array until pgvector is enabled
    style_vector: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    # tags for style_tag source type; null otherwise
    style_tags: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    # image_url for inspiration_image source type
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    weight: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # async learning fields
    learn_status: Mapped[str] = mapped_column(
        String(32), default="learned", server_default="learned", nullable=False
    )
    learned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    learned_summary: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
