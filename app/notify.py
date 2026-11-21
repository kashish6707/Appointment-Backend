"""Which real-time events each kind of change sends. Call these after committing."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import object_session

from app import events
from app.models import Booking, OpdSession
from app.serializers import booking_item, session_json


def booking_changed(booking: Booking, doctor_id: uuid.UUID, event: str = "booking.updated") -> None:
    """event: booking.created, booking.updated or booking.cancelled (the doctor's dashboard event)."""
    events.emit(f"doctor:{doctor_id}", event, booking_item(booking))
    events.emit(
        f"booking:{booking.id}",
        "booking.updated",
        {"id": str(booking.id), "token": booking.token, "status": booking.status},
    )


def queue_changed(session: OpdSession, doctor_id: uuid.UUID) -> None:
    """The session or its queue moved: status, now serving, or who is ahead of whom."""
    events.emit(f"doctor:{doctor_id}", "session.updated", session_json(session))
    events.emit(
        f"session:{session.id}",
        "session.updated",
        {
            "id": str(session.id),
            "status": session.status,
            "nowServing": session.current_token,
            "breakUntil": session.break_until.isoformat() if session.break_until else None,
        },
    )
    live_changed(session)


def live_changed(session: OpdSession) -> None:
    """Today's public queue status for patients browsing clinics (no patient details): OPD
    status, the token with the doctor, and how many are waiting."""
    from app import directory  # avoid an import cycle
    from app.timeutil import today_ist

    directory.forget(session.doctor_clinic_id)
    db = object_session(session)
    if db is None or session.date != today_ist():
        return
    with_doctor, waiting = db.execute(
        select(
            func.min(Booking.token).filter(Booking.status == "WITH_DOCTOR"),
            func.count(Booking.id).filter(Booking.status == "WAITING"),
        ).where(Booking.session_id == session.id)
    ).one()
    events.emit(
        f"live:{session.doctor_clinic_id}",
        "live.updated",
        {
            "doctorClinicId": str(session.doctor_clinic_id),
            "sessionStatus": session.status,
            "withDoctor": with_doctor,
            "waiting": waiting,
            "breakUntil": session.break_until.isoformat() if session.break_until else None,
        },
    )


def booking_and_queue(booking: Booking, doctor_id: uuid.UUID, event: str) -> None:
    booking_changed(booking, doctor_id, event)
    queue_changed(booking.session, doctor_id)
