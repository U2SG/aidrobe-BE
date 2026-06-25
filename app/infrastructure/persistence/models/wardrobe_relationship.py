import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.models.base import Base, JsonDict

RELATIONSHIP_TYPES = (
    "pairs_well",
    "avoid_pairing",
    "user_approved_pairing",
    "user_rejected_pairing",
    "style_clash",
    "neutral",
)
RELATIONSHIP_SOURCES = ("rule_based", "user_feedback", "llm")
POSITIVE_RELATIONSHIP_TYPES = {"pairs_well", "user_approved_pairing"}
REJECTED_RELATIONSHIP_TYPES = {"avoid_pairing", "user_rejected_pairing"}


class WardrobeItemRelationship(Base):
    __tablename__ = "wardrobe_item_relationships"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    source_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wardrobe_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wardrobe_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_type: Mapped[str] = mapped_column(String(40), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_json: Mapped[JsonDict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("score BETWEEN -1.0 AND 1.0", name="score_range"),
        CheckConstraint("confidence BETWEEN 0.0 AND 1.0", name="confidence_range"),
        CheckConstraint(
            "relationship_type IN "
            "('pairs_well', 'avoid_pairing', 'user_approved_pairing', "
            "'user_rejected_pairing', 'style_clash', 'neutral')",
            name="relationship_type_enum",
        ),
        CheckConstraint(
            "source IN ('rule_based', 'user_feedback', 'llm')",
            name="source_enum",
        ),
        UniqueConstraint(
            "source_item_id",
            "target_item_id",
            "relationship_type",
            name="uq_wardrobe_item_relationships_pair_type",
        ),
        Index("ix_wardrobe_item_relationships_source_item_id", "source_item_id"),
        Index("ix_wardrobe_item_relationships_target_item_id", "target_item_id"),
    )
