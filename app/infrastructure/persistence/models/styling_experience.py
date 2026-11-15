"""User-stated experiences, separate from ratings and inferred preferences."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.models.base import Base, UUIDPrimaryKeyMixin


class StylingExperience(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "styling_experiences"
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="uq_styling_experience_request"),
        Index("ix_styling_experiences_user_created", "user_id", "created_at"),
        Index("ix_styling_experiences_user_outfit", "user_id", "outfit_id"),
        Index(
            "uq_styling_experience_live_nonwear", "user_id", "outfit_id", "kind", unique=True,
            postgresql_where=text("deleted_at IS NULL AND kind <> 'wear_feedback'"),
        ),
        Index(
            "uq_styling_experience_live_wear", "user_id", "outfit_id", "worn_date", unique=True,
            postgresql_where=text("deleted_at IS NULL AND kind = 'wear_feedback'"),
        ),
        CheckConstraint(
            "kind IN ('appreciated', 'want_to_try', 'wear_feedback')", name="styling_experience_kind"
        ),
        CheckConstraint(
            "(kind = 'wear_feedback' AND worn_date IS NOT NULL AND wear_history_id IS NOT NULL "
            "AND feedback IS NOT NULL AND feedback IN ('like', 'neutral', 'dislike')) OR "
            "(kind <> 'wear_feedback' AND worn_date IS NULL AND wear_history_id IS NULL AND feedback IS NULL)",
            name="styling_experience_wear_evidence",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    outfit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("outfits.id", ondelete="CASCADE"), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ask_conversations.id", ondelete="SET NULL"), nullable=True
    )
    revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worn_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    wear_history_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wear_history.id", ondelete="CASCADE"), nullable=True
    )
    feedback: Mapped[str | None] = mapped_column(String(16), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    occasion: Mapped[str | None] = mapped_column(String(24), nullable=True)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("styling_experiences.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
