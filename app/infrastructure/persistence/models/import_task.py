import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import ImportStatus
from app.infrastructure.persistence.models.base import (
    Base,
    JsonDict,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class ImportTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "import_tasks"
    __table_args__ = (
        Index("ix_import_tasks_user_conversation", "user_id", "conversation_id", "created_at"),
        Index("ix_import_tasks_user_batch", "user_id", "batch_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    task_status: Mapped[str] = mapped_column(
        String(32), default=ImportStatus.QUEUED.value, nullable=False, index=True
    )
    input_payload: Mapped[JsonDict] = mapped_column(JSONB, nullable=False, default=dict)
    candidate_payload: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    run_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Set when the photo was attached in an Ask conversation; the conversation
    # read returns these tasks so the chat can show the import cards again.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ask_conversations.id", ondelete="SET NULL"), nullable=True
    )
    # Album scan: the client-chosen batch this photo belongs to, and its capture date.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    taken_at: Mapped[date | None] = mapped_column(Date, nullable=True)


class ImportBatchVerdict(UUIDPrimaryKeyMixin, Base):
    """Cached same-garment verdict for two images compared while grouping a batch."""

    __tablename__ = "import_batch_verdicts"
    __table_args__ = (UniqueConstraint("user_id", "batch_id", "image_a", "image_b"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # Image refs (object key, else URL) in sorted order.
    image_a: Mapped[str] = mapped_column(Text, nullable=False)
    image_b: Mapped[str] = mapped_column(Text, nullable=False)
    same: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
