"""The doctor running their OPD: start/pause/resume/end, call next, mark outcomes, walk-ins.

Every change locks the session row first (the same lock bookings take), so a double-clicked
"Next" or a booking arriving mid-change can't leave the queue in a mixed state.
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException
from sqlalchemy import select

from app import bookings, notifications, notify
from app.access import Actor, CanAddWalkIns, CanCall, CanManageOpd, CanSetOutcome, audit
from app.deps import Db
from app.models import Booking, DoctorClinic, OpdSession
from app.schemas import BookingStatusIn, WalkInIn
from app.serializers import booking_item, session_json
from app.timeutil import now_ist, today_ist

router = APIRouter(prefix="/api/doctor")

SESSION_NOT_FOUND = "We couldn't find that OPD session."
BOOKING_NOT_FOUND = "We couldn't find that booking."
CLOSED = "This OPD session is closed."


def _lock_session(db: Db, actor: Actor, session_id: uuid.UUID) -> OpdSession:
    session = db.scalar(
        select(OpdSession)
        .join(DoctorClinic)
        .where(OpdSession.id == session_id, actor.slot_filter())
        .with_for_update(of=OpdSession)
    )
    if session is None:  # unknown, or another doctor's: same answer
        raise HTTPException(404, SESSION_NOT_FOUND)
    return session


def _require_today(session: OpdSession, message: str = "You can only run today's OPD.") -> None:
    if session.date != today_ist():
        raise HTTPException(409, message)


def _finish(session: OpdSession, actor: Actor, db: Db, action: str) -> dict:
    audit(db, actor, action, f"session:{session.id}", date=session.date.isoformat())
    db.commit()
    notify.queue_changed(session, actor.doctor.id)
    return {"session": session_json(session)}


# --- session status -----------------------------------------------------------------


@router.post("/sessions/{session_id}/start")
def start(session_id: uuid.UUID, actor: CanManageOpd, db: Db, background: BackgroundTasks):
    """Start today's OPD and tell every waiting patient (sent in the background after saving)."""
    session = _lock_session(db, actor, session_id)
    _require_today(session)
    if session.status == "CLOSED":
        raise HTTPException(409, CLOSED)
    if session.status != "NOT_STARTED":
        raise HTTPException(409, "OPD has already started.")
    session.status = "IN_OPD"
    session.started_at = now_ist()
    queued = notifications.queue_opd_started(db, session)
    result = _finish(session, actor, db, "opd.start")
    background.add_task(notifications.deliver_due)
    return {**result, "notifications": notifications.session_summary(db, session.id), "notified": queued}


@router.post("/sessions/{session_id}/pause")
def pause(session_id: uuid.UUID, actor: CanManageOpd, db: Db):
    session = _lock_session(db, actor, session_id)
    _require_today(session)
    if session.status != "IN_OPD":
        raise HTTPException(409, "You can only take a break while OPD is running.")
    session.status = "ON_BREAK"
    return _finish(session, actor, db, "opd.pause")


@router.post("/sessions/{session_id}/resume")
def resume(session_id: uuid.UUID, actor: CanManageOpd, db: Db):
    session = _lock_session(db, actor, session_id)
    _require_today(session)
    if session.status != "ON_BREAK":
        raise HTTPException(409, "OPD isn't on a break.")
    session.status = "IN_OPD"
    return _finish(session, actor, db, "opd.resume")


@router.post("/sessions/{session_id}/end")
def end(session_id: uuid.UUID, actor: CanManageOpd, db: Db):
    """Close the session (any date: this is also how a doctor closes a future day for bookings).

    The patient currently with the doctor is marked DONE. Waiting patients are left as they are.
    Both can be undone with /reopen.
    """
    session = _lock_session(db, actor, session_id)
    if session.status == "CLOSED":
        raise HTTPException(409, CLOSED)
    session.status = "CLOSED"
    session.closed_at = now_ist()
    finished = _complete_current(db, session, at=session.closed_at)
    result = _finish(session, actor, db, "opd.end")
    for booking in finished:
        notify.booking_changed(booking, actor.doctor.id)
    return result


@router.post("/sessions/{session_id}/reopen")
def reopen(session_id: uuid.UUID, actor: CanManageOpd, db: Db):
    """Undo "End OPD" (pressed by mistake, or a closed coming day opened again).

    The session goes back to IN_OPD if it had started (NOT_STARTED otherwise), and the patient
    "End OPD" marked as seen goes back to WITH_DOCTOR, unless their status was changed since.
    """
    session = _lock_session(db, actor, session_id)
    if session.status != "CLOSED":
        raise HTTPException(409, "OPD isn't closed.")
    if session.date < today_ist():
        raise HTTPException(409, "You can only reopen today's OPD or a coming day.")

    restored: list[Booking] = []
    if session.closed_at is not None:
        restored = list(
            db.scalars(
                select(Booking)
                .where(
                    Booking.session_id == session.id,
                    Booking.status == "DONE",
                    Booking.completed_at == session.closed_at,
                )
                .with_for_update()
            ).all()
        )
        for booking in restored:
            booking.status = "WITH_DOCTOR"
            booking.completed_at = None
    session.status = "IN_OPD" if session.started_at else "NOT_STARTED"
    session.closed_at = None
    result = _finish(session, actor, db, "opd.reopen")
    for booking in restored:
        notify.booking_changed(booking, actor.doctor.id)
    return {**result, "restored": [booking_item(b) for b in restored]}


# --- the queue ----------------------------------------------------------------------


def _complete_current(db: Db, session: OpdSession, at=None) -> list[Booking]:
    current = db.scalars(
        select(Booking)
        .where(Booking.session_id == session.id, Booking.status == "WITH_DOCTOR")
        .with_for_update()
    ).all()
    for booking in current:
        booking.status = "DONE"
        booking.completed_at = at or now_ist()
    return list(current)


@router.post("/sessions/{session_id}/next")
def call_next(session_id: uuid.UUID, actor: CanCall, db: Db):
    """Mark the patient with the doctor as DONE and call the lowest waiting token."""
    session = _lock_session(db, actor, session_id)
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
    audit(db, actor, "queue.next", f"session:{session.id}", token=called.token if called else None)
    db.commit()

    for booking in [*finished, *([called] if called else [])]:
        notify.booking_changed(booking, actor.doctor.id)
    notify.queue_changed(session, actor.doctor.id)
    return {
        "session": session_json(session),
        "finished": booking_item(finished[0]) if finished else None,
        "current": booking_item(called) if called else None,
    }


@router.post("/bookings/{booking_id}/call")
def call_patient(booking_id: uuid.UUID, actor: CanCall, db: Db):
    """Like "next", but for one chosen waiting patient (e.g. called out of turn)."""
    session_id = db.scalar(
        select(Booking.session_id)
        .join(OpdSession)
        .join(DoctorClinic)
        .where(Booking.id == booking_id, actor.slot_filter())
    )
    if session_id is None:
        raise HTTPException(404, BOOKING_NOT_FOUND)
    session = _lock_session(db, actor, session_id)
    _require_today(session)
    if session.status == "NOT_STARTED":
        raise HTTPException(409, "Start the OPD first.")
    if session.status == "ON_BREAK":
        raise HTTPException(409, "You're on a break. Resume OPD first.")
    if session.status == "CLOSED":
        raise HTTPException(409, CLOSED)
    called = db.scalar(select(Booking).where(Booking.id == booking_id).with_for_update())
    if called.status != "WAITING":
        raise HTTPException(409, "This patient isn't waiting.")

    finished = _complete_current(db, session)
    called.status = "WITH_DOCTOR"
    called.called_at = now_ist()
    session.current_token = called.token
    audit(db, actor, "queue.call", f"booking:{called.id}", token=called.token)
    db.commit()

    for booking in [*finished, called]:
        notify.booking_changed(booking, actor.doctor.id)
    notify.queue_changed(session, actor.doctor.id)
    return {
        "session": session_json(session),
        "finished": booking_item(finished[0]) if finished else None,
        "current": booking_item(called),
    }


def apply_outcome(booking: Booking, status: str) -> bool:
    """Set DONE, SENT_FOR_TESTS or NO_SHOW, keeping consult times right. False if unchanged."""
    if status == booking.status:
        return False
    if status in ("DONE", "SENT_FOR_TESTS"):
        if booking.called_at and booking.completed_at is None:
            booking.completed_at = now_ist()
    else:  # NO_SHOW: they weren't seen, so it doesn't count towards consult times
        booking.completed_at = None
    booking.status = status
    return True


@router.patch("/bookings/{booking_id}/status")
def set_status(booking_id: uuid.UUID, body: BookingStatusIn, actor: CanSetOutcome, db: Db):
    """Record the outcome: DONE, SENT_FOR_TESTS or NO_SHOW. Also used to correct a mistake."""
    booking = db.scalar(
        select(Booking)
        .join(OpdSession)
        .join(DoctorClinic)
        .where(Booking.id == booking_id, actor.slot_filter())
        .with_for_update(of=Booking)
    )
    if booking is None:
        raise HTTPException(404, BOOKING_NOT_FOUND)
    if booking.status == "CANCELLED":
        raise HTTPException(409, "This booking was cancelled.")

    old = booking.status
    if apply_outcome(booking, body.status):
        audit(db, actor, "booking.status", f"booking:{booking.id}", token=booking.token, old=old, new=body.status)
        db.commit()
        notify.booking_and_queue(booking, actor.doctor.id, "booking.updated")
    return booking_item(booking)


@router.post("/sessions/{session_id}/walk-ins", status_code=201)
def add_walk_in(session_id: uuid.UUID, body: WalkInIn, actor: CanAddWalkIns, db: Db):
    """Reception adds a patient at the desk. They pay in full there; no online payment."""
    session = _lock_session(db, actor, session_id)
    _require_today(session, "Walk-ins can only be added to today's OPD.")
    patient = bookings.find_or_create_patient(
        db, name=body.name, phone=body.phone, age=body.age, gender=body.gender
    )
    booking, code = bookings.add_booking(
        db, session, patient, visit_type=body.visit_type, source="WALK_IN", allow_over_limit=body.over_limit
    )
    audit(db, actor, "walkin.add", f"booking:{booking.id}", token=booking.token, name=patient.name, overLimit=body.over_limit)
    db.commit()
    notify.booking_and_queue(booking, actor.doctor.id, "booking.created")
    return {"booking": booking_item(booking), "trackingCode": code}
