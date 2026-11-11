"""Patient-facing routes. No login: a booking is protected by its tracking code."""

import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from app import bookings, events
from app.deps import Db
from app.models import Booking, Clinic, Doctor, DoctorClinic, OpdSession
from app.schemas import BookingIn, CancelIn
from app.serializers import booking_item, clinic_slot
from app.timeutil import now_ist, today_ist

router = APIRouter(prefix="/api/public")


def _waiting_today():
    """Subquery: number of WAITING bookings in today's session, per doctor_clinic."""
    return (
        select(OpdSession.doctor_clinic_id, func.count(Booking.id).label("waiting"))
        .join(Booking, Booking.session_id == OpdSession.id)
        .where(OpdSession.date == today_ist(), Booking.status == "WAITING")
        .group_by(OpdSession.doctor_clinic_id)
        .subquery()
    )


def _doctor_profile(d: Doctor) -> dict:
    return {
        "id": str(d.id),
        "fullName": d.full_name,
        "department": d.department,
        "qualification": d.qualification,
        "experienceYears": d.experience_years,
    }


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/doctors")
def list_doctors(db: Db, search: str = "", department: str = ""):
    """One row per doctor per clinic. Only verified doctors are listed."""
    waiting = _waiting_today()
    query = (
        select(DoctorClinic, func.coalesce(waiting.c.waiting, 0))
        .join(Doctor)
        .join(Clinic)
        .outerjoin(waiting, waiting.c.doctor_clinic_id == DoctorClinic.id)
        .options(joinedload(DoctorClinic.doctor), joinedload(DoctorClinic.clinic))
        .where(Doctor.is_verified.is_(True))
        .order_by(Doctor.full_name, Clinic.name)
        .limit(100)
    )
    if search.strip():
        pattern = _like(search.strip()[:100])
        query = query.where(
            or_(
                Doctor.full_name.ilike(pattern),
                Doctor.department.ilike(pattern),
                Clinic.name.ilike(pattern),
                Clinic.area.ilike(pattern),
            )
        )
    if department.strip():
        query = query.where(func.lower(Doctor.department) == department.strip().lower())

    return [
        {
            **_doctor_profile(dc.doctor),
            "doctorId": str(dc.doctor.id),
            "clinic": clinic_slot(dc),
            "fee": dc.fee,
            "waitingToday": waiting_count,
        }
        for dc, waiting_count in db.execute(query).all()
    ]


@router.get("/doctors/{doctor_id}")
def get_doctor(doctor_id: uuid.UUID, db: Db):
    doctor = db.scalar(select(Doctor).where(Doctor.id == doctor_id, Doctor.is_verified.is_(True)))
    if doctor is None:
        raise HTTPException(404, "We couldn't find that doctor.")
    waiting = _waiting_today()
    today = today_ist()
    rows = db.execute(
        select(DoctorClinic, func.coalesce(waiting.c.waiting, 0), OpdSession.status, OpdSession.current_token)
        .outerjoin(waiting, waiting.c.doctor_clinic_id == DoctorClinic.id)
        .outerjoin(OpdSession, (OpdSession.doctor_clinic_id == DoctorClinic.id) & (OpdSession.date == today))
        .options(joinedload(DoctorClinic.clinic))
        .where(DoctorClinic.doctor_id == doctor.id)
        .order_by(DoctorClinic.opd_start)
    ).all()
    return {
        **_doctor_profile(doctor),
        "clinics": [
            {
                **clinic_slot(dc),
                "waitingToday": waiting_count,
                "todaySessionStatus": status or "NOT_STARTED",
                "nowServing": current_token,
            }
            for dc, waiting_count, status, current_token in rows
        ],
    }


def notify_booking(booking: Booking, event: str, doctor_id: uuid.UUID) -> None:
    """Tell the doctor's dashboard and everyone watching this session's queue."""
    events.emit(f"doctor:{doctor_id}", event, booking_item(booking))
    events.emit(f"booking:{booking.id}", "booking.updated", {"id": str(booking.id), "status": booking.status})
    events.emit(f"session:{booking.session_id}", "session.updated", {"id": str(booking.session_id)})


@router.post("/bookings", status_code=201)
def create_booking(body: BookingIn, db: Db):
    dc = bookings.get_bookable_slot(db, body.doctor_clinic_id)
    bookings.check_bookable_date(dc, body.date, now_ist())
    session = bookings.lock_session(db, dc.id, body.date)
    patient = bookings.find_or_create_patient(
        db,
        name=body.patient.name,
        phone=body.patient.phone,
        age=body.patient.age,
        gender=body.patient.gender,
        height_cm=body.patient.height_cm,
        weight_kg=body.patient.weight_kg,
        records_consent=body.records_consent,
    )
    booking, code = bookings.add_booking(db, session, patient, visit_type=body.visit_type, source="ONLINE")
    db.commit()
    notify_booking(booking, "booking.created", dc.doctor_id)
    return {"bookingId": str(booking.id), "token": booking.token, "trackingCode": code}


@router.get("/bookings/{booking_id}")
def booking_status(booking_id: uuid.UUID, db: Db, code: str = ""):
    if not code:
        raise HTTPException(400, "Add the tracking code from your booking link.")
    booking = bookings.get_booking_by_code(db, booking_id, code)
    return bookings.queue_status(db, booking)


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: uuid.UUID, body: CancelIn, db: Db):
    booking = bookings.get_booking_by_code(db, booking_id, body.code, lock=True)
    if booking.status != "WAITING":
        raise HTTPException(409, "This booking can't be cancelled now.")
    booking.status = "CANCELLED"
    booking.cancelled_at = now_ist()
    booking.cancelled_by = "PATIENT"
    booking.cancel_reason = body.reason
    doctor_id = booking.session.doctor_clinic.doctor_id
    db.commit()
    notify_booking(booking, "booking.cancelled", doctor_id)
    return {"bookingId": str(booking.id), "status": booking.status}
