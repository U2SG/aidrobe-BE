"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_table(
        "user_settings",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("departure_time", sa.Time(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_settings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_settings")),
        sa.UniqueConstraint("user_id", name=op.f("uq_user_settings_user_id")),
    )
    op.create_index(op.f("ix_user_settings_user_id"), "user_settings", ["user_id"], unique=False)
    op.create_table(
        "wardrobe_items",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("color_main", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cover_image_url", sa.String(length=1024), nullable=True),
        sa.Column("original_image_url", sa.String(length=1024), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_wardrobe_items_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wardrobe_items")),
    )
    op.create_index(
        op.f("ix_wardrobe_items_category"), "wardrobe_items", ["category"], unique=False
    )
    op.create_index(op.f("ix_wardrobe_items_user_id"), "wardrobe_items", ["user_id"], unique=False)
    op.create_table(
        "import_tasks",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("input_type", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("task_status", sa.String(length=32), nullable=False),
        sa.Column("input_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("candidate_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_import_tasks_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_tasks")),
    )
    op.create_index(
        op.f("ix_import_tasks_task_status"), "import_tasks", ["task_status"], unique=False
    )
    op.create_index(op.f("ix_import_tasks_user_id"), "import_tasks", ["user_id"], unique=False)
    op.create_table(
        "daily_recommendations",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recommendation_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("generation_source", sa.String(length=32), nullable=False),
        sa.Column("score_breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_daily_recommendations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_recommendations")),
        sa.UniqueConstraint(
            "user_id", "recommendation_date", name=op.f("uq_daily_recommendations_user_id")
        ),
    )
    op.create_index(
        op.f("ix_daily_recommendations_status"), "daily_recommendations", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_daily_recommendations_user_id"), "daily_recommendations", ["user_id"], unique=False
    )
    op.create_table(
        "wardrobe_item_images",
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("image_type", sa.String(length=32), nullable=False),
        sa.Column("image_url", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["wardrobe_items.id"],
            name=op.f("fk_wardrobe_item_images_item_id_wardrobe_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wardrobe_item_images")),
    )
    op.create_index(
        op.f("ix_wardrobe_item_images_item_id"), "wardrobe_item_images", ["item_id"], unique=False
    )
    op.create_table(
        "outfits",
        sa.Column("daily_recommendation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outfit_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("reason_text", sa.String(length=512), nullable=False),
        sa.Column("score_total", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column("rank_order", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["daily_recommendation_id"],
            ["daily_recommendations.id"],
            name=op.f("fk_outfits_daily_recommendation_id_daily_recommendations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_outfits_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outfits")),
    )
    op.create_index(
        op.f("ix_outfits_daily_recommendation_id"),
        "outfits",
        ["daily_recommendation_id"],
        unique=False,
    )
    op.create_index(op.f("ix_outfits_user_id"), "outfits", ["user_id"], unique=False)
    op.create_table(
        "outfit_feedback",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outfit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("feedback_type", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["outfit_id"],
            ["outfits.id"],
            name=op.f("fk_outfit_feedback_outfit_id_outfits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_outfit_feedback_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outfit_feedback")),
    )
    op.create_index(
        op.f("ix_outfit_feedback_outfit_id"), "outfit_feedback", ["outfit_id"], unique=False
    )
    op.create_index(
        op.f("ix_outfit_feedback_user_id"), "outfit_feedback", ["user_id"], unique=False
    )
    op.create_table(
        "outfit_items",
        sa.Column("outfit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("display_order", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["wardrobe_items.id"],
            name=op.f("fk_outfit_items_item_id_wardrobe_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["outfit_id"],
            ["outfits.id"],
            name=op.f("fk_outfit_items_outfit_id_outfits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outfit_items")),
    )
    op.create_index(op.f("ix_outfit_items_item_id"), "outfit_items", ["item_id"], unique=False)
    op.create_index(op.f("ix_outfit_items_outfit_id"), "outfit_items", ["outfit_id"], unique=False)
    op.create_table(
        "wear_history",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outfit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("worn_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["outfit_id"],
            ["outfits.id"],
            name=op.f("fk_wear_history_outfit_id_outfits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_wear_history_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wear_history")),
    )
    op.create_index(op.f("ix_wear_history_outfit_id"), "wear_history", ["outfit_id"], unique=False)
    op.create_index(op.f("ix_wear_history_user_id"), "wear_history", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_wear_history_user_id"), table_name="wear_history")
    op.drop_index(op.f("ix_wear_history_outfit_id"), table_name="wear_history")
    op.drop_table("wear_history")
    op.drop_index(op.f("ix_outfit_items_outfit_id"), table_name="outfit_items")
    op.drop_index(op.f("ix_outfit_items_item_id"), table_name="outfit_items")
    op.drop_table("outfit_items")
    op.drop_index(op.f("ix_outfit_feedback_user_id"), table_name="outfit_feedback")
    op.drop_index(op.f("ix_outfit_feedback_outfit_id"), table_name="outfit_feedback")
    op.drop_table("outfit_feedback")
    op.drop_index(op.f("ix_outfits_user_id"), table_name="outfits")
    op.drop_index(op.f("ix_outfits_daily_recommendation_id"), table_name="outfits")
    op.drop_table("outfits")
    op.drop_index(op.f("ix_wardrobe_item_images_item_id"), table_name="wardrobe_item_images")
    op.drop_table("wardrobe_item_images")
    op.drop_index(op.f("ix_daily_recommendations_user_id"), table_name="daily_recommendations")
    op.drop_index(op.f("ix_daily_recommendations_status"), table_name="daily_recommendations")
    op.drop_table("daily_recommendations")
    op.drop_index(op.f("ix_import_tasks_user_id"), table_name="import_tasks")
    op.drop_index(op.f("ix_import_tasks_task_status"), table_name="import_tasks")
    op.drop_table("import_tasks")
    op.drop_index(op.f("ix_wardrobe_items_user_id"), table_name="wardrobe_items")
    op.drop_index(op.f("ix_wardrobe_items_category"), table_name="wardrobe_items")
    op.drop_table("wardrobe_items")
    op.drop_index(op.f("ix_user_settings_user_id"), table_name="user_settings")
    op.drop_table("user_settings")
    op.drop_table("users")
