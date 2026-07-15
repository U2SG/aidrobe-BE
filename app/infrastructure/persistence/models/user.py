import uuid
from datetime import datetime, time

from sqlalchemy import DateTime, ForeignKey, String, Time, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.persistence.models.base import (
    Base,
    JsonDict,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class User(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "users"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    apple_subject: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Phase E1 — PIPL soft-delete state machine. Both NULL = active.
    # `deactivated_at` only → in grace; auth refuses login.
    # `purged_at` not NULL → personal data wiped; row preserved as a stub
    # so dangling foreign-key references (audit_log.user_id) stay readable.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settings: Mapped["UserSettings | None"] = relationship(back_populates="user", cascade="all, delete-orphan")
    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    departure_time: Mapped[time | None] = mapped_column(Time(timezone=False), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    push_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # BE#30 — UI locale for AI-generated clothing names / Ask answers.
    # One of 'zh-CN' | 'en-US' | 'system'. 'system' (default) defers to the
    # product default (zh-CN) at resolution time; see app/common/locale.py.
    locale: Mapped[str] = mapped_column(String(16), default="system", server_default="system", nullable=False)
    preferences_json: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    # 'more_outdoor' | 'less_outdoor' — commute exposure for indoor-climate inference.
    # Added via migration 0042; NULL means not yet answered.
    commute_exposure: Mapped[str | None] = mapped_column(String(16), nullable=True)

    user: Mapped[User] = relationship(back_populates="settings")


class UserSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")
