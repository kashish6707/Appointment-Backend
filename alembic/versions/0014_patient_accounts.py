"""Patient accounts: login, sessions, password reset links, family profiles with health details,
and extra tracking links for signed-in patients.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "patient_accounts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.Column("phone", sa.Text, unique=True),
        sa.Column("email", sa.Text, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("phone IS NOT NULL OR email IS NOT NULL", name="patient_accounts_phone_or_email"),
    )
    op.create_table(
        "patient_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", UUID(as_uuid=True), sa.ForeignKey("patient_accounts.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "password_resets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", UUID(as_uuid=True), sa.ForeignKey("patient_accounts.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_table(
        "tracking_links",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("booking_id", UUID(as_uuid=True), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("code_hash", sa.Text, nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.alter_column("patients", "age", nullable=True)
    op.alter_column("patients", "gender", nullable=True)
    op.add_column("patients", sa.Column("account_id", UUID(as_uuid=True), sa.ForeignKey("patient_accounts.id", ondelete="SET NULL")))
    op.create_index("ix_patients_account_id", "patients", ["account_id"])
    for name, type_ in [
        ("relation", sa.Text), ("date_of_birth", sa.Date), ("blood_group", sa.Text), ("allergies", sa.Text),
        ("conditions", sa.Text), ("surgeries", sa.Text), ("medicines", sa.Text), ("emergency_name", sa.Text),
        ("emergency_relation", sa.Text), ("emergency_phone", sa.Text), ("address_area", sa.Text),
        ("address_city", sa.Text), ("profile_updated_at", sa.DateTime(timezone=True)),
    ]:
        op.add_column("patients", sa.Column(name, type_))


def downgrade() -> None:
    for name in ["relation", "date_of_birth", "blood_group", "allergies", "conditions", "surgeries", "medicines",
                 "emergency_name", "emergency_relation", "emergency_phone", "address_area", "address_city", "profile_updated_at"]:
        op.drop_column("patients", name)
    op.drop_index("ix_patients_account_id", "patients")
    op.drop_column("patients", "account_id")
    op.execute("DELETE FROM patients WHERE age IS NULL OR gender IS NULL")
    op.alter_column("patients", "gender", nullable=False)
    op.alter_column("patients", "age", nullable=False)
    op.drop_table("tracking_links")
    op.drop_table("password_resets")
    op.drop_table("patient_sessions")
    op.drop_table("patient_accounts")
