from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.deps import CurrentDoctor, Db
from app.models import DoctorClinic
from app.serializers import clinic_slot

router = APIRouter(prefix="/api/doctor")


@router.get("/me")
def me(doctor: CurrentDoctor, db: Db):
    clinics = db.scalars(
        select(DoctorClinic)
        .options(joinedload(DoctorClinic.clinic))
        .where(DoctorClinic.doctor_id == doctor.id)
        .order_by(DoctorClinic.opd_start)
    ).all()
    return {
        "doctor": {
            "id": str(doctor.id),
            "fullName": doctor.full_name,
            "phone": doctor.phone,
            "email": doctor.email,
            "department": doctor.department,
            "qualification": doctor.qualification,
            "experienceYears": doctor.experience_years,
            "registrationNo": doctor.registration_no,
            "verified": doctor.is_verified,
            "clinics": [clinic_slot(dc) for dc in clinics],
        }
    }
