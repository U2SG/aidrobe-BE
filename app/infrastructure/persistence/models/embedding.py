import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import DDL, DateTime, ForeignKey, String, event, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.models.base import Base, UUIDPrimaryKeyMixin

try:
    from pgvector.sqlalchemy import Vector
except ImportError:
    from sqlalchemy.dialects.postgresql import JSONB

    Vector = None

EMBEDDING_DIM = 512


class ItemEmbedding(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "item_embeddings"

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wardrobe_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    vector: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIM) if Vector is not None else JSONB,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


if Vector is not None:
    event.listen(
        ItemEmbedding.__table__,
        "before_create",
        cast(Any, DDL)("CREATE EXTENSION IF NOT EXISTS vector"),
    )
