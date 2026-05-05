"""add import task source type column

Revision ID: 0008_import_task_source_types
Revises: 0007_wardrobe_fit_fields
Create Date: 2026-05-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_import_task_source_types"
down_revision: str | None = "0007_wardrobe_fit_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("import_tasks", "source_type"):
        op.add_column("import_tasks", sa.Column("source_type", sa.String(length=32), nullable=True))


def downgrade() -> None:
    # The current baseline migration already contains this column, so upgrade is
    # often a no-op. Do not drop a pre-existing column during downgrade.
    pass
