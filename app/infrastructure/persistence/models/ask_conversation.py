import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
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
