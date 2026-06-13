import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.models.base import (
    Base,
    JsonDict,
    UUIDPrimaryKeyMixin,
)


class AskSession(UUIDPrimaryKeyMixin, Base):
    """M15 Ask Aidrobe — one row per Q&A turn. FK cascades on user purge
    (PIPL); `item_refs` lists referenced wardrobe item ids, `tokens_used`
    mirrors the cost recorded on async_jobs for the feature record."""

    __tablename__ = "ask_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_refs: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    context: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
