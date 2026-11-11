"""notifications: an outbox of messages to patients (e.g. "OPD has started"), sent and retried
in the background

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-11
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("booking_id", UUID(as_uuid=True), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("opd_sessions.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("phone", sa.Text),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="PENDING"),
        sa.Column("channel", sa.Text),
        sa.Column("provider_id", sa.Text),
        sa.Column("attempts", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("booking_id", "kind", name="notifications_booking_kind"),
        sa.CheckConstraint("status IN ('PENDING', 'SENT', 'FAILED', 'SKIPPED')", name="notifications_status"),
    )
    op.create_index("ix_notifications_due", "notifications", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_due", "notifications")
    op.drop_table("notifications")
