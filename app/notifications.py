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
GET_READY_AT = 2  # "get ready" once this many (or fewer) patients are before you
COVERED = "Covered by the OPD started message"


@dataclass(frozen=True)
class Kind:
    template: str
    en: str
    hi: str


# {1}, {2}, ... are the template's parameters, in order. Keep in sync with docs/whatsapp-templates.md.
KINDS = {
    "BOOKED": Kind(
        "clinically_token_booked",
        "Hello {1}, your token number is {2} for {3} at {4} on {5}. OPD starts around {6}. Follow the queue live: {7} . Clinically",
        "नमस्ते {1}, {4} में {3} के लिए {5} को आपका टोकन नंबर {2} है। ओपीडी लगभग {6} पर शुरू होगी। लाइन लाइव देखें: {7} . Clinically",
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
        "Clinically: Get ready, only {1} patients are before your token {2} for {3} at {4}. Please start coming to the clinic. Live queue: {5} . Thank you",
        "Clinically: तैयार हो जाइए, {4} में {3} के लिए आपके टोकन {2} से पहले सिर्फ़ {1} मरीज़ हैं। कृपया क्लिनिक आना शुरू करें। लाइव लाइन: {5} . धन्यवाद",
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
    starts = _clock(plan.start or dc.opd_start)
    params = [patient_name, booking.token, dc.doctor.full_name, dc.clinic.name, _day(session.date), starts, _link(db, booking, code)]
    return _insert(db, [_row(booking, session.id, "BOOKED", params)])


def _progress_params(db: DbSession, dc: DoctorClinic, b: Booking, ahead: int) -> list:
    if ahead == 0:
        return [b.token, dc.doctor.full_name, dc.clinic.name]
    return [ahead, b.token, dc.doctor.full_name, dc.clinic.name, _link(db, b)]


def queue_opd_started(db: DbSession, session: OpdSession) -> int:
    """"OPD has started" for every waiting patient. Returns how many were queued.

    Patients already within GET_READY_AT of the front are told that here, so they don't also
    get "get ready" / "you're next" straight after (recorded as SKIPPED). Safe to call twice.
    """
    dc = _slot(db, session)
    done = _already(db, session)
    rows = []
    for ahead, b in enumerate(_waiting(db, session)):
        if (b.id, OPD_STARTED) in done:
            continue
        rows.append(_row(b, session.id, OPD_STARTED, [dc.doctor.full_name, dc.clinic.name, b.token, ahead, _link(db, b)]))
        if ahead <= GET_READY_AT:
            kind = "NEXT" if ahead == 0 else "GET_READY"
            covered = _row(b, session.id, kind, [b.token] if ahead == 0 else [ahead, b.token])
            rows.append({**covered, "params": [], "body": COVERED, "status": "SKIPPED", "last_error": COVERED})
    _insert(db, rows)
    return sum(1 for r in rows if r["kind"] == OPD_STARTED)


def queue_progress(db: DbSession, session: OpdSession) -> int:
    """After the queue moved: "get ready" (2 or fewer before you), "you're next", and
    "it's your turn" for whoever was just called in. Each booking gets each one once."""
    if session.status != "IN_OPD":
        return 0
    dc = _slot(db, session)
    done = _already(db, session)
    rows = []
    for ahead, b in enumerate(_waiting(db, session)[: GET_READY_AT + 1]):
        kind = "NEXT" if ahead == 0 else "GET_READY"
        if (b.id, kind) not in done:  # don't make a tracking link for a message that exists
            rows.append(_row(b, session.id, kind, _progress_params(db, dc, b, ahead)))
    called = db.scalars(
        select(Booking).options(joinedload(Booking.patient)).where(Booking.session_id == session.id, Booking.status == "WITH_DOCTOR")
    ).all()
    for b in called:
        if (b.id, "YOUR_TURN") not in done:
            rows.append(_row(b, session.id, "YOUR_TURN", [b.token, dc.doctor.full_name]))
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
