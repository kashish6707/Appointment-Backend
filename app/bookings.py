"""Booking rules shared by the public booking route and doctor walk-ins."""

import hmac
import uuid
from datetime import date, datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import joinedload

from app import schedule, security
from app.models import WEEKDAYS, Booking, Doctor, DoctorClinic, OpdSession, Patient, TrackingLink
from app.timeutil import IST, now_ist

MAX_DAYS_AHEAD = 7
ACTIVE_STATUSES = ("WAITING", "WITH_DOCTOR")
DEFAULT_CONSULT_MINUTES = 10
MIN_CONSULT_MINUTES = 3
BOOKING_NOT_FOUND = "We couldn't find that booking. Check the link and try again."


def get_bookable_slot(db: DbSession, doctor_clinic_id: uuid.UUID) -> DoctorClinic:
    dc = db.scalar(
        select(DoctorClinic)
        .join(Doctor)
        .options(joinedload(DoctorClinic.doctor), joinedload(DoctorClinic.clinic))
        .where(DoctorClinic.id == doctor_clinic_id, Doctor.is_verified.is_(True))
    )
    if dc is None:
        raise HTTPException(404, "We couldn't find that doctor. They may no longer take bookings here.")
    return dc


NOT_AVAILABLE_MSG = "The doctor doesn't hold OPD at this clinic on that day. Choose another day."
FULL_MSG = "All tokens for that day are booked. Choose another day."


class LimitReached(HTTPException):
    """The day's patient limit is reached. The body also carries code/limit/booked, so the desk
    can ask "add anyway?" for a walk-in."""

    def __init__(self, message: str, limit: int, booked: int):
        super().__init__(409, message)
        self.extra = {"code": "LIMIT_REACHED", "limit": limit, "booked": booked}


def check_bookable_date(db: DbSession, dc: DoctorClinic, day: date, now: datetime | None = None) -> None:
    now = now or now_ist()
    today = now.date()
    if day < today or day > today + timedelta(days=MAX_DAYS_AHEAD):
        raise HTTPException(400, f"You can book for today or up to {MAX_DAYS_AHEAD} days ahead.")
    plan = schedule.day_plan(db, dc, day)
    if not plan.open:
        raise HTTPException(400, NOT_AVAILABLE_MSG)
    if day == today and now.time() >= plan.end:
        raise HTTPException(400, "Today's OPD hours are over. Choose another day.")


def lock_session(db: DbSession, doctor_clinic_id: uuid.UUID, day: date) -> OpdSession:
    """Get (creating if needed) the day's session and lock its row until commit.

    Every booking for a session goes through this lock, so token numbers are handed out one
    at a time and can never clash. The unique (session_id, token) constraint is the backstop.
    """
    db.execute(
        insert(OpdSession)
        .values(id=uuid.uuid4(), doctor_clinic_id=doctor_clinic_id, date=day)
        .on_conflict_do_nothing(constraint="opd_sessions_doctor_clinic_date")
    )
    return db.scalar(
        select(OpdSession)
        .where(OpdSession.doctor_clinic_id == doctor_clinic_id, OpdSession.date == day)
        .with_for_update()
    )


def find_or_create_patient(
    db: DbSession,
    *,
    name: str,
    phone: str | None,
    age: int,
    gender: str,
    height_cm: float | None = None,
    weight_kg: float | None = None,
    records_consent: bool | None = None,
) -> Patient:
    """Same phone + same name (ignoring case) is the same patient. No phone: always a new one.

    records_consent: True grants consent (keeping the original time), False withdraws it,
    None (walk-ins) leaves it unchanged.
    """
    patient = None
    if phone:
        patient = db.scalar(
            select(Patient)
            # Never a profile in a patient account: a guest who types the same name and number
            # mustn't add to someone's history or change their consent.
            .where(Patient.phone == phone, func.lower(Patient.name) == name.lower(), Patient.account_id.is_(None))
            .order_by(Patient.created_at)
            .limit(1)
            .with_for_update()
        )
    if patient is None:
        patient = Patient(name=name, phone=phone)
        db.add(patient)
    patient.age = age
    patient.gender = gender
    if height_cm is not None:
        patient.height_cm = height_cm
    if weight_kg is not None:
        patient.weight_kg = weight_kg
    if records_consent is True and patient.records_consent_at is None:
        patient.records_consent_at = now_ist()
    elif records_consent is False:
        patient.records_consent_at = None
    db.flush()
    return patient


CLOSED_MSG = "This OPD session is closed. Choose another day."


def active_token(db: DbSession, session_id: uuid.UUID, patient_id: uuid.UUID) -> int | None:
    return db.scalar(
        select(Booking.token).where(
            Booking.session_id == session_id,
            Booking.patient_id == patient_id,
            Booking.status.in_(ACTIVE_STATUSES),
        )
    )


def add_booking(
    db: DbSession,
    session: OpdSession,
    patient: Patient,
    *,
    visit_type: str,
    source: str,
    booking_id: uuid.UUID | None = None,
    tracking_code_hash: str | None = None,
    allow_over_limit: bool = False,
    slot_start: time | None = None,
    choose_hour: bool = False,
) -> tuple[Booking, str | None]:
    """Give the patient the next token. The caller must hold the session lock (lock_session).

    When the clinic limits tokens per hour, the token comes from an hour's numbers: the hour the
    patient chose (choose_hour: online booking and moving a token, which must send slot_start),
    else the first hour from now with room (walk-ins, paid bookings). If every hour is full, a
    walk-in goes after the last hour.

    Returns the booking and a new tracking code, or None for the code when the caller passed
    tracking_code_hash (the patient already has their code from the payment step).
    """
    if session.status == "CLOSED":
        raise HTTPException(409, CLOSED_MSG)
    plan = schedule.day_plan(db, session.doctor_clinic, session.date)
    if not plan.open:
        raise HTTPException(409, "The doctor isn't available on this day.")
    already = active_token(db, session.id, patient.id)
    if already is not None:
        raise HTTPException(409, f"{patient.name} already has token {already} for this OPD session.")
    if plan.limit is not None and not allow_over_limit:
        booked = schedule.booked_count(db, session.id)
        if booked >= plan.limit:
            raise LimitReached(
                FULL_MSG if source == "ONLINE" else f"The limit of {plan.limit} patients for this day is reached.",
                plan.limit,
                booked,
            )

    code = None
    if tracking_code_hash is None:
        code = security.new_tracking_code()
        tracking_code_hash = security.hash_token(code)
    token, hour = _token_for(db, session, plan, slot_start, choose_hour)
    booking = Booking(
        id=booking_id or uuid.uuid4(),
        session_id=session.id,
        token=token,
        slot_start=hour,
        patient=patient,
        visit_type=visit_type,
        source=source,
        status="WAITING",
        tracking_code_hash=tracking_code_hash,
    )
    db.add(booking)
    db.flush()
    return booking, code


def _token_for(db: DbSession, session: OpdSession, plan, slot_start: time | None, choose_hour: bool) -> tuple[int, time | None]:
    """The token number (and chosen hour) for a new booking in this session."""
    hours = schedule.hour_slots(plan, session.doctor_clinic.tokens_per_hour)
    if not hours:
        last = db.scalar(select(func.max(Booking.token)).where(Booking.session_id == session.id))
        return (last or 0) + 1, None
    taken = set(db.scalars(select(Booking.token).where(Booking.session_id == session.id, Booking.status != "CANCELLED")))

    # Switched on after patients booked today: the hours' numbers start after theirs, so nobody
    # new jumps ahead of them. Fixed once per session (an overflow walk-in later doesn't move it).
    if session.hour_token_base is None:
        has_hours = db.scalar(
            select(func.count()).select_from(Booking).where(Booking.session_id == session.id, Booking.slot_start.is_not(None))
        )
        last = db.scalar(select(func.max(Booking.token)).where(Booking.session_id == session.id))
        session.hour_token_base = 0 if has_hours else (last or 0)
    base = session.hour_token_base

    def numbers(h: schedule.HourSlot) -> range:
        return range(base + h.first_token, base + h.first_token + h.capacity)

    after_last = max(taken | {numbers(h)[-1] for h in hours} | {0}) + 1

    in_hour = dict(
        db.execute(
            select(Booking.slot_start, func.count(Booking.id))
            .where(Booking.session_id == session.id, Booking.status != "CANCELLED", Booking.slot_start.is_not(None))
            .group_by(Booking.slot_start)
        ).all()
    )

    def free_number(h: schedule.HourSlot) -> int | None:
        if in_hour.get(h.start, 0) >= h.capacity:
            return None
        return next((n for n in numbers(h) if n not in taken), None)

    now = now_ist()
    is_today = session.date == now.date()
    if choose_hour:
        if slot_start is None:
            raise HTTPException(400, "Choose a time for your visit.")
        hour = next((h for h in hours if h.start == slot_start), None)
        if hour is None:
            raise HTTPException(400, "Choose one of the times shown.")
        if is_today and now.time() >= hour.end:
            raise HTTPException(409, "That time is over. Choose a later time.")
        n = free_number(hour)
        if n is None:
            raise HTTPException(409, "That time is full. Choose another time.")
        return n, hour.start

    for h in hours:
        if is_today and now.time() >= h.end:
            continue
        n = free_number(h)
        if n is not None:
            return n, h.start
    return after_last, None


def _whatsapp_state(db: DbSession, booking: Booking) -> str:
    """Whether the patient gets updates on WhatsApp: ON, OFF (they didn't ask, or gave no number)
    or FAILED (messages to their number failed and none got through, e.g. it isn't on WhatsApp)."""
    from app.models import Notification  # local: keeps the module's imports small

    if not booking.whatsapp_opt_in or not booking.patient.phone:
        return "OFF"
    rows = dict(
        db.execute(
            select(Notification.status, func.count(Notification.id))
            .where(Notification.booking_id == booking.id, Notification.status.in_(("SENT", "FAILED")))
            .group_by(Notification.status)
        ).all()
    )
    return "FAILED" if rows.get("FAILED") and not rows.get("SENT") else "ON"


def _slot_end(plan, dc: DoctorClinic, start: time | None) -> time | None:
    if start is None:
        return None
    hour = next((h for h in schedule.hour_slots(plan, dc.tokens_per_hour) if h.start == start), None)
    return hour.end if hour else (datetime.combine(plan.date, start) + timedelta(hours=1)).time()


def precheck_online_booking(db: DbSession, dc: DoctorClinic, day: date, name: str, phone: str) -> None:
    """Refuse before taking money if the booking would fail anyway (closed day, duplicate)."""
    session = db.scalar(
        select(OpdSession).where(OpdSession.doctor_clinic_id == dc.id, OpdSession.date == day)
    )
    if session is None:
        return
    if session.status == "CLOSED":
        raise HTTPException(409, CLOSED_MSG)
    plan = schedule.day_plan(db, dc, day)
    if plan.limit is not None and schedule.booked_count(db, session.id) >= plan.limit:
        raise HTTPException(409, FULL_MSG)
    patient = db.scalar(
        select(Patient)
        .where(Patient.phone == phone, func.lower(Patient.name) == name.lower())
        .order_by(Patient.created_at)
        .limit(1)
    )
    if patient is not None:
        already = active_token(db, session.id, patient.id)
        if already is not None:
            raise HTTPException(409, f"{patient.name} already has token {already} for this OPD session.")


def get_booking_by_code(db: DbSession, booking_id: uuid.UUID, code: str, *, lock: bool = False) -> Booking:
    """The booking, if `code` is its tracking code. Unknown id and wrong code look the same."""
    query = select(Booking).where(Booking.id == booking_id)
    if lock:
        query = query.with_for_update()
    booking = db.scalar(query)
    if booking is None:
        raise HTTPException(404, BOOKING_NOT_FOUND)
    code_hash = security.hash_token(code)
    if not hmac.compare_digest(booking.tracking_code_hash, code_hash) and not db.scalar(
        select(TrackingLink.id).where(TrackingLink.booking_id == booking.id, TrackingLink.code_hash == code_hash)
    ):
        raise HTTPException(404, BOOKING_NOT_FOUND)
    return booking


def new_tracking_link(db: DbSession, booking: Booking) -> str:
    """Another code for this booking (for a signed-in patient's device). Returns the code."""
    code = security.new_tracking_code()
    db.add(TrackingLink(booking_id=booking.id, code_hash=security.hash_token(code)))
    return code


def average_consult_minutes(db: DbSession, session_id: uuid.UUID) -> float:
    seconds = db.scalar(
        select(func.avg(func.extract("epoch", Booking.completed_at - Booking.called_at))).where(
            Booking.session_id == session_id,
            Booking.called_at.is_not(None),
            Booking.completed_at.is_not(None),
            Booking.status.in_(("DONE", "SENT_FOR_TESTS")),
        )
    )
    # At least a few minutes: one patient marked done straight after being called (a mistake,
    # or a quick query) mustn't make everyone's wait look like nothing.
    return max(float(seconds) / 60, MIN_CONSULT_MINUTES) if seconds else DEFAULT_CONSULT_MINUTES


def _clock(at: datetime) -> str:
    """A time to arrive by, rounded down to 5 minutes, as HH:MM in India."""
    at = at.astimezone(IST)
    return (at - timedelta(minutes=at.minute % 5)).strftime("%H:%M")


def usual_minutes(db: DbSession, session: OpdSession) -> float:
    """Minutes per patient when one isn't set on the booking: the clinic's usual time, else
    (before OPD) the day's hours spread over its limit, else the average so far today."""
    dc = session.doctor_clinic
    if dc.minutes_per_patient:
        return float(dc.minutes_per_patient)
    average = average_consult_minutes(db, session.id)
    if session.status == "NOT_STARTED":
        return schedule.minutes_per_patient(schedule.day_plan(db, dc, session.date)) or average
    return average


def turn_times(db: DbSession, session: OpdSession, now: datetime | None = None) -> list[tuple[Booking, int, datetime]]:
    """Every waiting patient, in queue order, with the minutes until their turn and that time.

    Each patient before them counts for their own time with the doctor (consult_minutes) or the
    usual time, and whoever is with the doctor for what's left of theirs.

    - Not started: from the day's start (or now, if the doctor is late).
    - In OPD: from now. On a break: after the time left on the break.
    - Closed: nobody.
    """
    now = now or now_ist()
    if session.status == "CLOSED":
        return []
    usual = usual_minutes(db, session)
    rows = db.scalars(
        select(Booking).where(Booking.session_id == session.id, Booking.status.in_(ACTIVE_STATUSES)).order_by(Booking.token)
    ).all()
    if session.status == "NOT_STARTED":
        plan = schedule.day_plan(db, session.doctor_clinic, session.date)
        if not plan.open or plan.start is None:
            return []
        at = max(datetime.combine(session.date, plan.start, tzinfo=IST), now)
    else:
        at = now
        if session.status == "ON_BREAK" and session.break_until is not None:
            at = max(session.break_until, now)
        # In OPD, whoever is with the doctor still needs what's left of their time. (A doctor
        # on a break has finished with them, even if they weren't marked done yet.)
        for b in rows if session.status == "IN_OPD" else []:
            if b.status == "WITH_DOCTOR":
                spent = (now - b.called_at).total_seconds() / 60 if b.called_at else 0
                at += timedelta(minutes=max((b.consult_minutes or usual) - spent, 0))
    result = []
    for b in rows:
        if b.status != "WAITING":
            continue
        result.append((b, max(round((at - now).total_seconds() / 60), 0), at))
        at += timedelta(minutes=b.consult_minutes or usual)
    return result


def wait_estimates(db: DbSession, session: OpdSession, patients_ahead: list[int], now: datetime | None = None) -> list[tuple[int, str] | None]:
    """For each waiting patient (given how many wait before them): minutes until their turn
    and roughly what time that is (HH:MM, rounded down to 5 minutes)."""
    times = turn_times(db, session, now)
    return [(times[a][1], _clock(times[a][2])) if 0 <= a < len(times) else None for a in patients_ahead]


def queue_status(db: DbSession, booking: Booking) -> dict:
    """What a patient's tracking page shows."""
    session = booking.session
    dc = session.doctor_clinic
    ahead = 0
    estimate = None
    expected_at = None
    plan = schedule.day_plan(db, dc, session.date)
    if booking.status == "WAITING" and session.status != "CLOSED":
        ahead = db.scalar(
            select(func.count()).where(
                Booking.session_id == session.id, Booking.status == "WAITING", Booking.token < booking.token
            )
        )
        # Moves with breaks: the time left on a break is added before anyone's turn.
        found = wait_estimates(db, session, [ahead])[0]
        if found:
            estimate, expected_at = found
        # Not before the hour they chose (patients ahead may have booked earlier hours).
        chosen = schedule.hhmm(booking.slot_start)
        if chosen and expected_at and expected_at < chosen:
            expected_at = chosen
    # Who is with the doctor right now (not just the last token called, which stays after
    # that patient is marked seen).
    with_doctor = db.scalar(
        select(Booking.token).where(Booking.session_id == session.id, Booking.status == "WITH_DOCTOR").order_by(Booking.token).limit(1)
    )
    return {
        "bookingId": str(booking.id),
        "token": booking.token,
        "status": booking.status,
        "nowServing": session.current_token,
        "withDoctor": with_doctor,
        "patientsAhead": ahead,
        "estimatedMinutes": estimate,
        "sessionStatus": session.status,
        "sessionStartedAt": session.started_at.isoformat() if session.started_at else None,
        "breakUntil": session.break_until.isoformat() if session.break_until else None,
        "sessionId": str(session.id),
        "doctorClinicId": str(dc.id),
        "date": session.date.isoformat(),
        "doctorName": dc.doctor.full_name,
        "clinicName": dc.clinic.name,
        "opdStart": schedule.hhmm(plan.start) or dc.opd_start.strftime("%H:%M"),
        "opdEnd": schedule.hhmm(plan.end) or dc.opd_end.strftime("%H:%M"),
        # The hour the patient chose, when the clinic limits tokens per hour.
        "whatsapp": _whatsapp_state(db, booking),
        "slotStart": schedule.hhmm(booking.slot_start),
        "slotEnd": schedule.hhmm(_slot_end(plan, dc, booking.slot_start)),
        "expectedAt": expected_at,
        "dayNote": plan.note,
    }
