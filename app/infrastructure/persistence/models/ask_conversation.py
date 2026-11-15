import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.models.base import Base, JsonDict, UUIDPrimaryKeyMixin


class AskConversation(UUIDPrimaryKeyMixin, Base):
    """Short-lived Ask conversation thread for multi-turn outfit refinement."""

    __tablename__ = "ask_conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    styling_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AskMessage(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "ask_messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ask_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ask_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ask_sessions.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_metadata: Mapped[JsonDict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant')",
            name="role_enum",
        ),
    )


class AskConversationState(Base):
    __tablename__ = "ask_conversation_state"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ask_conversations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    last_outfit: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    active_constraints: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    rejected_item_ids: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    preferred_item_ids: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AskStylingRevision(UUIDPrimaryKeyMixin, Base):
    """Immutable design result; adopting a draft never changes its item set."""

    __tablename__ = "ask_styling_revisions"
    __table_args__ = (
        UniqueConstraint("conversation_id", "revision", name="uq_ask_styling_revision"),
        UniqueConstraint("user_id", "request_id", name="uq_ask_styling_request"),
        CheckConstraint("status IN ('draft', 'adopted')", name="styling_status"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ask_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    outfit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outfits.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    constraints: Mapped[JsonDict] = mapped_column(JSONB, nullable=False, default=dict)
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("daily_recommendations.id", ondelete="SET NULL"), nullable=True
    )
    recommendation_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommendation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
