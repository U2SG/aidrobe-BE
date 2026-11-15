"""Persist travel's source design and idempotent creation request.

Revision ID: 0058_design_continuity
Revises: 0057_styling_experiences
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0058_design_continuity"
down_revision = "0057_styling_experiences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("packing_lists", sa.Column("design_context", postgresql.JSONB(), nullable=True))
    op.add_column("packing_lists", sa.Column("design_request_id", sa.Uuid(), nullable=True))
    op.add_column("packing_lists", sa.Column("design_request_fingerprint", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_packing_design_request", "packing_lists", ["user_id", "design_request_id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM packing_lists WHERE design_context IS NOT NULL)")):
        raise RuntimeError("Cannot downgrade while travel lists retain design context")
    op.drop_constraint("uq_packing_design_request", "packing_lists", type_="unique")
    op.drop_column("packing_lists", "design_request_fingerprint")
    op.drop_column("packing_lists", "design_request_id")
    op.drop_column("packing_lists", "design_context")
