"""The doctor running their OPD: start/pause/resume/end, call next, mark outcomes, walk-ins.

Every change locks the session row first (the same lock bookings take), so a double-clicked
"Next" or a booking arriving mid-change can't leave the queue in a mixed state.
"""

import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app import bookings, notify
from app.deps import CurrentDoctor, Db
from app.models import Booking, Doctor, DoctorClinic, OpdSession
from app.schemas import BookingStatusIn, WalkInIn
from app.serializers import booking_item, session_json
from app.timeutil import now_ist, today_ist

router = APIRouter(prefix="/api/doctor")

SESSION_NOT_FOUND = "We couldn't find that OPD session."
BOOKING_NOT_FOUND = "We couldn't find that booking."
CLOSED = "This OPD session is closed."


def _lock_session(db: Db, doctor: Doctor, session_id: uuid.UUID) -> OpdSession:
    session = db.scalar(
        select(OpdSession)
        .join(DoctorClinic)
        .where(OpdSession.id == session_id, DoctorClinic.doctor_id == doctor.id)
        .with_for_update(of=OpdSession)
    )
    if session is None:  # unknown, or another doctor's: same answer
        raise HTTPException(404, SESSION_NOT_FOUND)
    return session


def _require_today(session: OpdSession, message: str = "You can only run today's OPD.") -> None:
    if session.date != today_ist():
        raise HTTPException(409, message)


def _finish(session: OpdSession, doctor: Doctor, db: Db) -> dict:
    db.commit()
    notify.queue_changed(session, doctor.id)
    return {"session": session_json(session)}


# --- session status -----------------------------------------------------------------


@router.post("/sessions/{session_id}/start")
def start(session_id: uuid.UUID, doctor: CurrentDoctor, db: Db):
    session = _lock_session(db, doctor, session_id)
    _require_today(session)
    if session.status == "CLOSED":
        raise HTTPException(409, CLOSED)
    if session.status != "NOT_STARTED":
        raise HTTPException(409, "OPD has already started.")
    session.status = "IN_OPD"
    session.started_at = now_ist()
    return _finish(session, doctor, db)


@router.post("/sessions/{session_id}/pause")
def pause(session_id: uuid.UUID, doctor: CurrentDoctor, db: Db):
    session = _lock_session(db, doctor, session_id)
    _require_today(session)
    if session.status != "IN_OPD":
        raise HTTPException(409, "You can only take a break while OPD is running.")
    session.status = "ON_BREAK"
    return _finish(session, doctor, db)


@router.post("/sessions/{session_id}/resume")
def resume(session_id: uuid.UUID, doctor: CurrentDoctor, db: Db):
    session = _lock_session(db, doctor, session_id)
    _require_today(session)
    if session.status != "ON_BREAK":
        raise HTTPException(409, "OPD isn't on a break.")
    session.status = "IN_OPD"
    return _finish(session, doctor, db)


@router.post("/sessions/{session_id}/end")
def end(session_id: uuid.UUID, doctor: CurrentDoctor, db: Db):
    """Close the session (any date: this is also how a doctor closes a future day for bookings).

    The patient currently with the doctor is marked DONE. Waiting patients are left as they are.
    """
    session = _lock_session(db, doctor, session_id)
    if session.status == "CLOSED":
        raise HTTPException(409, CLOSED)
    session.status = "CLOSED"
    finished = _complete_current(db, session)
    result = _finish(session, doctor, db)
    for booking in finished:
        notify.booking_changed(booking, doctor.id)
    return result


# --- the queue ----------------------------------------------------------------------


def _complete_current(db: Db, session: OpdSession) -> list[Booking]:
    current = db.scalars(
        select(Booking)
        .where(Booking.session_id == session.id, Booking.status == "WITH_DOCTOR")
        .with_for_update()
    ).all()
    for booking in current:
        booking.status = "DONE"
        booking.completed_at = now_ist()
    return list(current)


@router.post("/sessions/{session_id}/next")
def call_next(session_id: uuid.UUID, doctor: CurrentDoctor, db: Db):
    """Mark the patient with the doctor as DONE and call the lowest waiting token."""
    session = _lock_session(db, doctor, session_id)
    _require_today(session)
    if session.status == "NOT_STARTED":
        raise HTTPException(409, "Start the OPD first.")
    if session.status == "ON_BREAK":
        raise HTTPException(409, "You're on a break. Resume OPD first.")
    if session.status == "CLOSED":
        raise HTTPException(409, CLOSED)

    finished = _complete_current(db, session)
    called = db.scalar(
        select(Booking)
        .where(Booking.session_id == session.id, Booking.status == "WAITING")
        .order_by(Booking.token)
        .limit(1)
        .with_for_update()
    )
    if called is not None:
        called.status = "WITH_DOCTOR"
        called.called_at = now_ist()
        session.current_token = called.token
    db.commit()

    for booking in [*finished, *([called] if called else [])]:
        notify.booking_changed(booking, doctor.id)
    notify.queue_changed(session, doctor.id)
    return {
        "session": session_json(session),
        "finished": booking_item(finished[0]) if finished else None,
        "current": booking_item(called) if called else None,
    }


@router.patch("/bookings/{booking_id}/status")
def set_status(booking_id: uuid.UUID, body: BookingStatusIn, doctor: CurrentDoctor, db: Db):
    """Record the outcome: DONE, SENT_FOR_TESTS or NO_SHOW. Also used to correct a mistake."""
    booking = db.scalar(
        select(Booking)
        .join(OpdSession)
        .join(DoctorClinic)
        .where(Booking.id == booking_id, DoctorClinic.doctor_id == doctor.id)
        .with_for_update(of=Booking)
    )
    if booking is None:
        raise HTTPException(404, BOOKING_NOT_FOUND)
    if booking.status == "CANCELLED":
        raise HTTPException(409, "This booking was cancelled.")

    if body.status != booking.status:
        if body.status in ("DONE", "SENT_FOR_TESTS"):
            if booking.called_at and booking.completed_at is None:
                booking.completed_at = now_ist()
        else:  # NO_SHOW: they weren't seen, so it doesn't count towards consult times
            booking.completed_at = None
        booking.status = body.status
        db.commit()
        notify.booking_and_queue(booking, doctor.id, "booking.updated")
    return booking_item(booking)


@router.post("/sessions/{session_id}/walk-ins", status_code=201)
def add_walk_in(session_id: uuid.UUID, body: WalkInIn, doctor: CurrentDoctor, db: Db):
    """Reception adds a patient at the desk. They pay in full there; no online payment."""
    session = _lock_session(db, doctor, session_id)
    _require_today(session, "Walk-ins can only be added to today's OPD.")
    patient = bookings.find_or_create_patient(
        db, name=body.name, phone=body.phone, age=body.age, gender=body.gender
    )
    booking, code = bookings.add_booking(db, session, patient, visit_type=body.visit_type, source="WALK_IN")
    db.commit()
    notify.booking_and_queue(booking, doctor.id, "booking.created")
    return {"booking": booking_item(booking), "trackingCode": code}
