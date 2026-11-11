"""clinics opd bookings records

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('clinics',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('area', sa.Text(), nullable=False),
    sa.Column('address', sa.Text(), nullable=False),
    sa.Column('phone', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('patients',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('phone', sa.Text(), nullable=True),
    sa.Column('age', sa.SmallInteger(), nullable=False),
    sa.Column('gender', sa.Text(), nullable=False),
    sa.Column('height_cm', sa.Numeric(precision=5, scale=1), nullable=True),
    sa.Column('weight_kg', sa.Numeric(precision=5, scale=1), nullable=True),
    sa.Column('records_consent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("gender IN ('MALE', 'FEMALE', 'OTHER')", name='patients_gender'),
    sa.CheckConstraint('age BETWEEN 0 AND 120', name='patients_age'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_patients_phone'), 'patients', ['phone'], unique=False)
    op.create_table('doctor_clinics',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('doctor_id', sa.UUID(), nullable=False),
    sa.Column('clinic_id', sa.UUID(), nullable=False),
    sa.Column('fee', sa.Integer(), nullable=False),
    sa.Column('opd_days', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('opd_start', sa.Time(), nullable=False),
    sa.Column('opd_end', sa.Time(), nullable=False),
    sa.CheckConstraint("opd_days <@ ARRAY['MON','TUE','WED','THU','FRI','SAT','SUN']::text[]", name='doctor_clinics_opd_days'),
    sa.CheckConstraint('fee >= 0', name='doctor_clinics_fee_nonnegative'),
    sa.CheckConstraint('opd_start < opd_end', name='doctor_clinics_opd_hours'),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['doctor_id'], ['doctors.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_doctor_clinics_clinic_id'), 'doctor_clinics', ['clinic_id'], unique=False)
    op.create_index(op.f('ix_doctor_clinics_doctor_id'), 'doctor_clinics', ['doctor_id'], unique=False)
    op.create_table('medical_records',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('added_by', sa.Text(), nullable=False),
    sa.Column('file_key', sa.Text(), nullable=False),
    sa.Column('content_type', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("added_by IN ('PATIENT', 'CLINIC')", name='medical_records_added_by'),
    sa.CheckConstraint("kind IN ('PRESCRIPTION', 'REPORT')", name='medical_records_kind'),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('file_key')
    )
    op.create_index(op.f('ix_medical_records_patient_id'), 'medical_records', ['patient_id'], unique=False)
    op.create_table('opd_sessions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('doctor_clinic_id', sa.UUID(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('status', sa.Text(), server_default='NOT_STARTED', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('current_token', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('NOT_STARTED', 'IN_OPD', 'ON_BREAK', 'CLOSED')", name='opd_sessions_status'),
    sa.ForeignKeyConstraint(['doctor_clinic_id'], ['doctor_clinics.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('doctor_clinic_id', 'date', name='opd_sessions_doctor_clinic_date')
    )
    op.create_table('bookings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('token', sa.Integer(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('visit_type', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='WAITING', nullable=False),
    sa.Column('booked_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.Column('cancelled_by', sa.Text(), nullable=True),
    sa.Column('tracking_code_hash', sa.Text(), nullable=False),
    sa.CheckConstraint("cancelled_by IS NULL OR cancelled_by IN ('PATIENT', 'CLINIC')", name='bookings_cancelled_by'),
    sa.CheckConstraint("source IN ('ONLINE', 'WALK_IN')", name='bookings_source'),
    sa.CheckConstraint("status IN ('WAITING', 'WITH_DOCTOR', 'DONE', 'SENT_FOR_TESTS', 'NO_SHOW', 'CANCELLED')", name='bookings_status'),
    sa.CheckConstraint("visit_type IN ('NEW', 'FOLLOW_UP', 'REPORT_REVIEW')", name='bookings_visit_type'),
    sa.CheckConstraint('token > 0', name='bookings_token_positive'),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['opd_sessions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('session_id', 'token', name='bookings_session_token'),
    sa.UniqueConstraint('tracking_code_hash')
    )
    op.create_index(op.f('ix_bookings_patient_id'), 'bookings', ['patient_id'], unique=False)
    op.create_table('test_advice',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('booking_id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('advised_on', sa.Date(), nullable=False),
    sa.Column('status', sa.Text(), server_default='PENDING', nullable=False),
    sa.Column('record_id', sa.UUID(), nullable=True),
    sa.CheckConstraint("status IN ('PENDING', 'DONE')", name='test_advice_status'),
    sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['record_id'], ['medical_records.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_test_advice_booking_id'), 'test_advice', ['booking_id'], unique=False)
    op.create_index(op.f('ix_test_advice_patient_id'), 'test_advice', ['patient_id'], unique=False)
    op.add_column('doctors', sa.Column('department', sa.Text(), nullable=True))
    op.add_column('doctors', sa.Column('qualification', sa.Text(), nullable=True))
    op.add_column('doctors', sa.Column('experience_years', sa.SmallInteger(), nullable=True))
    op.add_column('doctors', sa.Column('registration_no', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('doctors', 'registration_no')
    op.drop_column('doctors', 'experience_years')
    op.drop_column('doctors', 'qualification')
    op.drop_column('doctors', 'department')
    op.drop_index(op.f('ix_test_advice_patient_id'), table_name='test_advice')
    op.drop_index(op.f('ix_test_advice_booking_id'), table_name='test_advice')
    op.drop_table('test_advice')
    op.drop_index(op.f('ix_bookings_patient_id'), table_name='bookings')
    op.drop_table('bookings')
    op.drop_table('opd_sessions')
    op.drop_index(op.f('ix_medical_records_patient_id'), table_name='medical_records')
    op.drop_table('medical_records')
    op.drop_index(op.f('ix_doctor_clinics_doctor_id'), table_name='doctor_clinics')
    op.drop_index(op.f('ix_doctor_clinics_clinic_id'), table_name='doctor_clinics')
    op.drop_table('doctor_clinics')
    op.drop_index(op.f('ix_patients_phone'), table_name='patients')
    op.drop_table('patients')
    op.drop_table('clinics')
