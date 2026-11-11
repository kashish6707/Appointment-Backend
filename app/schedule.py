"""When a clinic slot holds OPD on a given date, and how many patients the doctor will see.

The plan for a date comes from, in order:
1. a DateException for that date (day off, or different hours/limit), else
2. the slot's WeeklyHours row for that weekday (no row: no OPD), else, if the slot has never
   saved weekly hours,
3. DoctorClinic.opd_days / opd_start / opd_end, with no limit.

Everything that decides "can this date be booked?" reads it from here, so the doctor dashboard,
the calendar, booking and the patient app always agree.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models import WEEKDAYS, Booking, DateException, DoctorClinic, OpdSession, WeeklyHours


@dataclass(frozen=True)
class DayPlan:
    date: date
    open: bool
    start: time | None
    end: time | None
    limit: int | None  # most tokens for the day (online + walk-in); None = no limit
    note: str | None  # the doctor's note for an exception, e.g. "At a conference"
    exception: str | None  # None, "CLOSED" or "HOURS"

    def json(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "open": self.open,
            "start": hhmm(self.start),
            "end": hhmm(self.end),
            "limit": self.limit,
            "note": self.note,
            "exception": self.exception,
        }


def hhmm(t: time | None) -> str | None:
    return t.strftime("%H:%M") if t else None


def weekday(day: date) -> str:
    return WEEKDAYS[day.weekday()]


def _weekly(db: DbSession, dc: DoctorClinic) -> dict[str, WeeklyHours] | None:
    rows = db.scalars(select(WeeklyHours).where(WeeklyHours.doctor_clinic_id == dc.id)).all()
    return {r.weekday: r for r in rows} if rows else None


def plans(db: DbSession, dc: DoctorClinic, first: date, last: date) -> dict[date, DayPlan]:
    """The plan for every date from first to last (inclusive), with two queries."""
    weekly = _weekly(db, dc)
    exceptions = {
        e.date: e
        for e in db.scalars(
            select(DateException).where(DateException.doctor_clinic_id == dc.id, DateException.date.between(first, last))
        )
    }
    result = {}
    day = first
    while day <= last:
        result[day] = _plan(dc, day, weekly, exceptions.get(day))
        day += timedelta(days=1)
    return result


def day_plan(db: DbSession, dc: DoctorClinic, day: date) -> DayPlan:
    return plans(db, dc, day, day)[day]


def _plan(dc: DoctorClinic, day: date, weekly: dict[str, WeeklyHours] | None, exc: DateException | None) -> DayPlan:
    wd = weekday(day)
    if weekly is None:
        regular = (dc.opd_start, dc.opd_end, None) if wd in dc.opd_days else None
    else:
        row = weekly.get(wd)
        regular = (row.start, row.end, row.patient_limit) if row else None

    if exc is not None and exc.kind == "CLOSED":
        return DayPlan(day, False, None, None, None, exc.note, "CLOSED")
    if exc is not None:  # HOURS: open that day even if it's normally off
        limit = exc.patient_limit if exc.patient_limit is not None else (regular[2] if regular else None)
        return DayPlan(day, True, exc.start, exc.end, limit, exc.note, "HOURS")
    if regular is None:
        return DayPlan(day, False, None, None, None, None, None)
    return DayPlan(day, True, regular[0], regular[1], regular[2], None, None)


def weekly_json(db: DbSession, dc: DoctorClinic) -> list[dict]:
    """All seven weekdays, Monday first, as the doctor's timings form shows them."""
    weekly = _weekly(db, dc)
    out = []
    for wd in WEEKDAYS:
        if weekly is None:
            open_, start, end, limit = wd in dc.opd_days, dc.opd_start, dc.opd_end, None
        else:
            row = weekly.get(wd)
            open_ = row is not None
            start, end, limit = (row.start, row.end, row.patient_limit) if row else (None, None, None)
        out.append({"weekday": wd, "open": open_, "start": hhmm(start) if open_ else None, "end": hhmm(end) if open_ else None, "limit": limit})
    return out


def booked_count(db: DbSession, session_id) -> int:
    """Tokens that count towards the day's limit: everything except cancellations."""
    return db.scalar(
        select(func.count()).where(Booking.session_id == session_id, Booking.status != "CANCELLED")
    )


def booked_by_date(db: DbSession, dc: DoctorClinic, first: date, last: date) -> dict[date, int]:
    rows = db.execute(
        select(OpdSession.date, func.count(Booking.id))
        .join(Booking, Booking.session_id == OpdSession.id)
        .where(OpdSession.doctor_clinic_id == dc.id, OpdSession.date.between(first, last), Booking.status != "CANCELLED")
        .group_by(OpdSession.date)
    ).all()
    return dict(rows)


def minutes_per_patient(plan: DayPlan) -> float | None:
    """With a limit, the doctor's hours spread evenly over it (e.g. 4 hours / 30 = 8 minutes)."""
    if not plan.open or not plan.limit or not plan.start or not plan.end:
        return None
    span = datetime.combine(plan.date, plan.end) - datetime.combine(plan.date, plan.start)
    return span.total_seconds() / 60 / plan.limit


def expected_time(plan: DayPlan, patients_ahead: int, fallback_minutes: float) -> str | None:
    """Roughly when a patient's turn comes, before OPD starts: start + patients ahead × minutes each."""
    if not plan.open or plan.start is None:
        return None
    each = minutes_per_patient(plan) or fallback_minutes
    at = datetime.combine(plan.date, plan.start) + timedelta(minutes=patients_ahead * each)
    if plan.end and at.time() > plan.end:
        at = datetime.combine(plan.date, plan.end)
    # Round down to 5 minutes: a time to arrive by, not a promise.
    return (at - timedelta(minutes=at.minute % 5)).strftime("%H:%M")
