"""Staff accounts and per-doctor access grants, audit log, patient corrections, visit measurements

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-11
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "staff",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.Column("phone", sa.Text, unique=True),
        sa.Column("email", sa.Text, unique=True),
        sa.Column("password_hash", sa.Text),  # null until the invite is accepted
        sa.Column("invite_token_hash", sa.Text, unique=True),
        sa.Column("invite_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("phone IS NOT NULL OR email IS NOT NULL", name="staff_phone_or_email"),
    )
    op.create_table(
        "staff_memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("staff_id", UUID(as_uuid=True), sa.ForeignKey("staff.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("doctor_id", UUID(as_uuid=True), sa.ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("permissions", ARRAY(sa.Text), nullable=False),
        sa.Column("doctor_clinic_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("staff_id", "doctor_id", name="staff_memberships_staff_doctor"),
        sa.CheckConstraint("role IN ('RECEPTIONIST', 'COMPOUNDER')", name="staff_memberships_role"),
    )

    # A session belongs to a doctor or to a staff member (who picks which doctor they work for).
    op.alter_column("sessions", "doctor_id", nullable=True)
    op.add_column("sessions", sa.Column("staff_id", UUID(as_uuid=True), sa.ForeignKey("staff.id", ondelete="CASCADE"), index=True))
    op.add_column("sessions", sa.Column("acting_doctor_id", UUID(as_uuid=True), sa.ForeignKey("doctors.id", ondelete="SET NULL")))
    op.create_check_constraint("sessions_one_owner", "sessions", "(doctor_id IS NULL) <> (staff_id IS NULL)")

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("doctor_id", UUID(as_uuid=True), sa.ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_kind", sa.Text, nullable=False),
        sa.Column("actor_id", UUID(as_uuid=True), nullable=False),
        sa.Column("actor_name", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target", sa.Text),
        sa.Column("detail", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("actor_kind IN ('DOCTOR', 'STAFF')", name="audit_log_actor_kind"),
    )
    op.create_index("ix_audit_log_doctor_created_at", "audit_log", ["doctor_id", "created_at"])

    op.create_table(
        "patient_corrections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("patient_id", UUID(as_uuid=True), sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("doctor_id", UUID(as_uuid=True), sa.ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text),
        sa.Column("age", sa.SmallInteger),
        sa.Column("gender", sa.Text),
        sa.Column("updated_by", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("patient_id", "doctor_id", name="patient_corrections_patient_doctor"),
        sa.CheckConstraint("age IS NULL OR age BETWEEN 0 AND 120", name="patient_corrections_age"),
        sa.CheckConstraint("gender IS NULL OR gender IN ('MALE', 'FEMALE', 'OTHER')", name="patient_corrections_gender"),
    )

    # Height and weight measured at this visit (the patient's own values stay on patients).
    op.add_column("bookings", sa.Column("height_cm", sa.Numeric(5, 1)))
    op.add_column("bookings", sa.Column("weight_kg", sa.Numeric(5, 1)))


def downgrade() -> None:
    op.drop_column("bookings", "weight_kg")
    op.drop_column("bookings", "height_cm")
    op.drop_table("patient_corrections")
    op.drop_index("ix_audit_log_doctor_created_at", "audit_log")
    op.drop_table("audit_log")
    op.drop_constraint("sessions_one_owner", "sessions")
    op.drop_column("sessions", "acting_doctor_id")
    op.drop_column("sessions", "staff_id")
    op.execute("DELETE FROM sessions WHERE doctor_id IS NULL")
    op.alter_column("sessions", "doctor_id", nullable=False)
    op.drop_table("staff_memberships")
    op.drop_table("staff")
