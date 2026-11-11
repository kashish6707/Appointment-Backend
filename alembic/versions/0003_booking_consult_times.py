"""bookings.called_at, bookings.completed_at

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("called_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "completed_at")
    op.drop_column("bookings", "called_at")
