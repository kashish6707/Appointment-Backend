"""Live dashboard updates over Socket.IO at /api/socket.io.

The path is under /api so the Next.js server forwards it like every other API call, and the
browser's session cookie comes along. A connection is accepted only with a valid session:
- a doctor joins "doctor:<doctorId>" and gets every event for their clinics;
- a staff member joins "slot:<doctorClinicId>" for each clinic their grant covers, so they only
  get events for those clinics. The doctor can disconnect them (kick) after changing or removing
  their access; the dashboard then reloads and picks up the new access.
Events: booking.created, booking.updated, booking.cancelled and session.updated (app/notify.py).

A patient's tracking page connects without a cookie, sending {bookingId, code} (its tracking
link) as the Socket.IO auth. It joins only "booking:<id>" and "session:<sessionId>" for that
booking, which carry token numbers and the queue's status, never other patients' details.

Anyone browsing clinics can connect with {watch: [doctorClinicId, ...]} (no sign-in) to join
"live:<doctorClinicId>" rooms: "live.updated" with today's OPD status, the token with the doctor
and how many are waiting. Nothing about any patient.

Routes call events.emit() from worker threads (they are sync functions), so emits are handed to
the server's event loop. Rooms live in this process's memory: run a single worker, or add a
Socket.IO message queue (e.g. Redis) before running several.
"""

import asyncio
import secrets
import time
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie

import socketio
from sqlalchemy import select

import uuid

from fastapi import HTTPException

from app import bookings, events, security
from app.config import get_settings
from app.db import SessionLocal
from app.models import Session, StaffMembership

# FRONTEND_ORIGIN may list several sites, comma-separated (e.g. the live site and a preview).
sio = socketio.AsyncServer(
    async_mode="asgi", cors_allowed_origins=[o.strip() for o in get_settings().frontend_origin.split(",") if o.strip()]
)
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
    return _rooms_for_session(security.hash_token(morsel.value))


# Tickets let the dashboard connect straight to this server (not through the website's proxy,
# e.g. when the website's host can't forward WebSockets), where its cookie isn't sent. The
# dashboard asks for one through the proxy (GET /api/auth/realtime-ticket, cookie checked) and
# sends it as the Socket.IO auth. One use, valid for a minute, kept in this process's memory.
TICKET_SECONDS = 60
_tickets: dict[str, tuple[str, float]] = {}  # ticket -> (session token hash, expires at)


def new_ticket(session_token_hash: str) -> str:
    now = time.monotonic()
    for t, (_, exp) in list(_tickets.items()):
        if exp <= now:
            _tickets.pop(t, None)
    ticket = secrets.token_urlsafe(24)
    _tickets[ticket] = (session_token_hash, now + TICKET_SECONDS)
    return ticket


def _use_ticket(ticket: object) -> str | None:
    if not isinstance(ticket, str):
        return None
    found = _tickets.pop(ticket, None)
    return found[0] if found and found[1] > time.monotonic() else None


def _rooms_for_session(token_hash: str) -> tuple[list[str], tuple[str, str] | None] | None:
    with SessionLocal() as db:
        session = db.scalar(select(Session).where(Session.token_hash == token_hash))
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


def _patient_rooms(auth: object) -> list[str] | None:
    """The rooms a tracking link may join: its booking and that booking's OPD session."""
    if not isinstance(auth, dict):
        return None
    booking_id, code = auth.get("bookingId"), auth.get("code")
    if not isinstance(booking_id, str) or not isinstance(code, str) or not code or len(code) > 100:
        return None
    try:
        bid = uuid.UUID(booking_id)
    except ValueError:
        return None
    with SessionLocal() as db:
        try:
            booking = bookings.get_booking_by_code(db, bid, code)
        except HTTPException:
            return None
        return [f"booking:{booking.id}", f"session:{booking.session_id}"]


MAX_WATCHED = 60


def _watch_rooms(auth: dict) -> list[str] | None:
    """Patients browsing clinics: the public live rooms of the doctor-clinic slots on screen."""
    ids = auth.get("watch")
    if not isinstance(ids, list) or len(ids) > MAX_WATCHED:
        return None
    try:
        return [f"live:{uuid.UUID(str(i))}" for i in ids]
    except ValueError:
        return None


@sio.event
async def connect(sid, environ, auth=None):
    if isinstance(auth, dict) and "watch" in auth:
        rooms = _watch_rooms(auth)
        if rooms is None:
            raise socketio.exceptions.ConnectionRefusedError("Send up to 60 clinic ids to watch.")
        for room in rooms:
            await sio.enter_room(sid, room)
        return
    if isinstance(auth, dict) and "bookingId" in auth:
        rooms = await asyncio.to_thread(_patient_rooms, auth)
        if rooms is None:
            raise socketio.exceptions.ConnectionRefusedError("We couldn't find that booking.")
        for room in rooms:
            await sio.enter_room(sid, room)
        return
    if isinstance(auth, dict) and "ticket" in auth:
        token_hash = _use_ticket(auth["ticket"])
        found = await asyncio.to_thread(_rooms_for_session, token_hash) if token_hash else None
    else:
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
    # for patients' tracking pages.
    loop = _loop  # read once: shutdown may clear it from another thread
    if loop is None or loop.is_closed() or not room.startswith(("doctor:", "session:", "booking:", "live:")):
        return
    rooms = [room]
    if room.startswith("doctor:") and payload.get("doctorClinicId"):
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
