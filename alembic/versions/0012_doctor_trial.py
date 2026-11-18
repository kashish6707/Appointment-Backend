"""Free trial end date for doctor accounts.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("doctors", sa.Column("trial_ends_at", sa.DateTime(timezone=True)))
    # Existing accounts get 90 days from when they registered.
    op.execute("UPDATE doctors SET trial_ends_at = created_at + interval '90 days'")


def downgrade() -> None:
    op.drop_column("doctors", "trial_ends_at")
