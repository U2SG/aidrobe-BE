"""BE#46 — persistent user preference memory.

A durable, user-correctable layer of long-term preferences (disliked fits,
preferred colors, avoided styles, occasion defaults, ...). Distinct from the
behavioral `app/application/recommendations/preferences.py` aggregate, which is
only a wear/feedback snapshot, not a remembered statement.

Safety: `preference_value` is a normalized, length-bounded string written only
through the controlled write service — never raw prompt text. A DB CHECK plus an
application-level clamp enforce the bound from both sides; `confidence` is pinned
to [0, 1] by CHECK. The partial unique index drives the merge semantics (one
live row per (user, type, value)); soft delete via `deleted_at` keeps the row
correctable later.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.models.base import Base, UUIDPrimaryKeyMixin

# Length bound on the normalized value. Mirrored by the DB CHECK below and by the
# application clamp in the write service so neither side alone is load-bearing.
PREFERENCE_VALUE_MAX_LEN = 120


class UserPreferenceMemory(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "user_preference_memory"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # style | color | fit | occasion | brand | material | formality | climate |
    # footwear | negative_preference — kept as a free string (not a DB enum) so
    # adding a type is a code change, not a migration.
    preference_type: Mapped[str] = mapped_column(String(40), nullable=False)
    preference_value: Mapped[str] = mapped_column(Text, nullable=False)
    polarity: Mapped[str] = mapped_column(String(8), nullable=False, default="neutral")
    confidence: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False, default=0.5)
    # explicit_user_statement | feedback_signal | inferred_from_history
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_range",
        ),
        CheckConstraint(
            "polarity IN ('positive', 'negative', 'neutral')",
            name="polarity_enum",
        ),
        CheckConstraint(
            f"char_length(preference_value) <= {PREFERENCE_VALUE_MAX_LEN}",
            name="value_length",
        ),
        # Merge key: one LIVE row per (user, type, value). Soft-deleted rows are
        # excluded so a correction can re-create a value the user previously
        # removed. ON CONFLICT in the repo targets this index.
        Index(
            "uq_user_preference_memory_live",
            "user_id",
            "preference_type",
            "preference_value",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
