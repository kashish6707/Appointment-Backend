"""Booking rules shared by the public booking route and doctor walk-ins."""

import hmac
import uuid
from datetime import date, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import joinedload

from app import security
from app.models import WEEKDAYS, Booking, Doctor, DoctorClinic, OpdSession, Patient
from app.timeutil import IST, now_ist

MAX_DAYS_AHEAD = 7
ACTIVE_STATUSES = ("WAITING", "WITH_DOCTOR")
DEFAULT_CONSULT_MINUTES = 10
BOOKING_NOT_FOUND = "We couldn't find that booking. Check the link and try again."


def get_bookable_slot(db: DbSession, doctor_clinic_id: uuid.UUID) -> DoctorClinic:
    dc = db.scalar(
        select(DoctorClinic)
        .join(Doctor)
        .options(joinedload(DoctorClinic.doctor), joinedload(DoctorClinic.clinic))
        .where(DoctorClinic.id == doctor_clinic_id, Doctor.is_verified.is_(True))
    )
    if dc is None:
        raise HTTPException(404, "We couldn't find that doctor. They may no longer take bookings here.")
    return dc


def check_bookable_date(dc: DoctorClinic, day: date, now: datetime | None = None) -> None:
    now = now or now_ist()
    today = now.date()
    if day < today or day > today + timedelta(days=MAX_DAYS_AHEAD):
        raise HTTPException(400, f"You can book for today or up to {MAX_DAYS_AHEAD} days ahead.")
    if WEEKDAYS[day.weekday()] not in dc.opd_days:
        raise HTTPException(400, "The doctor doesn't hold OPD at this clinic on that day. Choose another day.")
    if day == today and now.time() >= dc.opd_end:
        raise HTTPException(400, "Today's OPD hours are over. Choose another day.")


def lock_session(db: DbSession, doctor_clinic_id: uuid.UUID, day: date) -> OpdSession:
    """Get (creating if needed) the day's session and lock its row until commit.

    Every booking for a session goes through this lock, so token numbers are handed out one
    at a time and can never clash. The unique (session_id, token) constraint is the backstop.
    """
    db.execute(
        insert(OpdSession)
        .values(id=uuid.uuid4(), doctor_clinic_id=doctor_clinic_id, date=day)
        .on_conflict_do_nothing(constraint="opd_sessions_doctor_clinic_date")
    )
    return db.scalar(
        select(OpdSession)
        .where(OpdSession.doctor_clinic_id == doctor_clinic_id, OpdSession.date == day)
        .with_for_update()
    )


def find_or_create_patient(
    db: DbSession,
    *,
    name: str,
    phone: str | None,
    age: int,
    gender: str,
    height_cm: float | None = None,
    weight_kg: float | None = None,
    records_consent: bool | None = None,
) -> Patient:
    """Same phone + same name (ignoring case) is the same patient. No phone: always a new one.

    records_consent: True grants consent (keeping the original time), False withdraws it,
    None (walk-ins) leaves it unchanged.
    """
    patient = None
    if phone:
        patient = db.scalar(
            select(Patient)
            .where(Patient.phone == phone, func.lower(Patient.name) == name.lower())
            .order_by(Patient.created_at)
            .limit(1)
            .with_for_update()
        )
    if patient is None:
        patient = Patient(name=name, phone=phone)
        db.add(patient)
    patient.age = age
    patient.gender = gender
    if height_cm is not None:
        patient.height_cm = height_cm
    if weight_kg is not None:
        patient.weight_kg = weight_kg
    if records_consent is True and patient.records_consent_at is None:
        patient.records_consent_at = now_ist()
    elif records_consent is False:
        patient.records_consent_at = None
    db.flush()
    return patient


CLOSED_MSG = "This OPD session is closed. Choose another day."


def active_token(db: DbSession, session_id: uuid.UUID, patient_id: uuid.UUID) -> int | None:
    return db.scalar(
        select(Booking.token).where(
            Booking.session_id == session_id,
            Booking.patient_id == patient_id,
            Booking.status.in_(ACTIVE_STATUSES),
        )
    )


def add_booking(
    db: DbSession,
    session: OpdSession,
    patient: Patient,
    *,
    visit_type: str,
    source: str,
    booking_id: uuid.UUID | None = None,
    tracking_code_hash: str | None = None,
) -> tuple[Booking, str | None]:
    """Give the patient the next token. The caller must hold the session lock (lock_session).

    Returns the booking and a new tracking code, or None for the code when the caller passed
    tracking_code_hash (the patient already has their code from the payment step).
    """
    if session.status == "CLOSED":
        raise HTTPException(409, CLOSED_MSG)
    already = active_token(db, session.id, patient.id)
    if already is not None:
        raise HTTPException(409, f"{patient.name} already has token {already} for this OPD session.")

    code = None
    if tracking_code_hash is None:
        code = security.new_tracking_code()
        tracking_code_hash = security.hash_token(code)
    last_token = db.scalar(select(func.max(Booking.token)).where(Booking.session_id == session.id))
    booking = Booking(
        id=booking_id or uuid.uuid4(),
        session_id=session.id,
        token=(last_token or 0) + 1,
        patient=patient,
        visit_type=visit_type,
        source=source,
        status="WAITING",
        tracking_code_hash=tracking_code_hash,
    )
    db.add(booking)
    db.flush()
    return booking, code


def precheck_online_booking(db: DbSession, dc: DoctorClinic, day: date, name: str, phone: str) -> None:
    """Refuse before taking money if the booking would fail anyway (closed day, duplicate)."""
    session = db.scalar(
        select(OpdSession).where(OpdSession.doctor_clinic_id == dc.id, OpdSession.date == day)
    )
    if session is None:
        return
    if session.status == "CLOSED":
        raise HTTPException(409, CLOSED_MSG)
    patient = db.scalar(
        select(Patient)
        .where(Patient.phone == phone, func.lower(Patient.name) == name.lower())
        .order_by(Patient.created_at)
        .limit(1)
    )
    if patient is not None:
        already = active_token(db, session.id, patient.id)
        if already is not None:
            raise HTTPException(409, f"{patient.name} already has token {already} for this OPD session.")


def get_booking_by_code(db: DbSession, booking_id: uuid.UUID, code: str, *, lock: bool = False) -> Booking:
    """The booking, if `code` is its tracking code. Unknown id and wrong code look the same."""
    query = select(Booking).where(Booking.id == booking_id)
    if lock:
        query = query.with_for_update()
    booking = db.scalar(query)
    if booking is None or not hmac.compare_digest(booking.tracking_code_hash, security.hash_token(code)):
        raise HTTPException(404, BOOKING_NOT_FOUND)
    return booking


def average_consult_minutes(db: DbSession, session_id: uuid.UUID) -> float:
    seconds = db.scalar(
        select(func.avg(func.extract("epoch", Booking.completed_at - Booking.called_at))).where(
            Booking.session_id == session_id,
            Booking.called_at.is_not(None),
            Booking.completed_at.is_not(None),
            Booking.status.in_(("DONE", "SENT_FOR_TESTS")),
        )
    )
    return float(seconds) / 60 if seconds else DEFAULT_CONSULT_MINUTES


def queue_status(db: DbSession, booking: Booking) -> dict:
    """What a patient's tracking page shows."""
    session = booking.session
    dc = session.doctor_clinic
    ahead = 0
    estimate = None
    if booking.status == "WAITING" and session.status != "CLOSED":
        ahead = db.scalar(
            select(func.count()).where(
                Booking.session_id == session.id, Booking.status == "WAITING", Booking.token < booking.token
            )
        )
        estimate = round(ahead * average_consult_minutes(db, session.id))
    return {
        "bookingId": str(booking.id),
        "token": booking.token,
        "status": booking.status,
        "nowServing": session.current_token,
        "patientsAhead": ahead,
        "estimatedMinutes": estimate,
        "sessionStatus": session.status,
        "sessionId": str(session.id),
        "date": session.date.isoformat(),
        "doctorName": dc.doctor.full_name,
        "clinicName": dc.clinic.name,
        "opdStart": dc.opd_start.strftime("%H:%M"),
    }
