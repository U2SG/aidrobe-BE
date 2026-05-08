"""add object_key column to internal-storage image tables

Revision ID: 0026_add_object_key_columns
Revises: 0025_user_soft_delete_and_audit_log
Create Date: 2026-05-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_add_object_key_columns"
down_revision: str | None = "0025_user_soft_delete_and_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Phase E5 (no-CDN path) — readers must sign each render URL when the
    bucket is private. Storing a stable object key rather than a baked-in
    URL keeps the persisted reference valid across:
    - signature TTL expiry (URL would 403 hours after store)
    - bucket / CDN domain renames
    - future storage provider migrations

    `image_url` stays for cases where the value is genuinely an external
    URL we don't own (import flow saves the user's pasted source URL).
    Newly-generated internal artifacts (cutouts, thumbnails, board
    composites) populate `object_key` instead. Read code prefers
    `object_key` and signs at request time."""
    op.execute("SET lock_timeout = '3s'")

    op.add_column(
        "wardrobe_item_images",
        sa.Column("object_key", sa.String(length=512), nullable=True),
    )
    # image_url stays NOT NULL for legacy rows. For new rows produced from
    # the storage pipeline, callers can leave image_url as "" and rely on
    # object_key. Tightening to NULL would break the import-flow callers.

    op.add_column(
        "outfit_visual_assets",
        sa.Column("object_key", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_column("outfit_visual_assets", "object_key")
    op.drop_column("wardrobe_item_images", "object_key")
