"""weekly_hours (per-weekday OPD hours and daily patient limit) and date_exceptions (days off,
different hours on a date)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-11
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

WEEKDAYS = "'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'"


def upgrade() -> None:
    op.create_table(
        "weekly_hours",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("doctor_clinic_id", UUID(as_uuid=True), sa.ForeignKey("doctor_clinics.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("weekday", sa.Text, nullable=False),
        sa.Column("start", sa.Time, nullable=False),
        sa.Column("end", sa.Time, nullable=False),
        sa.Column("patient_limit", sa.SmallInteger),
        sa.UniqueConstraint("doctor_clinic_id", "weekday", name="weekly_hours_slot_weekday"),
        sa.CheckConstraint(f"weekday IN ({WEEKDAYS})", name="weekly_hours_weekday"),
        sa.CheckConstraint('start < "end"', name="weekly_hours_hours"),
        sa.CheckConstraint("patient_limit IS NULL OR patient_limit BETWEEN 1 AND 500", name="weekly_hours_limit"),
    )
    op.create_table(
        "date_exceptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("doctor_clinic_id", UUID(as_uuid=True), sa.ForeignKey("doctor_clinics.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("start", sa.Time),
        sa.Column("end", sa.Time),
        sa.Column("patient_limit", sa.SmallInteger),
        sa.Column("note", sa.Text),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("doctor_clinic_id", "date", name="date_exceptions_slot_date"),
        sa.CheckConstraint("kind IN ('CLOSED', 'HOURS')", name="date_exceptions_kind"),
        sa.CheckConstraint('kind = \'CLOSED\' OR (start IS NOT NULL AND "end" IS NOT NULL AND start < "end")', name="date_exceptions_hours"),
        sa.CheckConstraint("patient_limit IS NULL OR patient_limit BETWEEN 1 AND 500", name="date_exceptions_limit"),
    )


def downgrade() -> None:
    op.drop_table("date_exceptions")
    op.drop_table("weekly_hours")
