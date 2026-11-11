"""Messages to patients, through an outbox.

1. A change that should tell patients something (e.g. Start OPD) calls a `queue_*` function in
   the same transaction, so the messages exist if and only if the change was saved.
2. After the commit, `deliver_due()` sends them (FastAPI runs it as a background task, so the
   doctor isn't kept waiting), and a loop started with the app retries anything left over:
   failures back off 1, 2, 4, 8 minutes, then are marked FAILED.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.messaging import SendError, get_sender
from app.models import Booking, DoctorClinic, Notification, OpdSession
from app.serializers import iso

log = logging.getLogger("clinically")

OPD_STARTED = "OPD_STARTED"
MAX_ATTEMPTS = 5
BATCH = 50


def _opd_started_text(doctor: str, clinic: str, token: int, now_serving: int | None, ahead: int) -> str:
    serving = f" Now serving token {now_serving}." if now_serving else ""
    if ahead == 0:
        turn = " You're next. Please reach the clinic now."
    else:
        turn = f" {ahead} patient{'s' if ahead != 1 else ''} before you."
    return f"Clinically: {doctor} has started OPD at {clinic}. Your token is {token}.{serving}{turn}"


def queue_opd_started(db: DbSession, session: OpdSession) -> int:
    """Queue "OPD has started" for every patient waiting in this session. Returns how many.

    A booking is only ever told once (unique booking + kind), so this is safe to call twice.
    """
    dc = db.scalar(
        select(DoctorClinic)
        .options(joinedload(DoctorClinic.doctor), joinedload(DoctorClinic.clinic))
        .where(DoctorClinic.id == session.doctor_clinic_id)
    )
    waiting = db.scalars(
        select(Booking)
        .options(joinedload(Booking.patient))
        .where(Booking.session_id == session.id, Booking.status == "WAITING")
        .order_by(Booking.token)
    ).all()
    rows = []
    for ahead, b in enumerate(waiting):
        phone = b.patient.phone
        rows.append(
            {
                "id": uuid.uuid4(),
                "booking_id": b.id,
                "session_id": session.id,
                "kind": OPD_STARTED,
                "phone": phone,
                "body": _opd_started_text(dc.doctor.full_name, dc.clinic.name, b.token, session.current_token, ahead),
                # A walk-in without a mobile number can't be messaged.
                "status": "PENDING" if phone else "SKIPPED",
            }
        )
    if not rows:
        return 0
    inserted = db.scalars(
        insert(Notification)
        .values(rows)
        .on_conflict_do_nothing(constraint="notifications_booking_kind")
        .returning(Notification.id)
    ).all()
    return len(inserted)


def deliver_due(limit: int = BATCH) -> int:
    """Send pending messages whose time has come. Safe to run from several places at once:
    each message is locked while it's being sent. Returns how many were sent."""
    sender = get_sender()
    sent = 0
    with SessionLocal() as db:
        due = db.scalars(
            select(Notification)
            .where(Notification.status == "PENDING", Notification.next_attempt_at <= func.now())
            .order_by(Notification.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for n in due:
            n.attempts += 1
            n.channel = sender.name
            try:
                n.provider_id = sender.send(n.phone, n.body)
            except SendError as e:
                n.last_error = str(e)[:500]
                if n.attempts >= MAX_ATTEMPTS:
                    n.status = "FAILED"
                else:
                    n.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=2 ** (n.attempts - 1))
                continue
            except Exception as e:  # never let one bad message stop the rest
                log.exception("Sending notification %s failed", n.id)
                n.last_error = f"Unexpected error: {e}"[:500]
                n.status = "FAILED"
                continue
            n.status = "SENT"
            n.sent_at = datetime.now(timezone.utc)
            n.last_error = None
            sent += 1
        db.commit()
    return sent


def session_summary(db: DbSession, session_id) -> dict:
    """How the "OPD has started" messages for a session went, for the doctor's dashboard."""
    counts = dict(
        db.execute(
            select(Notification.status, func.count())
            .where(Notification.session_id == session_id, Notification.kind == OPD_STARTED)
            .group_by(Notification.status)
        ).all()
    )
    return {s.lower(): counts.get(s, 0) for s in ("PENDING", "SENT", "FAILED", "SKIPPED")}


def for_booking(db: DbSession, booking_id) -> list[dict]:
    """Messages about one booking, for the patient's own booking page."""
    rows = db.scalars(
        select(Notification).where(Notification.booking_id == booking_id).order_by(Notification.created_at)
    ).all()
    return [
        {"kind": n.kind, "text": n.body, "status": n.status, "createdAt": iso(n.created_at), "sentAt": iso(n.sent_at)}
        for n in rows
    ]
