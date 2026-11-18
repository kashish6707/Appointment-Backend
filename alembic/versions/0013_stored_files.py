"""Uploaded images kept in the database (STORAGE_BACKEND=db).

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stored_files",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("content_type", sa.Text, nullable=False),
        sa.Column("data", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("stored_files")
