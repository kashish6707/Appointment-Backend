"""Doctor profile: photo, standard fee, about, achievements, expertise, medical council, degrees

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-11
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("doctors", sa.Column("photo_key", sa.Text))
    op.add_column("doctors", sa.Column("photo_content_type", sa.Text))
    op.add_column("doctors", sa.Column("consultation_fee", sa.Integer))
    op.add_column("doctors", sa.Column("about", sa.Text))
    op.add_column("doctors", sa.Column("achievements", ARRAY(sa.Text), server_default="{}", nullable=False))
    op.add_column("doctors", sa.Column("expertise", ARRAY(sa.Text), server_default="{}", nullable=False))
    op.add_column("doctors", sa.Column("medical_council", sa.Text))
    op.create_check_constraint("doctors_fee", "doctors", "consultation_fee IS NULL OR consultation_fee BETWEEN 0 AND 100000")
    op.create_table(
        "doctor_degrees",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("doctor_id", UUID(as_uuid=True), sa.ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("position", sa.SmallInteger, nullable=False),
        sa.Column("level", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("institute", sa.Text),
        sa.Column("year", sa.SmallInteger),
        sa.CheckConstraint("level IN ('UG', 'PG', 'SUPER_SPECIALITY', 'FELLOWSHIP', 'DIPLOMA', 'OTHER')", name="doctor_degrees_level"),
    )


def downgrade() -> None:
    op.drop_table("doctor_degrees")
    op.drop_constraint("doctors_fee", "doctors")
    for col in ("medical_council", "expertise", "achievements", "about", "consultation_fee", "photo_content_type", "photo_key"):
        op.drop_column("doctors", col)
