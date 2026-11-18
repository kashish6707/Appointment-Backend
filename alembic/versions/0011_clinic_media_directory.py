"""Directory: areas, clinic type/location/photos/Hindi name/hours text, doctor designation,
Hindi name, photo consent and department key

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-12
"""
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

from app.departments import AREAS, match

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    areas = op.create_table(
        "areas",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("name_hi", sa.Text, nullable=False),
        sa.Column("lat", sa.Numeric(9, 6), nullable=False),
        sa.Column("lng", sa.Numeric(9, 6), nullable=False),
    )
    op.bulk_insert(areas, [{"id": uuid.uuid4(), "key": k, "name": n, "name_hi": h, "lat": la, "lng": ln} for k, n, h, la, ln in AREAS])

    op.add_column("clinics", sa.Column("type", sa.Text, nullable=False, server_default="CLINIC"))
    op.add_column("clinics", sa.Column("name_hi", sa.Text))
    op.add_column("clinics", sa.Column("area_id", UUID(as_uuid=True), sa.ForeignKey("areas.id", ondelete="SET NULL")))
    op.add_column("clinics", sa.Column("lat", sa.Numeric(9, 6)))
    op.add_column("clinics", sa.Column("lng", sa.Numeric(9, 6)))
    op.add_column("clinics", sa.Column("cover_photo_key", sa.Text))
    op.add_column("clinics", sa.Column("opd_hours_text_en", sa.Text))
    op.add_column("clinics", sa.Column("opd_hours_text_hi", sa.Text))
    op.create_check_constraint("clinics_type", "clinics", "type IN ('CLINIC', 'HOSPITAL')")
    op.create_check_constraint("clinics_location", "clinics", "(lat IS NULL) = (lng IS NULL) AND (lat IS NULL OR (lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180))")
    op.create_table(
        "clinic_photos",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("clinic_id", UUID(as_uuid=True), sa.ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("position", sa.SmallInteger, nullable=False),
        sa.Column("key", sa.Text, nullable=False, unique=True),
        sa.Column("width", sa.SmallInteger, nullable=False),
        sa.Column("height", sa.SmallInteger, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.add_column("doctors", sa.Column("designation", sa.Text))
    op.add_column("doctors", sa.Column("name_hi", sa.Text))
    op.add_column("doctors", sa.Column("photo_consent_at", sa.DateTime(timezone=True)))
    op.add_column("doctors", sa.Column("department_key", sa.Text))

    # Match existing doctors' specialisations to departments, and clinics' area text to areas.
    conn = op.get_bind()
    for doctor_id, department in conn.execute(sa.text("SELECT id, department FROM doctors WHERE department IS NOT NULL")).all():
        conn.execute(sa.text("UPDATE doctors SET department_key = :k WHERE id = :id"), {"k": match(department), "id": doctor_id})
    conn.execute(sa.text("UPDATE clinics c SET area_id = a.id FROM areas a WHERE lower(c.area) = lower(a.name)"))


def downgrade() -> None:
    for col in ("department_key", "photo_consent_at", "name_hi", "designation"):
        op.drop_column("doctors", col)
    op.drop_table("clinic_photos")
    op.drop_constraint("clinics_location", "clinics")
    op.drop_constraint("clinics_type", "clinics")
    for col in ("opd_hours_text_hi", "opd_hours_text_en", "cover_photo_key", "lng", "lat", "area_id", "name_hi", "type"):
        op.drop_column("clinics", col)
    op.drop_table("areas")
