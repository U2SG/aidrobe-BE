"""add avatar profile, source asset and version persistence

Additive only (V3-011 acceptance 4): three new tables, no change to any
existing table, no data migration. All three live in a single revision so
`alembic downgrade -1` rolls the whole feature back in one step.

`avatar_profiles.active_version_id` → `avatar_versions.id` closes a cycle with
`avatar_versions.avatar_profile_id` → `avatar_profiles.id`, so that one FK is
added by ALTER after both tables exist (SQLAlchemy `use_alter=True` on the
model side) and is dropped first on the way down. Without that ordering the
downgrade cannot drop either table.

Revision ID: 0047_add_avatar_persistence
Revises: 0046_add_purchase_decision_persistence
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0047_add_avatar_persistence"
down_revision: str | None = "0046_add_purchase_decision_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_VERSION_FK = "fk_avatar_profiles_active_version_id_avatar_versions"


def upgrade() -> None:
    op.create_table(
        "avatar_source_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        # Stable storage key, never a signed URL (acceptance 2).
        sa.Column("source_object_key", sa.String(length=512), nullable=False),
        sa.Column("quality_status", sa.String(length=16), nullable=False),
        sa.Column("quality_signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("consent_status", sa.String(length=16), nullable=False),
        sa.Column("consent_version", sa.String(length=64), nullable=True),
        sa.Column("consent_decided_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_avatar_source_assets_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_avatar_source_assets")),
        # Prompt-once (V3_SPEC §7.3) made structural: the same photo cannot
        # produce a second candidate row for the same user.
        sa.UniqueConstraint("user_id", "source_object_key", name="one_row_per_source_object"),
        sa.CheckConstraint(
            "source_type IN ('ootd_photo', 'outfit_import', 'user_upload', 'camera_capture')",
            name=op.f("ck_avatar_source_assets_source_type_enum"),
        ),
        sa.CheckConstraint(
            "consent_status IN ('pending', 'accepted', 'declined')",
            name=op.f("ck_avatar_source_assets_consent_status_enum"),
        ),
        sa.CheckConstraint(
            "quality_status IN ('pending', 'passed', 'rejected')",
            name=op.f("ck_avatar_source_assets_quality_status_enum"),
        ),
        sa.CheckConstraint(
            "(consent_status = 'pending') = (consent_decided_at IS NULL)",
            name=op.f("ck_avatar_source_assets_decided_consent_has_timestamp"),
        ),
        sa.CheckConstraint(
            "(consent_version IS NULL) OR (consent_status = 'accepted')",
            name=op.f("ck_avatar_source_assets_consent_version_only_when_accepted"),
        ),
    )
    op.create_index(
        "ix_avatar_source_assets_user_id_created_at",
        "avatar_source_assets",
        ["user_id", "created_at"],
    )

    op.create_table(
        "avatar_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        # FK added below, after avatar_versions exists (cycle).
        sa.Column("active_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("consent_version", sa.String(length=64), nullable=True),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_avatar_profiles_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_avatar_profiles")),
        sa.UniqueConstraint("user_id", name="one_profile_per_user"),
        sa.CheckConstraint(
            "status IN ('none', 'candidate', 'preparing', 'ready', 'refresh_recommended', 'disabled')",
            name=op.f("ck_avatar_profiles_status_enum"),
        ),
        # The frozen GET shape, enforced by the database (contract §4.2).
        sa.CheckConstraint(
            "(status IN ('ready', 'refresh_recommended')) = (active_version_id IS NOT NULL)",
            name=op.f("ck_avatar_profiles_active_version_matches_status"),
        ),
        sa.CheckConstraint(
            "(status = 'disabled') = (disabled_at IS NOT NULL)",
            name=op.f("ck_avatar_profiles_disabled_status_has_timestamp"),
        ),
        sa.CheckConstraint(
            "(consent_version IS NULL) = (consented_at IS NULL)",
            name=op.f("ck_avatar_profiles_consent_fields_are_paired"),
        ),
    )

    op.create_table(
        "avatar_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("avatar_profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Provenance as JSONB, not FKs: a source photo is purged at retention
        # and that must not cascade away the record of what this was built from.
        sa.Column("source_asset_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_front_object_key", sa.String(length=512), nullable=False),
        sa.Column("canonical_side_object_key", sa.String(length=512), nullable=True),
        sa.Column("quality_status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # No updated_at: a version is written once and then only retired.
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["avatar_profile_id"],
            ["avatar_profiles.id"],
            name=op.f("fk_avatar_versions_avatar_profile_id_avatar_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_avatar_versions")),
        sa.CheckConstraint(
            "quality_status IN ('pending', 'passed', 'rejected')",
            name=op.f("ck_avatar_versions_quality_status_enum"),
        ),
    )
    op.create_index(
        "ix_avatar_versions_avatar_profile_id_created_at",
        "avatar_versions",
        ["avatar_profile_id", "created_at"],
    )

    # The cycle-closing FK. SET NULL rather than CASCADE: deleting a version
    # must never delete the profile. Combined with the
    # `active_version_matches_status` CHECK it also means deleting the version
    # a ready profile points at fails outright — the profile has to be
    # disabled or swapped first, so a ready avatar cannot silently disappear.
    op.create_foreign_key(
        op.f(ACTIVE_VERSION_FK),
        "avatar_profiles",
        "avatar_versions",
        ["active_version_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Break the cycle first, or neither table can be dropped.
    op.drop_constraint(op.f(ACTIVE_VERSION_FK), "avatar_profiles", type_="foreignkey")
    op.drop_index("ix_avatar_versions_avatar_profile_id_created_at", table_name="avatar_versions")
    op.drop_table("avatar_versions")
    op.drop_table("avatar_profiles")
    op.drop_index("ix_avatar_source_assets_user_id_created_at", table_name="avatar_source_assets")
    op.drop_table("avatar_source_assets")
