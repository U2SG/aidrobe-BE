import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String
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
