"""Which real-time events each kind of change sends. Call these after committing."""

import uuid

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
        {"id": str(session.id), "status": session.status, "nowServing": session.current_token},
    )


def booking_and_queue(booking: Booking, doctor_id: uuid.UUID, event: str) -> None:
    booking_changed(booking, doctor_id, event)
    queue_changed(booking.session, doctor_id)
