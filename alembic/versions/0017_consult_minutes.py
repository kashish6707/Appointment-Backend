"""Minutes per patient: a clinic default and a per-patient time, for expected times and alerts.

Also remembers the expected time a patient was last told, so a big delay can be sent once.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("doctor_clinics", sa.Column("minutes_per_patient", sa.Integer))
    op.create_check_constraint(
        "doctor_clinics_minutes_per_patient", "doctor_clinics", "minutes_per_patient IS NULL OR minutes_per_patient BETWEEN 1 AND 120"
    )
    op.add_column("bookings", sa.Column("consult_minutes", sa.Integer))
    op.create_check_constraint("bookings_consult_minutes", "bookings", "consult_minutes IS NULL OR consult_minutes BETWEEN 1 AND 240")
    op.add_column("bookings", sa.Column("expected_told_at", sa.DateTime(timezone=True)))
    op.add_column("bookings", sa.Column("delay_told_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("bookings", "delay_told_at")
    op.drop_column("bookings", "expected_told_at")
    op.drop_constraint("bookings_consult_minutes", "bookings")
    op.drop_column("bookings", "consult_minutes")
    op.drop_constraint("doctor_clinics_minutes_per_patient", "doctor_clinics")
    op.drop_column("doctor_clinics", "minutes_per_patient")
