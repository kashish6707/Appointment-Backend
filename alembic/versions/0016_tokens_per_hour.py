"""Tokens per hour: a clinic slot can cap bookings in each hour of OPD, and patients pick the hour.

Token numbers follow the hour (e.g. 10 an hour: 10-11 am gets 1-10, 11-12 gets 11-20), so a
cancelled token's number can be given out again: uniqueness now ignores cancelled bookings.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("doctor_clinics", sa.Column("tokens_per_hour", sa.Integer))
    op.create_check_constraint(
        "doctor_clinics_tokens_per_hour", "doctor_clinics", "tokens_per_hour IS NULL OR tokens_per_hour BETWEEN 1 AND 60"
    )
    op.add_column("bookings", sa.Column("slot_start", sa.Time))
    op.drop_constraint("bookings_session_token", "bookings", type_="unique")
    op.create_index(
        "bookings_session_token", "bookings", ["session_id", "token"], unique=True,
        postgresql_where=sa.text("status <> 'CANCELLED'"),
    )


def downgrade() -> None:
    op.drop_index("bookings_session_token", "bookings")
    # Cancelled bookings may now share a number with a later one: move them out of the way.
    op.execute(
        "UPDATE bookings b SET token = b.token + 100000 WHERE b.status = 'CANCELLED' AND EXISTS "
        "(SELECT 1 FROM bookings o WHERE o.session_id = b.session_id AND o.token = b.token AND o.id <> b.id)"
    )
    op.create_unique_constraint("bookings_session_token", "bookings", ["session_id", "token"])
    op.drop_column("bookings", "slot_start")
    op.drop_constraint("doctor_clinics_tokens_per_hour", "doctor_clinics")
    op.drop_column("doctor_clinics", "tokens_per_hour")
