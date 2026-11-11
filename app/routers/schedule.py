"""OPD timings: weekly hours and limits per weekday, and date exceptions (days off, different hours).

Changing them can take away a day that patients have already booked. Then the request is refused
with 409 code HAS_BOOKINGS (and how many), unless it says confirmCancel: those bookings are then
cancelled as "Doctor unavailable" and online token payments refunded.
"""

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.orm import joinedload

from app import booking_payments, notify, schedule
from app.access import Actor, CanManageSchedule, audit
from app.bookings import MAX_DAYS_AHEAD
from app.deps import Db
from app.models import Booking, DateException, DoctorClinic, OpdSession, Payment, WeeklyHours
from app.schemas import DateExceptionIn, WeeklyHoursIn
from app.serializers import clinic_slot
from app.timeutil import now_ist, today_ist

router = APIRouter(prefix="/api/doctor")

MAX_EXCEPTION_DAYS = 90
UNAVAILABLE_REASON = "Doctor unavailable"


class HasBookings(HTTPException):
    def __init__(self, count: int, dates: list[date]):
        n = f"{count} patient{'s are' if count != 1 else ' is'}"
        super().__init__(409, f"{n} booked on the days this takes away. Confirm to cancel their bookings and refund online payments.")
        self.extra = {"code": "HAS_BOOKINGS", "count": count, "dates": [d.isoformat() for d in dates]}


def _slot(db: Db, actor: Actor, doctor_clinic_id: uuid.UUID) -> DoctorClinic:
    dc = db.scalar(
        select(DoctorClinic)
        .options(joinedload(DoctorClinic.clinic))
        .where(DoctorClinic.id == doctor_clinic_id, actor.slot_filter())
        .with_for_update(of=DoctorClinic)  # one timing change at a time per slot
    )
    if dc is None:
        raise HTTPException(404, "We couldn't find that clinic.")
    return dc


def _exception_json(e: DateException) -> dict:
    return {
        "date": e.date.isoformat(),
        "kind": e.kind,
        "start": schedule.hhmm(e.start),
        "end": schedule.hhmm(e.end),
        "limit": e.patient_limit,
        "note": e.note,
        "createdBy": e.created_by,
    }


def _schedule_json(db: Db, dc: DoctorClinic) -> dict:
    today = today_ist()
    exceptions = db.scalars(
        select(DateException)
        .where(DateException.doctor_clinic_id == dc.id, DateException.date >= today)
        .order_by(DateException.date)
    ).all()
    plans = schedule.plans(db, dc, today, today + timedelta(days=MAX_DAYS_AHEAD))
    booked = schedule.booked_by_date(db, dc, today, today + timedelta(days=MAX_DAYS_AHEAD))
    return {
        "clinic": clinic_slot(dc),
        "weekly": schedule.weekly_json(db, dc),
        "exceptions": [_exception_json(e) for e in exceptions],
        # What patients can book right now, so the doctor sees the effect of their changes.
        "upcoming": [{**p.json(), "booked": booked.get(d, 0)} for d, p in plans.items()],
        "bookingWindowDays": MAX_DAYS_AHEAD,
    }


def _bookings_on_closed_days(db: Db, dc: DoctorClinic) -> list[Booking]:
    """Waiting bookings, today or later, on dates the (already flushed) timings make closed."""
    rows = db.scalars(
        select(Booking)
        .join(OpdSession, Booking.session_id == OpdSession.id)
        .options(joinedload(Booking.session))
        .where(OpdSession.doctor_clinic_id == dc.id, OpdSession.date >= today_ist(), Booking.status == "WAITING")
        .with_for_update(of=Booking)
    ).unique().all()
    if not rows:
        return []
    dates = [b.session.date for b in rows]
    plans = schedule.plans(db, dc, min(dates), max(dates))
    return [b for b in rows if not plans[b.session.date].open]


def _apply(db: Db, actor: Actor, dc: DoctorClinic, confirm: bool, action: str, **detail) -> dict:
    """Finish a timing change: refuse, or cancel and refund bookings on days it closed."""
    db.flush()
    affected = _bookings_on_closed_days(db, dc)
    if affected and not confirm:
        db.rollback()
        raise HasBookings(len(affected), sorted({b.session.date for b in affected}))

    refunds: list[Payment] = []
    for b in affected:
        b.status = "CANCELLED"
        b.cancelled_at = now_ist()
        b.cancelled_by = "CLINIC"
        b.cancel_reason = UNAVAILABLE_REASON
        payment = db.scalar(select(Payment).where(Payment.booking_id == b.id).with_for_update())
        if payment is not None and payment.status == "PAID":
            booking_payments.mark_for_refund(payment, UNAVAILABLE_REASON)
            refunds.append(payment)
    audit(db, actor, action, f"clinic:{dc.id}", cancelled=len(affected), **detail)
    db.commit()

    for b in affected:
        notify.booking_and_queue(b, actor.doctor.id, "booking.cancelled")
    for payment in refunds:
        booking_payments.refund(db, payment)
    return {**_schedule_json(db, dc), "cancelled": len(affected), "refunded": len(refunds)}


@router.get("/clinics/{doctor_clinic_id}/schedule")
def get_schedule(doctor_clinic_id: uuid.UUID, actor: CanManageSchedule, db: Db):
    dc = _slot(db, actor, doctor_clinic_id)
    result = _schedule_json(db, dc)
    db.commit()  # release the row lock
    return result


@router.put("/clinics/{doctor_clinic_id}/weekly")
def set_weekly(doctor_clinic_id: uuid.UUID, body: WeeklyHoursIn, actor: CanManageSchedule, db: Db):
    """Replace the regular hours for all seven weekdays."""
    dc = _slot(db, actor, doctor_clinic_id)
    db.execute(delete(WeeklyHours).where(WeeklyHours.doctor_clinic_id == dc.id))
    open_days = [d for d in body.days if d.open]
    for d in open_days:
        db.add(WeeklyHours(doctor_clinic_id=dc.id, weekday=d.weekday, start=d.start, end=d.end, patient_limit=d.limit))
    # Keep the slot's summary in step (used for listings and sorting).
    order = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    dc.opd_days = [wd for wd in order if any(d.weekday == wd for d in open_days)]
    if open_days:
        dc.opd_start = min(d.start for d in open_days)
        dc.opd_end = max(d.end for d in open_days)
    return _apply(
        db, actor, dc, body.confirm_cancel, "schedule.weekly",
        days={d.weekday: (f"{schedule.hhmm(d.start)}-{schedule.hhmm(d.end)}" + (f" x{d.limit}" if d.limit else "")) for d in open_days},
    )


@router.post("/clinics/{doctor_clinic_id}/exceptions")
def add_exception(doctor_clinic_id: uuid.UUID, body: DateExceptionIn, actor: CanManageSchedule, db: Db):
    """Mark one date, or each date in a range, as a day off or with different hours."""
    dc = _slot(db, actor, doctor_clinic_id)
    first, last = body.from_date, body.to_date or body.from_date
    if first < today_ist():
        raise HTTPException(400, "Choose today or a later date.")
    if (last - first).days >= MAX_EXCEPTION_DAYS:
        raise HTTPException(400, f"Choose at most {MAX_EXCEPTION_DAYS} days at a time.")

    existing = {
        e.date: e
        for e in db.scalars(
            select(DateException).where(DateException.doctor_clinic_id == dc.id, DateException.date.between(first, last))
        )
    }
    day = first
    while day <= last:
        e = existing.get(day) or DateException(doctor_clinic_id=dc.id, date=day)
        e.kind = body.kind
        e.start = body.start if body.kind == "HOURS" else None
        e.end = body.end if body.kind == "HOURS" else None
        e.patient_limit = body.limit if body.kind == "HOURS" else None
        e.note = body.note
        e.created_by = actor.name
        db.add(e)
        day += timedelta(days=1)
    return _apply(
        db, actor, dc, body.confirm_cancel, "schedule.exception",
        kind=body.kind, dates=f"{first.isoformat()}..{last.isoformat()}", note=body.note,
    )


@router.delete("/clinics/{doctor_clinic_id}/exceptions/{day}")
def remove_exception(
    doctor_clinic_id: uuid.UUID,
    day: date,
    actor: CanManageSchedule,
    db: Db,
    confirm_cancel: bool = Query(False, alias="confirmCancel"),
):
    """Back to the weekly hours for that date. Bookings cancelled earlier stay cancelled.
    (If the date was only open because of this change, its bookings are handled as above.)"""
    dc = _slot(db, actor, doctor_clinic_id)
    found = db.execute(
        delete(DateException).where(DateException.doctor_clinic_id == dc.id, DateException.date == day)
    ).rowcount
    if not found:
        raise HTTPException(404, "There's no change saved for that date.")
    return _apply(db, actor, dc, confirm_cancel, "schedule.exception_removed", date=day.isoformat())
