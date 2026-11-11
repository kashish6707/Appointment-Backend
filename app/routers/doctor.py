from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.deps import CurrentDoctor, Db
from app.models import Doctor, DoctorClinic

router = APIRouter(prefix="/api/doctor")


def clinic_json(dc: DoctorClinic) -> dict:
    return {
        "doctorClinicId": str(dc.id),
        "clinicId": str(dc.clinic.id),
        "name": dc.clinic.name,
        "area": dc.clinic.area,
        "address": dc.clinic.address,
        "phone": dc.clinic.phone,
        "fee": dc.fee,
        "opdDays": dc.opd_days,
        "opdStart": dc.opd_start.strftime("%H:%M"),
        "opdEnd": dc.opd_end.strftime("%H:%M"),
    }


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
            "clinics": [clinic_json(dc) for dc in clinics],
        }
    }
