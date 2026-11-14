import os
import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import DDL, ColumnElement, DateTime, ForeignKey, Index, String, and_, event, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.models.base import Base, UUIDPrimaryKeyMixin
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

try:
    from pgvector.sqlalchemy import Vector as _PgVector

    _PGVECTOR_INSTALLED = True
except ImportError:
    _PgVector = None
    _PGVECTOR_INSTALLED = False

EMBEDDING_DIM = 512

# Use pgvector type only when explicitly enabled (production).
# Fall back to JSONB in test/dev environments where the extension is not installed.
_USE_VECTOR = _PGVECTOR_INSTALLED and os.getenv("PGVECTOR_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)

_VectorColumn = _PgVector(EMBEDDING_DIM) if _USE_VECTOR else JSONB()


class ItemEmbedding(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "item_embeddings"
    __table_args__ = (Index("uq_item_embeddings_item_model", "item_id", "model_name", unique=True),)

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wardrobe_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vector: Mapped[Any] = mapped_column(_VectorColumn, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def is_current_embedding(model_version: str) -> ColumnElement[bool]:
    """Predicate for queries that join a wardrobe item to its vectors."""
    return and_(
        ItemEmbedding.model_name == model_version,
        ItemEmbedding.source_version == WardrobeItem.embedding_source_version,
    )


def pgvector_enabled() -> bool:
    return _USE_VECTOR


def _maybe_create_vector_extension(*args: Any, **kw: Any) -> None:
    if not _USE_VECTOR:
        return
    conn = next((a for a in args if hasattr(a, "execute")), None)
    if conn is None:
        return
    try:
        conn.execute(cast(Any, DDL("SAVEPOINT _vec_ext")))  # type: ignore[no-untyped-call]
        conn.execute(cast(Any, DDL("CREATE EXTENSION IF NOT EXISTS vector")))  # type: ignore[no-untyped-call]
        conn.execute(cast(Any, DDL("RELEASE SAVEPOINT _vec_ext")))  # type: ignore[no-untyped-call]
    except Exception:
        try:
            conn.execute(cast(Any, DDL("ROLLBACK TO SAVEPOINT _vec_ext")))  # type: ignore[no-untyped-call]
        except Exception:
            pass


if _USE_VECTOR:
    event.listen(
        ItemEmbedding.__table__,
        "before_create",
        _maybe_create_vector_extension,
    )
