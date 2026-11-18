"""opd_sessions.break_started_at / break_until: a break has a planned length, so waiting
patients' expected times move by it

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("opd_sessions", sa.Column("break_started_at", sa.DateTime(timezone=True)))
    op.add_column("opd_sessions", sa.Column("break_until", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("opd_sessions", "break_until")
    op.drop_column("opd_sessions", "break_started_at")
