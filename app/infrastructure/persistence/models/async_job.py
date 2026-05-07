import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.models.base import (
    Base,
    JsonDict,
    UUIDPrimaryKeyMixin,
)


class AsyncJob(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "async_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_async_jobs_idempotency_key"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','dead')",
            name="ck_async_jobs_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_async_jobs_attempts_nonneg"),
        CheckConstraint("max_attempts >= 1", name="ck_async_jobs_max_attempts_min"),
        CheckConstraint("cost_tokens >= 0", name="ck_async_jobs_cost_tokens_nonneg"),
        CheckConstraint("cost_cents >= 0", name="ck_async_jobs_cost_cents_nonneg"),
    )

    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    payload: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
