"""WhatsApp updates: per-booking opt-in and language, and template messages in the outbox
(several breaks a day each get their own message, so uniqueness moves to a dedupe key).

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("whatsapp_opt_in", sa.Boolean, nullable=False, server_default="false"))
    op.add_column("bookings", sa.Column("lang", sa.Text, nullable=False, server_default="en"))
    op.create_check_constraint("bookings_lang", "bookings", "lang IN ('en', 'hi')")
    op.add_column("notifications", sa.Column("template", sa.Text))
    op.add_column("notifications", sa.Column("params", JSONB, nullable=False, server_default="[]"))
    op.add_column("notifications", sa.Column("lang", sa.Text, nullable=False, server_default="en"))
    op.add_column("notifications", sa.Column("dedupe_key", sa.Text))
    op.execute("UPDATE notifications SET dedupe_key = kind")
    op.alter_column("notifications", "dedupe_key", nullable=False)
    op.drop_constraint("notifications_booking_kind", "notifications", type_="unique")
    op.create_unique_constraint("notifications_booking_dedupe", "notifications", ["booking_id", "dedupe_key"])


def downgrade() -> None:
    op.drop_constraint("notifications_booking_dedupe", "notifications", type_="unique")
    op.execute("DELETE FROM notifications a USING notifications b WHERE a.booking_id = b.booking_id AND a.kind = b.kind AND a.created_at > b.created_at")
    op.create_unique_constraint("notifications_booking_kind", "notifications", ["booking_id", "kind"])
    for c in ("dedupe_key", "lang", "params", "template"):
        op.drop_column("notifications", c)
    op.drop_constraint("bookings_lang", "bookings")
    op.drop_column("bookings", "lang")
    op.drop_column("bookings", "whatsapp_opt_in")
