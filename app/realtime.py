"""Live dashboard updates over Socket.IO at /api/socket.io.

The path is under /api so the Next.js server forwards it like every other API call, and the
browser's session cookie comes along. A connection is accepted only with a valid session:
- a doctor joins "doctor:<doctorId>" and gets every event for their clinics;
- a staff member joins "slot:<doctorClinicId>" for each clinic their grant covers, so they only
  get events for those clinics. The doctor can disconnect them (kick) after changing or removing
  their access; the dashboard then reloads and picks up the new access.
Events: booking.created, booking.updated, booking.cancelled and session.updated (app/notify.py).

Routes call events.emit() from worker threads (they are sync functions), so emits are handed to
the server's event loop. Rooms live in this process's memory: run a single worker, or add a
Socket.IO message queue (e.g. Redis) before running several.
"""

import asyncio
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie

import socketio
from sqlalchemy import select

from app import events, security
from app.config import get_settings
from app.db import SessionLocal
from app.models import Session, StaffMembership

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=[get_settings().frontend_origin])
_loop: asyncio.AbstractEventLoop | None = None
# Staff connections, so they can be disconnected when the doctor changes their access.
_staff_sids: dict[str, tuple[str, str]] = {}  # sid -> (staff_id, doctor_id)


def _rooms(cookie_header: str) -> tuple[list[str], tuple[str, str] | None] | None:
    """The rooms this cookie may join, and (staff_id, doctor_id) for a staff member."""
    try:
        morsel = SimpleCookie(cookie_header).get(security.COOKIE_NAME)
    except CookieError:
        return None
    if morsel is None or not morsel.value:
        return None
    with SessionLocal() as db:
        session = db.scalar(select(Session).where(Session.token_hash == security.hash_token(morsel.value)))
        if session is None or session.expires_at <= datetime.now(timezone.utc):
            return None
        if session.doctor_id is not None:
            return [f"doctor:{session.doctor_id}"], None
        mine = db.scalars(select(StaffMembership).where(StaffMembership.staff_id == session.staff_id)).all()
        if session.acting_doctor_id is not None:
            membership = next((m for m in mine if m.doctor_id == session.acting_doctor_id), None)
        else:  # working for one doctor only: the same automatic choice as app.access.get_actor
            membership = mine[0] if len(mine) == 1 else None
        if membership is None:
            return None
        rooms = [f"slot:{dc_id}" for dc_id in membership.doctor_clinic_ids]
        return rooms, (str(session.staff_id), str(membership.doctor_id))


def _doctor_id(cookie_header: str) -> str | None:
    """The doctor whose events this cookie may receive (used by tests and logs)."""
    found = _rooms(cookie_header)
    if not found:
        return None
    rooms, staff = found
    return staff[1] if staff else rooms[0].removeprefix("doctor:")


@sio.event
async def connect(sid, environ, auth=None):
    found = await asyncio.to_thread(_rooms, environ.get("HTTP_COOKIE", ""))
    if found is None:
        raise socketio.exceptions.ConnectionRefusedError("Please sign in.")
    rooms, staff = found
    for room in rooms:
        await sio.enter_room(sid, room)
    if staff:
        _staff_sids[sid] = staff


@sio.event
async def disconnect(sid):
    _staff_sids.pop(sid, None)


def kick(staff_id, doctor_id) -> None:
    """Disconnect a staff member's dashboards for one doctor (after their access changed)."""
    loop = _loop
    if loop is None or loop.is_closed():
        return
    key = (str(staff_id), str(doctor_id))
    for sid, who in list(_staff_sids.items()):
        if who == key:
            asyncio.run_coroutine_threadsafe(sio.disconnect(sid), loop)


def _forward(room: str, event: str, payload: dict) -> None:
    # Doctor events also go to the clinic's room, where staff listen. session/booking rooms are
    # for patient tracking pages later.
    loop = _loop  # read once: shutdown may clear it from another thread
    if loop is None or loop.is_closed() or not room.startswith("doctor:"):
        return
    rooms = [room]
    if payload.get("doctorClinicId"):
        rooms.append(f"slot:{payload['doctorClinicId']}")
    asyncio.run_coroutine_threadsafe(sio.emit(event, payload, room=rooms), loop)


def start():
    """Call on app startup. Returns a function to call on shutdown."""
    global _loop
    _loop = asyncio.get_running_loop()
    unsubscribe = events.subscribe(_forward)

    def stop():
        global _loop
        unsubscribe()
        _loop = None

    return stop
