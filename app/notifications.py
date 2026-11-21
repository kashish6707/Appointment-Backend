"""Messages to patients (WhatsApp), through an outbox.

1. A change that should tell patients something (a booking, Start OPD, a break, calling the
   next patient, ...) calls a `queue_*` function in the same transaction, so the messages exist
   if and only if the change was saved.
2. After the commit, `deliver_due()` sends them (FastAPI runs it as a background task, so the
   doctor isn't kept waiting), and a loop started with the app retries anything left over:
   failures back off 1, 2, 4, 8 minutes, then are marked FAILED.

Each message is a WhatsApp template (approved by Meta) with numbered parameters, in English or
Hindi (the language the patient booked in). `KINDS` holds the template names and the text, which
must match the templates submitted to Meta word for word (docs/whatsapp-templates.md). The same
text is kept in `body`: the log sender prints it and the patient's queue page shows it.

A message goes out only if the booking opted in to WhatsApp and has a mobile number; otherwise
it's recorded as SKIPPED. Each booking gets each message once (unique booking + dedupe key).
"""

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import joinedload

from app import security
from app.config import get_settings
from app.db import SessionLocal
from app.messaging import PermanentSendError, SendError, get_sender
from app.models import Booking, DoctorClinic, Notification, OpdSession, TrackingLink
from app.serializers import iso
from app.timeutil import IST

log = logging.getLogger("clinically")

MAX_ATTEMPTS = 5
BATCH = 50
GET_READY_MINUTES = 20  # "get ready" when a patient's turn is about this many minutes away
LATE_MINUTES = 15  # "running late" when the expected time moves later by at least this much
LATE_GAP = timedelta(minutes=30)  # and at most one such message per patient in this time
COVERED = "Covered by the OPD started message"

# What the every-minute tick (app/main.py) has to look at. It only touches the database while one
# is set, so a free database that sleeps when idle (Neon) can sleep outside OPD hours.
_opd_running = threading.Event()  # an OPD may be running: expected times move as time passes
_messages_waiting = threading.Event()  # messages may be waiting to be sent or retried
_opd_running.set()  # after a restart, look once
_messages_waiting.set()


@dataclass(frozen=True)
class Kind:
    template: str
    en: str
    hi: str


# {1}, {2}, ... are the template's parameters, in order. Keep in sync with docs/whatsapp-templates.md.
KINDS = {
    "BOOKED": Kind(
        "clinically_token_booked",
        "Hello {1}, your token number is {2} for {3} at {4} on {5}. Please come by {6}. Follow the queue live: {7} . Clinically",
        "नमस्ते {1}, {4} में {3} के लिए {5} को आपका टोकन नंबर {2} है। कृपया {6} तक आ जाएँ। लाइन लाइव देखें: {7} . Clinically",
    ),
    "OPD_STARTED": Kind(
        "clinically_opd_started",
        "Clinically: {1} has started OPD at {2}. Your token is {3}. Patients before you: {4}. Follow the queue live: {5} . Thank you",
        "Clinically: {1} ने {2} में ओपीडी शुरू कर दी है। आपका टोकन {3} है। आपसे पहले मरीज़: {4}। लाइन लाइव देखें: {5} . धन्यवाद",
    ),
    "BREAK": Kind(
        "clinically_doctor_break",
        "Clinically: {1} is on a short break of {2} minutes and will be back around {3}. Your token is {4}. Live queue: {5} . Thank you",
        "Clinically: {1} {2} मिनट के छोटे ब्रेक पर हैं और लगभग {3} तक लौटेंगे। आपका टोकन {4} है। लाइव लाइन: {5} . धन्यवाद",
    ),
    "GET_READY": Kind(
        "clinically_get_ready",
        "Clinically: Get ready, your turn for token {1} with {2} at {3} is in about {4} minutes (around {5}). Please reach the clinic. Live queue: {6} . Thank you",
        "Clinically: तैयार हो जाइए, {3} में {2} के पास टोकन {1} की आपकी बारी लगभग {4} मिनट में ({5} के आसपास) है। कृपया क्लिनिक पहुँचें। लाइव लाइन: {6} . धन्यवाद",
    ),
    "RUNNING_LATE": Kind(
        "clinically_running_late",
        "Clinically: {1} is running late. The new expected time for your token {2} is around {3}. Live queue: {4} . Sorry for the wait",
        "Clinically: {1} को देर हो रही है। आपके टोकन {2} का नया अनुमानित समय लगभग {3} है। लाइव लाइन: {4} . इंतज़ार के लिए क्षमा करें",
    ),
    "NEXT": Kind(
        "clinically_you_are_next",
        "Clinically: You're next! Token {1}, please be ready outside {2}'s room at {3}. Thank you",
        "Clinically: अगला नंबर आपका है! टोकन {1}, कृपया {3} में {2} के कमरे के बाहर तैयार रहें। धन्यवाद",
    ),
    "YOUR_TURN": Kind(
        "clinically_your_turn",
        "Clinically: It's your turn. Token {1}, please go in to see {2} now. Thank you",
        "Clinically: आपकी बारी है। टोकन {1}, कृपया अभी {2} से मिलने अंदर जाएँ। धन्यवाद",
    ),
    "CANCELLED": Kind(
        "clinically_token_cancelled",
        "Clinically: Sorry, your token {1} for {2} on {3} is cancelled because the doctor is not available. Any online payment will be refunded. Please book another day.",
        "Clinically: क्षमा करें, {3} को {2} के लिए आपका टोकन {1} रद्द हो गया है क्योंकि डॉक्टर उपलब्ध नहीं हैं। ऑनलाइन भुगतान वापस होगा। कृपया दूसरे दिन बुक करें।",
    ),
    "OPD_CLOSED": Kind(
        "clinically_opd_closed",
        "Clinically: {1} has closed today's OPD at {2} before your token {3} was called. Please contact the clinic. Sorry for the trouble.",
        "Clinically: {1} ने आपका टोकन {3} आने से पहले {2} में आज की ओपीडी बंद कर दी है। कृपया क्लिनिक से संपर्क करें। असुविधा के लिए क्षमा करें।",
    ),
}
OPD_STARTED = "OPD_STARTED"


def render(kind: str, lang: str, params: list[str]) -> str:
    text = getattr(KINDS[kind], "hi" if lang == "hi" else "en")
    for i, p in enumerate(params, start=1):
        text = text.replace(f"{{{i}}}", p)
    return text


# --- what goes into the messages ------------------------------------------------------------


def _clock(t) -> str:
    """09:30 -> "9:30 am", 17:00 -> "5 pm"."""
    h, m = t.hour, t.minute
    return f"{(h + 11) % 12 + 1}{f':{m:02d}' if m else ''} {'am' if h < 12 else 'pm'}"


def _day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"  # "Thu 20 Nov"


def _link(db: DbSession, booking: Booking, code: str | None = None) -> str:
    """The live queue link. Only a code's hash is stored, so a message gets its own new code."""
    if code is None:
        code = security.new_tracking_code()
        db.add(TrackingLink(booking_id=booking.id, code_hash=security.hash_token(code)))
    return f"{get_settings().site_url.rstrip('/')}/track/{booking.id}?code={code}"


def _row(booking: Booking, session_id, kind: str, params: list, dedupe_key: str | None = None) -> dict:
    params = [str(p) for p in params]
    phone = booking.patient.phone
    can_send = bool(phone) and booking.whatsapp_opt_in
    return {
        "id": uuid.uuid4(),
        "booking_id": booking.id,
        "session_id": session_id,
        "kind": kind,
        "dedupe_key": dedupe_key or kind,
        "phone": phone,
        "template": KINDS[kind].template,
        "params": params,
        "lang": booking.lang,
        "body": render(kind, booking.lang, params),
        "status": "PENDING" if can_send else "SKIPPED",
        "last_error": None if can_send else ("No mobile number" if not phone else "Didn't ask for WhatsApp updates"),
    }


def _insert(db: DbSession, rows: list[dict]) -> int:
    if not rows:
        return 0
    inserted = db.scalars(
        insert(Notification)
        .values(rows)
        .on_conflict_do_nothing(constraint="notifications_booking_dedupe")
        .returning(Notification.id)
    ).all()
    if inserted:
        _messages_waiting.set()
    return len(inserted)


def _slot(db: DbSession, session: OpdSession) -> DoctorClinic:
    return db.scalar(
        select(DoctorClinic)
        .options(joinedload(DoctorClinic.doctor), joinedload(DoctorClinic.clinic))
        .where(DoctorClinic.id == session.doctor_clinic_id)
    )


def _waiting(db: DbSession, session: OpdSession) -> list[Booking]:
    return list(
        db.scalars(
            select(Booking)
            .options(joinedload(Booking.patient))
            .where(Booking.session_id == session.id, Booking.status == "WAITING")
            .order_by(Booking.token)
        ).all()
    )


def _already(db: DbSession, session: OpdSession) -> set[tuple]:
    """(booking id, dedupe key) of messages this session already has."""
    return {
        (bid, key)
        for bid, key in db.execute(
            select(Notification.booking_id, Notification.dedupe_key).where(Notification.session_id == session.id)
        ).all()
    }


# --- the moments patients are told about ---------------------------------------------------


def queue_booked(db: DbSession, booking: Booking, code: str | None, patient_name: str) -> int:
    """"Your token is 12 for Dr X ..." with the live link (`code` is the booking's tracking code)."""
    from app import schedule  # avoid an import cycle

    session = booking.session
    dc = _slot(db, session)
    plan = schedule.day_plan(db, dc, session.date)
    # The hour they chose (tokens per hour), else when OPD starts.
    starts = _clock(booking.slot_start or plan.start or dc.opd_start)
    params = [patient_name, booking.token, dc.doctor.full_name, dc.clinic.name, _day(session.date), starts, _link(db, booking, code)]
    return _insert(db, [_row(booking, session.id, "BOOKED", params)])


def _at(at: datetime) -> str:
    """An expected time for a message: India time, rounded down to 5 minutes ("11:40 am")."""
    at = at.astimezone(IST)
    return _clock(at - timedelta(minutes=at.minute % 5))


def _turns(db: DbSession, session: OpdSession) -> list:
    from app import bookings  # avoid an import cycle

    return bookings.turn_times(db, session)


def queue_opd_started(db: DbSession, session: OpdSession) -> int:
    """"OPD has started" for every waiting patient. Returns how many were queued.

    The patient at the front, and anyone whose turn is already within GET_READY_MINUTES, is told
    that here: they don't also get "you're next" / "get ready" straight after (recorded as
    SKIPPED). Safe to call twice. Each patient's expected time now is remembered, so a later
    big delay can be told.
    """
    dc = _slot(db, session)
    done = _already(db, session)
    rows = []
    for ahead, (b, minutes, at) in enumerate(_turns(db, session)):
        b.expected_told_at = at
        if (b.id, OPD_STARTED) in done:
            continue
        rows.append(_row(b, session.id, OPD_STARTED, [dc.doctor.full_name, dc.clinic.name, b.token, ahead, _link(db, b)]))
        if ahead == 0 or minutes <= GET_READY_MINUTES:
            kind = "NEXT" if ahead == 0 else "GET_READY"
            covered = _row(b, session.id, kind, [b.token])
            rows.append({**covered, "params": [], "body": COVERED, "status": "SKIPPED", "last_error": COVERED})
    _insert(db, rows)
    _opd_running.set()
    return sum(1 for r in rows if r["kind"] == OPD_STARTED)


def queue_progress(db: DbSession, session: OpdSession, now: datetime | None = None) -> int:
    """After the queue moved, or time passed while OPD runs. Each patient gets, once each:

    - "you're next" at the front of the queue,
    - "get ready" when their turn is about GET_READY_MINUTES away,
    - "it's your turn" when called in,
    - "running late" when their expected time has moved LATE_MINUTES later than they were told
      (at most once per LATE_GAP). Earlier is never sent: "get ready" covers it.
    """
    if session.status != "IN_OPD":
        return 0
    from app.timeutil import now_ist

    now = now or now_ist()
    dc = _slot(db, session)
    done = _already(db, session)
    rows = []
    for ahead, (b, minutes, at) in enumerate(_turns(db, session)):
        if ahead == 0:
            if (b.id, "NEXT") not in done:
                rows.append(_row(b, session.id, "NEXT", [b.token, dc.doctor.full_name, dc.clinic.name]))
        elif minutes <= GET_READY_MINUTES:
            if (b.id, "GET_READY") not in done:
                rows.append(_row(b, session.id, "GET_READY", [b.token, dc.doctor.full_name, dc.clinic.name, max(minutes, 2), _at(at), _link(db, b)]))
                b.expected_told_at = at
        elif b.expected_told_at is None:
            b.expected_told_at = at  # booked after OPD started: nothing told yet beyond "come by"
        elif at - b.expected_told_at >= timedelta(minutes=LATE_MINUTES) and (b.delay_told_at is None or now - b.delay_told_at >= LATE_GAP):
            key = f"LATE:{now.astimezone(timezone.utc):%Y%m%d%H%M%S%f}"
            rows.append(_row(b, session.id, "RUNNING_LATE", [dc.doctor.full_name, b.token, _at(at), _link(db, b)], key))
            b.expected_told_at, b.delay_told_at = at, now
    called = db.scalars(
        select(Booking).options(joinedload(Booking.patient)).where(Booking.session_id == session.id, Booking.status == "WITH_DOCTOR")
    ).all()
    for b in called:
        if (b.id, "YOUR_TURN") not in done:
            rows.append(_row(b, session.id, "YOUR_TURN", [b.token, dc.doctor.full_name]))
    _opd_running.set()
    return _insert(db, rows)


def queue_break(db: DbSession, session: OpdSession) -> int:
    """"Doctor is on a break of N minutes" for every waiting patient, once per break."""
    if session.break_until is None or session.break_started_at is None:
        return 0
    dc = _slot(db, session)
    minutes = round((session.break_until - session.break_started_at).total_seconds() / 60)
    back = _clock(session.break_until.astimezone(IST))
    key = f"BREAK:{session.break_started_at.astimezone(timezone.utc):%Y%m%d%H%M%S%f}"
    rows = [
        _row(b, session.id, "BREAK", [dc.doctor.full_name, minutes, back, b.token, _link(db, b)], key)
        for b in _waiting(db, session)
    ]
    # The break message tells them it'll be later: that's the time they now expect.
    for b, _, at in _turns(db, session):
        b.expected_told_at = at
    return _insert(db, rows)


def queue_closed(db: DbSession, session: OpdSession) -> int:
    """OPD ended with patients still waiting."""
    dc = _slot(db, session)
    rows = [_row(b, session.id, "OPD_CLOSED", [dc.doctor.full_name, dc.clinic.name, b.token]) for b in _waiting(db, session)]
    return _insert(db, rows)


def queue_cancelled(db: DbSession, bookings: list[Booking]) -> int:
    """The clinic cancelled these bookings (the doctor isn't available that day)."""
    rows = []
    for b in bookings:
        dc = _slot(db, b.session)
        rows.append(_row(b, b.session_id, "CANCELLED", [b.token, dc.doctor.full_name, _day(b.session.date)]))
    return _insert(db, rows)


# --- every minute -----------------------------------------------------------------------


def tick() -> None:
    """Run every minute by the app: while an OPD runs, send "get ready" / "running late" as time
    passes (not only when the queue moves); and send or retry messages left over."""
    from app.timeutil import today_ist

    if _opd_running.is_set():
        with SessionLocal() as db:
            sessions = db.scalars(
                select(OpdSession)
                .where(OpdSession.date == today_ist(), OpdSession.status.in_(("IN_OPD", "ON_BREAK")))
                .with_for_update(skip_locked=True)  # a doctor's request holds it: they'll do it
            ).all()
            if not sessions:
                _opd_running.clear()
            for session in sessions:
                queue_progress(db, session)
            db.commit()
    if _messages_waiting.is_set():
        deliver_due()
        with SessionLocal() as db:
            if not db.scalar(select(func.count()).select_from(Notification).where(Notification.status == "PENDING")):
                _messages_waiting.clear()


# --- sending ----------------------------------------------------------------------------


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
                n.provider_id = sender.send(n)
            except PermanentSendError as e:  # e.g. the number isn't on WhatsApp: retrying won't help
                n.last_error = str(e)[:500]
                n.status = "FAILED"
                continue
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
    """Messages about one booking, for the patient's own queue page (not the placeholders
    for messages that "OPD has started" already covered)."""
    rows = db.scalars(
        select(Notification)
        .where(Notification.booking_id == booking_id, Notification.body != COVERED)
        .order_by(Notification.created_at)
    ).all()
    return [
        {"kind": n.kind, "text": n.body, "status": n.status, "createdAt": iso(n.created_at), "sentAt": iso(n.sent_at)}
        for n in rows
    ]
