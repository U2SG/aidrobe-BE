"""M17 — Shopping Sense-Check history model.

A sense-check evaluates a candidate purchase (image_url or source_url) against
the user's existing wardrobe and records the verdict. Rows persist regardless
of the `ENABLE_SENSE_CHECK` flag — the flag only gates exposure, so flipping it
back is a clean rollback without data loss.
"""

import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.models.base import (
    Base,
    JsonDict,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class ShoppingSenseCheck(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "shopping_sense_checks"
    __table_args__ = (
        # /history lists a user's checks most-recent-first. Matches the
        # composite index migration 0030 creates so create_all (tests) and
        # Alembic describe the same schema.
        Index(
            "ix_shopping_sense_checks_user_id_created_at",
            "user_id",
            "created_at",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Input reference — exactly one of these is set (image_url preferred when both).
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    # Full response snapshot: similar_items / wardrobe_gap / outfit_potential /
    # the identified candidate attributes — kept so /history can replay without
    # recomputing against a wardrobe that has since changed.
    payload: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
