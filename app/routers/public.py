"""Patient-facing routes. No login: a booking is protected by its tracking code."""

import hmac
import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from app import booking_payments, bookings, notify, security
from app.deps import Db
from app.models import Booking, Clinic, Doctor, DoctorClinic, OpdSession, Payment
from app.payments import get_gateway
from app.schemas import BookingIn, CancelIn, PaymentConfirmIn
from app.serializers import clinic_slot
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


def _rupees(paise: int) -> int:
    return paise // 100


@router.post("/bookings", status_code=201)
def create_booking(body: BookingIn, db: Db):
    dc = bookings.get_bookable_slot(db, body.doctor_clinic_id)
    bookings.check_bookable_date(dc, body.date, now_ist())

    if dc.token_amount > 0:
        # Pay first; the token is assigned when the payment is confirmed.
        bookings.precheck_online_booking(db, dc, body.date, body.patient.name, body.patient.phone)
        patient = body.patient.model_dump(by_alias=True, exclude_none=True)
        payment, code = booking_payments.start(
            db, dc, body.date, body.visit_type, patient, body.records_consent
        )
        return {
            "bookingId": str(payment.booking_id),
            "token": None,
            "trackingCode": code,
            "payment": booking_payments.checkout_json(payment),
        }

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
    notify.booking_and_queue(booking, dc.doctor_id, "booking.created")
    return {"bookingId": str(booking.id), "token": booking.token, "trackingCode": code, "payment": None}


@router.post("/bookings/{booking_id}/payment")
def confirm_payment(booking_id: uuid.UUID, body: PaymentConfirmIn, db: Db):
    """Called by the frontend with Razorpay Checkout's success result."""
    payment = booking_payments.lock_by_booking(db, booking_id, body.code)
    if payment is None:
        raise HTTPException(404, bookings.BOOKING_NOT_FOUND)
    if payment.gateway_order_id != body.razorpay_order_id or not get_gateway().verify_checkout(
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
    ):
        raise HTTPException(
            400,
            "We couldn't verify this payment. If money was deducted, "
            "it will be confirmed or refunded automatically.",
        )
    booking = booking_payments.confirm(db, payment, body.razorpay_payment_id)
    if booking is None:
        booking_payments.refund(db, payment)
        raise HTTPException(
            409,
            f"We couldn't book this token: {payment.note} "
            f"Your \u20b9{_rupees(payment.amount_paise)} will be refunded.",
        )
    notify.booking_and_queue(booking, payment.doctor_clinic.doctor_id, "booking.created")
    return {"bookingId": str(booking.id), "token": booking.token, "status": booking.status}


def _pending_payment_status(db: Db, booking_id: uuid.UUID, code: str) -> dict:
    """Status for a booking that is still waiting for (or failed) payment."""
    payment = db.scalar(select(Payment).where(Payment.booking_id == booking_id))
    if payment is None or not hmac.compare_digest(payment.tracking_code_hash, security.hash_token(code)):
        raise HTTPException(404, bookings.BOOKING_NOT_FOUND)
    dc = payment.doctor_clinic
    status = booking_payments.pending_status(payment)
    return {
        "bookingId": str(booking_id),
        "token": None,
        "status": status,
        "nowServing": None,
        "patientsAhead": None,
        "estimatedMinutes": None,
        "sessionStatus": None,
        "date": payment.date.isoformat(),
        "doctorName": dc.doctor.full_name,
        "clinicName": dc.clinic.name,
        "opdStart": dc.opd_start.strftime("%H:%M"),
        "payment": {
            **booking_payments.summary(payment, dc.fee),
            "checkout": booking_payments.checkout_json(payment) if status == "PENDING_PAYMENT" else None,
        },
    }


@router.get("/bookings/{booking_id}")
def booking_status(booking_id: uuid.UUID, db: Db, code: str = ""):
    if not code:
        raise HTTPException(400, "Add the tracking code from your booking link.")
    if db.get(Booking, booking_id) is None:
        return _pending_payment_status(db, booking_id, code)
    booking = bookings.get_booking_by_code(db, booking_id, code)
    payment = db.scalar(select(Payment).where(Payment.booking_id == booking.id))
    return {
        **bookings.queue_status(db, booking),
        "payment": booking_payments.summary(payment, booking.session.doctor_clinic.fee),
    }


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: uuid.UUID, body: CancelIn, db: Db):
    if db.get(Booking, booking_id) is None:
        # Not paid yet: cancel the pending payment so a late payment gets refunded, not booked.
        payment = booking_payments.lock_by_booking(db, booking_id, body.code)
        if payment is None:
            raise HTTPException(404, bookings.BOOKING_NOT_FOUND)
        if payment.status != "CREATED":
            raise HTTPException(409, "This booking can't be cancelled now.")
        payment.status = "CANCELLED"
        db.commit()
        return {"bookingId": str(booking_id), "status": "CANCELLED", "refund": None}

    booking = bookings.get_booking_by_code(db, booking_id, body.code, lock=True)
    if booking.status != "WAITING":
        raise HTTPException(409, "This booking can't be cancelled now.")
    booking.status = "CANCELLED"
    booking.cancelled_at = now_ist()
    booking.cancelled_by = "PATIENT"
    booking.cancel_reason = body.reason
    session = booking.session
    doctor_id = session.doctor_clinic.doctor_id

    # The token amount is refunded if the patient cancels before that day's OPD starts.
    payment = db.scalar(select(Payment).where(Payment.booking_id == booking.id).with_for_update())
    refundable = payment is not None and payment.status == "PAID" and session.status == "NOT_STARTED"
    if refundable:
        booking_payments.mark_for_refund(payment, "Cancelled by patient before OPD started")
    db.commit()
    notify.booking_and_queue(booking, doctor_id, "booking.cancelled")

    refund = None
    if refundable:
        booking_payments.refund(db, payment)
        refund = {"amount": _rupees(payment.amount_paise), "status": payment.status}
    return {"bookingId": str(booking.id), "status": booking.status, "refund": refund}
