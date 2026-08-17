import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import VisualType
from app.infrastructure.persistence.models.base import Base, JsonDict, UUIDPrimaryKeyMixin

#: V3-016 — a `static_tryon` row must name the pipeline that produced it.
#:
#: Conditional rather than a blanket NOT NULL: the M10 rows already in this table
#: (`ootd_poster`, `outfit_composition`) were written before provenance existed
#: and backfilling an invented `provider` onto them would be worse than leaving
#: them null. So the constraint says what V3-016 acceptance 3 actually claims —
#: *try-on* assets record provider / model / prompt version — and says nothing
#: about anything else.
_TRYON_PROVENANCE_SQL = (
    f"visual_type <> '{VisualType.STATIC_TRYON.value}' "
    "OR (provider IS NOT NULL AND model_version IS NOT NULL AND prompt_version IS NOT NULL)"
)


class OutfitVisualAsset(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "outfit_visual_assets"
    __table_args__ = (CheckConstraint(_TRYON_PROVENANCE_SQL, name="tryon_records_provenance"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    outfit_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    visual_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase E5 — internal-storage object key for the rendered board.
    # Readers prefer this + sign on request when present.
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )
    input_snapshot: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    # V3-016 — which provider pipeline produced this asset (V3_SPEC §11.3).
    # Internal columns: contract §6.4 keeps `provider` / `model_version` /
    # `prompt_version` out of every response shape, and
    # `OutfitVisualAssetResponse` does not read them. They exist so a rendered
    # asset can be traced to a pipeline after the fact and so contract §6.3's
    # strong cache key has a persisted counterpart — the `avatar_versions`
    # precedent (V3-011), where the same triple is stored for the same reason.
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
