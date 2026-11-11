"""payments and token amount

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('payments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('booking_id', sa.UUID(), nullable=False),
    sa.Column('doctor_clinic_id', sa.UUID(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('visit_type', sa.Text(), nullable=False),
    sa.Column('request', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('tracking_code_hash', sa.Text(), nullable=False),
    sa.Column('amount_paise', sa.Integer(), nullable=False),
    sa.Column('currency', sa.Text(), server_default='INR', nullable=False),
    sa.Column('gateway', sa.Text(), nullable=False),
    sa.Column('gateway_order_id', sa.Text(), nullable=False),
    sa.Column('gateway_payment_id', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), server_default='CREATED', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('refund_id', sa.Text(), nullable=True),
    sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.CheckConstraint("status IN ('CREATED', 'PAID', 'CANCELLED', 'REFUND_PENDING', 'REFUNDED', 'REFUND_FAILED')", name='payments_status'),
    sa.CheckConstraint('amount_paise > 0', name='payments_amount_positive'),
    sa.ForeignKeyConstraint(['doctor_clinic_id'], ['doctor_clinics.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('booking_id'),
    sa.UniqueConstraint('gateway_order_id'),
    sa.UniqueConstraint('tracking_code_hash')
    )
    op.create_index(op.f('ix_payments_doctor_clinic_id'), 'payments', ['doctor_clinic_id'], unique=False)
    op.add_column('doctor_clinics', sa.Column('token_amount', sa.Integer(), server_default='0', nullable=False))
    op.create_check_constraint('doctor_clinics_token_amount', 'doctor_clinics', 'token_amount BETWEEN 0 AND fee')


def downgrade() -> None:
    op.drop_constraint('doctor_clinics_token_amount', 'doctor_clinics', type_='check')
    op.drop_column('doctor_clinics', 'token_amount')
    op.drop_index(op.f('ix_payments_doctor_clinic_id'), table_name='payments')
    op.drop_table('payments')
