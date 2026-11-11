"""What the doctor's dashboard reads and records besides running the queue: a day's bookings and
stats, the month calendar, search, one booking's details, tests advised, and record files.

Every query is limited to the signed-in doctor's own clinics. Someone else's booking, test or
record gets the same 404 as one that doesn't exist.
"""

import uuid
from calendar import monthrange
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import exists, func, select
from sqlalchemy.orm import joinedload

from app import bookings, notifications, notify, schedule
from app.config import get_settings
from app.access import Actor, CanCorrect, CanManageTests, CanView, CanViewRecords, audit
from app.deps import Db
from app.models import Booking, DoctorClinic, MedicalRecord, OpdSession, Patient, PatientCorrection, TestAdvice
from app.routers.queue import BOOKING_NOT_FOUND, apply_outcome
from app.schemas import PatientCorrectionIn, TestsIn, TestStatusIn
from app.serializers import booking_item, clinic_slot, correction_for, iso, patient_basic, session_json
from app.timeutil import now_ist, today_ist

router = APIRouter(prefix="/api/doctor")

MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
# Record files are served from the app's own site, so only types a browser can't run as a page
# are shown inline. Anything else downloads instead.
INLINE_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}

DayParam = Annotated[date | None, Query(alias="date")]
SlotParam = Annotated[uuid.UUID | None, Query(alias="doctorClinicId")]


# --- helpers --------------------------------------------------------------------------


def _slot(db: Db, actor: Actor, doctor_clinic_id: uuid.UUID | None) -> DoctorClinic:
    """The chosen clinic slot, or the doctor's first one (earliest OPD) when none is given."""
    query = (
        select(DoctorClinic)
        .options(joinedload(DoctorClinic.clinic))
        .where(actor.slot_filter())
    )
    if doctor_clinic_id is not None:
        query = query.where(DoctorClinic.id == doctor_clinic_id)
    else:
        query = query.order_by(DoctorClinic.opd_start, DoctorClinic.id).limit(1)
    dc = db.scalar(query)
    if dc is None:
        if doctor_clinic_id is None:
            raise HTTPException(404, "Add a clinic and OPD timings to your profile first.")
        raise HTTPException(404, "We couldn't find that clinic.")
    return dc


def _own_bookings():
    """Bookings joined up to their slot, for filtering by doctor."""
    return select(Booking).join(OpdSession, Booking.session_id == OpdSession.id).join(DoctorClinic)


def _own_booking(db: Db, actor: Actor, booking_id: uuid.UUID, *, lock: bool = False) -> Booking:
    query = _own_bookings().where(Booking.id == booking_id, actor.slot_filter())
    if lock:
        query = query.with_for_update(of=Booking)
    booking = db.scalar(query)
    if booking is None:
        raise HTTPException(404, BOOKING_NOT_FOUND)
    return booking


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _with_place(b: Booking) -> dict:
    """A booking plus which day and clinic it is for (search results span days and clinics)."""
    return {**booking_item(b), "date": b.session.date.isoformat(), "clinicName": b.session.doctor_clinic.clinic.name}


def _number(value) -> float | None:
    return float(value) if value is not None else None


def test_json(t: TestAdvice) -> dict:
    return {
        "id": str(t.id),
        "bookingId": str(t.booking_id),
        "name": t.name,
        "advisedOn": t.advised_on.isoformat(),
        "status": t.status,
        "recordId": str(t.record_id) if t.record_id else None,
    }


def record_json(r: MedicalRecord) -> dict:
    return {
        "id": str(r.id),
        "kind": r.kind,
        "title": r.title,
        "date": r.date.isoformat(),
        "addedBy": r.added_by,
        "contentType": r.content_type,
        "fileUrl": f"/api/doctor/records/{r.id}/file",
    }


# --- the day ----------------------------------------------------------------------------


@router.get("/dashboard")
def dashboard(actor: CanView, db: Db, day: DayParam = None, doctor_clinic_id: SlotParam = None):
    """One clinic's day: the session, every booking, cancellations, and the counts."""
    dc = _slot(db, actor, doctor_clinic_id)
    day = day or today_ist()
    plan = schedule.day_plan(db, dc, day)
    opd_day = plan.open

    session = db.scalar(
        select(OpdSession).where(OpdSession.doctor_clinic_id == dc.id, OpdSession.date == day)
    )
    if session is None and opd_day and day >= today_ist():
        # Create it now so the doctor can start OPD or add walk-ins before anyone has booked.
        session = bookings.lock_session(db, dc.id, day)
        db.commit()

    rows = []
    if session is not None:
        rows = db.scalars(
            select(Booking)
            .options(joinedload(Booking.patient))
            .where(Booking.session_id == session.id)
            .order_by(Booking.token)
        ).all()
    active = [b for b in rows if b.status != "CANCELLED"]
    cancelled = sorted(
        (b for b in rows if b.status == "CANCELLED"), key=lambda b: b.cancelled_at or b.booked_at, reverse=True
    )
    count = Counter(b.status for b in rows)

    return {
        "date": day.isoformat(),
        "clinic": clinic_slot(dc),
        "isOpdDay": opd_day,
        "plan": plan.json(),
        "session": session_json(session) if session else None,
        # How the "OPD has started" messages went (all zero before OPD starts).
        "notifications": notifications.session_summary(db, session.id) if session else None,
        "stats": {
            "booked": len(active),
            "online": sum(b.source == "ONLINE" for b in active),
            "walkIn": sum(b.source == "WALK_IN" for b in active),
            "waiting": count["WAITING"],
            "withDoctor": count["WITH_DOCTOR"],
            "seen": count["DONE"],
            "sentForTests": count["SENT_FOR_TESTS"],
            "noShow": count["NO_SHOW"],
            "cancelled": count["CANCELLED"],
        },
        "bookings": [booking_item(b) for b in active],
        "cancelled": [booking_item(b) for b in cancelled],
    }


@router.get("/calendar")
def calendar(
    actor: CanView,
    db: Db,
    month: Annotated[str | None, Query(pattern=MONTH_RE)] = None,
    doctor_clinic_id: SlotParam = None,
):
    """Every day of one month with its booking count (cancellations not counted)."""
    dc = _slot(db, actor, doctor_clinic_id)
    first = date.fromisoformat(f"{month}-01") if month else today_ist().replace(day=1)
    last = first.replace(day=monthrange(first.year, first.month)[1])

    rows = db.execute(
        select(OpdSession.date, OpdSession.status, func.count(Booking.id).filter(Booking.status != "CANCELLED"))
        .outerjoin(Booking, Booking.session_id == OpdSession.id)
        .where(OpdSession.doctor_clinic_id == dc.id, OpdSession.date.between(first, last))
        .group_by(OpdSession.id)
    ).all()
    by_date = {d: (status, n) for d, status, n in rows}

    today = today_ist()
    plans = schedule.plans(db, dc, first, last)
    days = []
    for d, plan in plans.items():
        status, n = by_date.get(d, (None, 0))
        days.append(
            {
                "date": d.isoformat(),
                "bookings": n,
                "isOpdDay": plan.open,
                "sessionStatus": status,
                # No OPD that day (weekly hours or a day off), or a coming day the doctor closed.
                "closed": not plan.open or (status == "CLOSED" and d > today),
                "limit": plan.limit,
                "exception": plan.exception,
                "note": plan.note,
            }
        )
    return {"month": first.strftime("%Y-%m"), "doctorClinicId": str(dc.id), "days": days}


# --- search and details ------------------------------------------------------------------


@router.get("/search")
def search(
    actor: CanView,
    db: Db,
    q: Annotated[str, Query(max_length=100)] = "",
    day: DayParam = None,
    doctor_clinic_id: SlotParam = None,
):
    """A token number searches one day (today by default); 5+ digits search phone numbers;
    anything else searches patient names across all days, most recent first."""
    q = q.strip()
    if not q:
        return {"results": []}

    query = (
        _own_bookings()
        .join(Patient, Booking.patient_id == Patient.id)
        .options(
            joinedload(Booking.patient),
            joinedload(Booking.session).joinedload(OpdSession.doctor_clinic).joinedload(DoctorClinic.clinic),
        )
        .where(actor.slot_filter())
        .order_by(OpdSession.date.desc(), Booking.token)
        .limit(20)
    )
    if doctor_clinic_id is not None:
        query = query.where(DoctorClinic.id == doctor_clinic_id)

    digits = q.isascii() and q.isdigit()
    if digits and len(q) <= 4:
        query = query.where(Booking.token == int(q), OpdSession.date == (day or today_ist()))
    elif digits:
        query = query.where(Patient.phone.like(_like(q)))
    else:
        query = query.where(Patient.name.ilike(_like(q)))

    return {"results": [_with_place(b) for b in db.scalars(query).unique()]}


@router.get("/bookings/{booking_id}")
def booking_detail(booking_id: uuid.UUID, actor: CanView, db: Db):
    """Everything the patient panel shows.

    Records (prescriptions and reports) are only included when the patient agreed to share them.
    Tests advised are always included: this doctor advised them, and they hold no results.
    """
    booking = _own_booking(db, actor, booking_id)
    patient = booking.patient
    session = booking.session
    consent = patient.records_consent_at is not None
    can_view_records = actor.can("records.view")

    position = None
    if booking.status == "WAITING":
        position = 1 + db.scalar(
            select(func.count()).where(
                Booking.session_id == session.id, Booking.status == "WAITING", Booking.token < booking.token
            )
        )

    tests = db.scalars(
        select(TestAdvice)
        .join(Booking, TestAdvice.booking_id == Booking.id)
        .join(OpdSession, Booking.session_id == OpdSession.id)
        .join(DoctorClinic)
        .where(TestAdvice.patient_id == patient.id, actor.slot_filter())
        .order_by(TestAdvice.advised_on.desc(), TestAdvice.name)
    ).all()
    records = []
    if consent and can_view_records:
        records = db.scalars(
            select(MedicalRecord)
            .where(MedicalRecord.patient_id == patient.id)
            .order_by(MedicalRecord.date.desc(), MedicalRecord.created_at.desc())
        ).all()

    return {
        "booking": _with_place(booking),
        "session": session_json(session),
        "queuePosition": position,
        "patient": _patient_detail(booking, actor),
        "correction": _correction_json(correction_for(patient, actor.doctor.id), patient),
        "recordsConsent": consent,
        "canViewRecords": can_view_records,
        "recordsConsentAt": iso(patient.records_consent_at),
        "tests": [test_json(t) for t in tests],
        "records": [record_json(r) for r in records],
    }


# --- tests advised ----------------------------------------------------------------------


@router.post("/bookings/{booking_id}/tests", status_code=201)
def send_for_tests(booking_id: uuid.UUID, body: TestsIn, actor: CanManageTests, db: Db):
    """Advise tests and mark the booking SENT_FOR_TESTS. Names already advised on it are skipped."""
    booking = _own_booking(db, actor, booking_id, lock=True)
    if booking.status == "CANCELLED":
        raise HTTPException(409, "This booking was cancelled.")

    existing = {
        n.lower() for n in db.scalars(select(TestAdvice.name).where(TestAdvice.booking_id == booking.id))
    }
    added = [
        TestAdvice(booking_id=booking.id, patient_id=booking.patient_id, name=name, advised_on=today_ist())
        for name in body.names
        if name.lower() not in existing
    ]
    db.add_all(added)
    changed = apply_outcome(booking, "SENT_FOR_TESTS")
    audit(db, actor, "tests.advise", f"booking:{booking.id}", token=booking.token, names=[t.name for t in added])
    db.commit()
    if changed:
        notify.booking_and_queue(booking, actor.doctor.id, "booking.updated")

    tests = db.scalars(
        select(TestAdvice).where(TestAdvice.booking_id == booking.id).order_by(TestAdvice.name)
    ).all()
    return {"booking": booking_item(booking), "tests": [test_json(t) for t in tests]}


@router.patch("/tests/{test_id}")
def set_test_status(test_id: uuid.UUID, body: TestStatusIn, actor: CanManageTests, db: Db):
    test = db.scalar(
        select(TestAdvice)
        .join(Booking, TestAdvice.booking_id == Booking.id)
        .join(OpdSession, Booking.session_id == OpdSession.id)
        .join(DoctorClinic)
        .where(TestAdvice.id == test_id, actor.slot_filter())
        .with_for_update(of=TestAdvice)
    )
    if test is None:
        raise HTTPException(404, "We couldn't find that test.")
    test.status = body.status
    audit(db, actor, "tests.status", f"test:{test.id}", name=test.name, status=body.status)
    db.commit()
    return test_json(test)


# --- record files -----------------------------------------------------------------------

FILE_NOT_FOUND = "We couldn't find that file."


@router.get("/records/{record_id}/file")
def record_file(record_id: uuid.UUID, actor: CanViewRecords, db: Db):
    """The file, if the patient shared their records and has booked with this doctor."""
    seen_by_doctor = exists(
        _own_bookings()
        .with_only_columns(Booking.id)
        .where(Booking.patient_id == MedicalRecord.patient_id, actor.slot_filter())
    )
    record = db.scalar(
        select(MedicalRecord)
        .join(Patient, MedicalRecord.patient_id == Patient.id)
        .where(MedicalRecord.id == record_id, Patient.records_consent_at.is_not(None), seen_by_doctor)
    )
    if record is None:
        raise HTTPException(404, FILE_NOT_FOUND)

    root = Path(get_settings().records_dir).resolve()
    path = (root / record.file_key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, FILE_NOT_FOUND)

    inline = record.content_type in INLINE_TYPES
    return FileResponse(
        path,
        media_type=record.content_type if inline else "application/octet-stream",
        content_disposition_type="inline" if inline else "attachment",
        filename=f"{record.title[:80]}{path.suffix}",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


# --- correcting patient details --------------------------------------------------------


def _patient_detail(booking: Booking, actor: Actor) -> dict:
    """The patient as this doctor sees them: with their corrections, and this visit's measurements."""
    p = booking.patient
    height = booking.height_cm if booking.height_cm is not None else p.height_cm
    weight = booking.weight_kg if booking.weight_kg is not None else p.weight_kg
    return {
        **patient_basic(p, correction_for(p, actor.doctor.id)),
        "heightCm": _number(height),
        "weightKg": _number(weight),
    }


def _correction_json(c: PatientCorrection | None, p: Patient) -> dict | None:
    if c is None:
        return None
    return {
        "updatedBy": c.updated_by,
        "updatedAt": iso(c.updated_at),
        "original": {"name": p.name, "age": p.age, "gender": p.gender},
    }


@router.patch("/bookings/{booking_id}/patient")
def correct_patient(booking_id: uuid.UUID, body: PatientCorrectionIn, actor: CanCorrect, db: Db):
    """Correct what this doctor's dashboard shows for the patient.

    Name, age and gender go into this doctor's correction (the shared patient row, which other
    doctors and the patient's own bookings use, is never changed). Height and weight are saved on
    this visit. The phone number and records consent can't be changed here.
    """
    booking = _own_booking(db, actor, booking_id, lock=True)
    patient = booking.patient
    before = _patient_detail(booking, actor)

    fields = body.model_fields_set
    identity = {k: getattr(body, k) for k in ("name", "age", "gender") if k in fields}
    if identity:
        correction = correction_for(patient, actor.doctor.id)
        if correction is None:
            correction = PatientCorrection(patient_id=patient.id, doctor_id=actor.doctor.id)
            db.add(correction)
            patient.corrections.append(correction)
        for key, value in identity.items():
            # The patient's own value again: drop the correction for that field.
            setattr(correction, key, None if value == getattr(patient, key) else value)
        correction.updated_by = actor.name
        correction.updated_at = now_ist()
        if correction.name is None and correction.age is None and correction.gender is None:
            patient.corrections.remove(correction)
            db.delete(correction)
    if "height_cm" in fields:
        booking.height_cm = body.height_cm
    if "weight_kg" in fields:
        booking.weight_kg = body.weight_kg

    after = _patient_detail(booking, actor)
    changes = {k: {"old": before[k], "new": after[k]} for k in after if before[k] != after[k]}
    if changes:
        audit(db, actor, "patient.correct", f"booking:{booking.id}", token=booking.token, changes=changes)
    db.commit()
    if changes:
        notify.booking_changed(booking, actor.doctor.id)
    return booking_detail(booking_id, actor, db)
