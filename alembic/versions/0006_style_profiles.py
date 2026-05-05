"""add user_style_profiles table

Revision ID: 0006_style_profiles
Revises: 0005_phase4_phase5
Create Date: 2026-05-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_style_profiles"
down_revision: str | None = "0005_phase4_phase5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_style_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("style_vector", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("style_tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("image_url", sa.String(length=1024), nullable=True),
        sa.Column("weight", sa.Numeric(precision=4, scale=2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_style_profiles_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_style_profiles")),
    )
    op.create_index(
        op.f("ix_user_style_profiles_user_id"), "user_style_profiles", ["user_id"]
    )
    op.create_index(
        "ix_user_style_profiles_user_source",
        "user_style_profiles",
        ["user_id", "source_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_style_profiles_user_source", table_name="user_style_profiles")
    op.drop_index(op.f("ix_user_style_profiles_user_id"), table_name="user_style_profiles")
    op.drop_table("user_style_profiles")
