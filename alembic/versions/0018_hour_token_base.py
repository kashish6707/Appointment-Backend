"""Where a session's hour-based token numbers start.

When tokens per hour is switched on after patients have booked that day, the hours' numbers start
after the tokens already given out, so nobody new is numbered (and seen) before them.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("opd_sessions", sa.Column("hour_token_base", sa.Integer))


def downgrade() -> None:
    op.drop_column("opd_sessions", "hour_token_base")
