"""The public directory: clinics and hospitals, their doctors, opening status and distance.

Only clinics with at least one verified doctor are listed, and only verified doctors are shown.
A doctor's registration number is only shown if they chose to show it (photo_consent_at).
"""

import base64
import binascii
import math
import time as _time
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import joinedload

from app import departments, images, profile, schedule
from app.models import Booking, Clinic, Doctor, DoctorClinic, OpdSession
from app.timeutil import now_ist

PAGE_SIZE = 10
STATUS_TTL_SECONDS = 30


# --- pagination --------------------------------------------------------------------------


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        kind, value = raw.split(":", 1)
        offset = int(value)
        if kind != "o" or offset < 0 or offset > 10_000:
            raise ValueError
        return offset
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise HTTPException(400, "That page link isn't valid. Start from the first page.") from None


def page(items: list, cursor: str | None, size: int = PAGE_SIZE) -> tuple[list, str | None]:
    start = decode_cursor(cursor)
    chunk = items[start : start + size]
    return chunk, encode_cursor(start + size) if start + size < len(items) else None


# --- distance ----------------------------------------------------------------------------


def distance_km(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float | None:
    """Straight-line distance (haversine)."""
    if a is None or b is None:
        return None
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def clinic_position(c: Clinic) -> tuple[float, float] | None:
    if c.lat is not None and c.lng is not None:
        return float(c.lat), float(c.lng)
    if c.area_ref is not None:
        return float(c.area_ref.lat), float(c.area_ref.lng)
    return None


# --- opening status (from the booking side, cached 30 seconds) ------------------------------


@dataclass(frozen=True)
class SlotStatus:
    status: str  # OPEN_NOW, OPENS_LATER, CLOSED_TODAY
    opens_at: str | None  # HH:MM, for OPENS_LATER
    waiting: int


_cache: dict[uuid.UUID, tuple[float, SlotStatus]] = {}


def clear_cache() -> None:
    _cache.clear()


def slot_statuses(db: DbSession, slots: list[DoctorClinic]) -> dict[uuid.UUID, SlotStatus]:
    """Today's status for each doctor-clinic slot: open now, opens later (when), or closed today,
    and how many patients are waiting. Cached for 30 seconds per slot."""
    now_mono = _time.monotonic()
    result = {}
    missing = []
    for dc in slots:
        hit = _cache.get(dc.id)
        if hit and hit[0] > now_mono:
            result[dc.id] = hit[1]
        else:
            missing.append(dc)
    if not missing:
        return result

    now = now_ist()
    today = now.date()
    ids = [dc.id for dc in missing]
    sessions = dict(
        db.execute(select(OpdSession.doctor_clinic_id, OpdSession.status).where(OpdSession.doctor_clinic_id.in_(ids), OpdSession.date == today)).all()
    )
    waiting = dict(
        db.execute(
            select(OpdSession.doctor_clinic_id, func.count(Booking.id))
            .join(Booking, Booking.session_id == OpdSession.id)
            .where(OpdSession.doctor_clinic_id.in_(ids), OpdSession.date == today, Booking.status == "WAITING")
            .group_by(OpdSession.doctor_clinic_id)
        ).all()
    )
    for dc in missing:
        plan = schedule.day_plan(db, dc, today)
        session_status = sessions.get(dc.id)
        if session_status in ("IN_OPD", "ON_BREAK"):
            # The doctor is actually running OPD (even past the set hours): open now.
            status = SlotStatus("OPEN_NOW", None, waiting.get(dc.id, 0))
        elif not plan.open or session_status == "CLOSED" or now.time() >= plan.end:
            status = SlotStatus("CLOSED_TODAY", None, waiting.get(dc.id, 0))
        elif now.time() < plan.start:
            status = SlotStatus("OPENS_LATER", schedule.hhmm(plan.start), waiting.get(dc.id, 0))
        else:
            status = SlotStatus("OPEN_NOW", None, waiting.get(dc.id, 0))
        _cache[dc.id] = (now_mono + STATUS_TTL_SECONDS, status)
        result[dc.id] = status
    return result


def clinic_status(statuses: list[SlotStatus]) -> dict:
    """A clinic is open now if any of its doctors is; else it opens at the earliest doctor's time."""
    waiting = sum(s.waiting for s in statuses)
    if any(s.status == "OPEN_NOW" for s in statuses):
        return {"opdStatus": "OPEN_NOW", "opensAt": None, "waitingNow": waiting}
    later = sorted(s.opens_at for s in statuses if s.status == "OPENS_LATER")
    if later:
        return {"opdStatus": "OPENS_LATER", "opensAt": later[0], "waitingNow": waiting}
    return {"opdStatus": "CLOSED_TODAY", "opensAt": None, "waitingNow": waiting}


# --- loading ----------------------------------------------------------------------------------


def listed_slots(db: DbSession, clinic_ids: list[uuid.UUID] | None = None) -> list[DoctorClinic]:
    """Slots of verified doctors (optionally at some clinics), with doctor and clinic loaded."""
    query = (
        select(DoctorClinic)
        .join(Doctor)
        .options(joinedload(DoctorClinic.doctor), joinedload(DoctorClinic.clinic))
        .where(Doctor.is_verified.is_(True))
        .order_by(Doctor.full_name, DoctorClinic.opd_start)
    )
    if clinic_ids is not None:
        query = query.where(DoctorClinic.clinic_id.in_(clinic_ids))
    return list(db.scalars(query).unique())


# --- shapes ------------------------------------------------------------------------------------


def doctor_card(dc: DoctorClinic, status: SlotStatus | None = None) -> dict:
    """A doctor at one clinic, as patients see them."""
    d = dc.doctor
    consent = d.photo_consent_at is not None
    pub = profile.public_json(d)
    card = {
        "id": str(d.id),
        "doctorClinicId": str(dc.id),
        "name": d.full_name,
        "nameHi": d.name_hi,
        "department": departments.name(d.department_key) or d.department,
        "departmentKey": d.department_key,
        "designation": d.designation,
        "degree": d.qualification,
        "experienceYears": d.experience_years,
        "fee": dc.fee,
        "registrationNo": d.registration_no if consent else None,
        "photoUrl": pub["photoUrl"],
        "initials": initials(d.full_name),
    }
    if status is not None:
        card["waitingToday"] = status.waiting if status.status == "OPEN_NOW" else None
        card["opensAt"] = status.opens_at
        card["opdStatus"] = status.status
    return card


def initials(name: str) -> str:
    words = [w for w in name.replace(".", " ").split() if w.lower() not in ("dr", "doctor")]
    return ((words[0][0] if words else "") + (words[-1][0] if len(words) > 1 else "")).upper()


def department_keys(slots: list[DoctorClinic]) -> list[str]:
    """The departments of these doctors, in the fixed department order."""
    found = {dc.doctor.department_key for dc in slots if dc.doctor.department_key in departments.BY_KEY}
    return [d.key for d in departments.DEPARTMENTS if d.key in found]


def summary(doctor_count: int, department_keys: list[str]) -> str:
    names = [departments.name(k) for k in department_keys if departments.name(k)]
    doctors = f"{doctor_count} doctor{'s' if doctor_count != 1 else ''}"
    if not names:
        return doctors
    shown = ", ".join(names[:2]) + (f" +{len(names) - 2} more" if len(names) > 2 else "")
    return f"{doctors}, {shown}"


def cover_url(c: Clinic) -> str | None:
    found = images.urls(c.cover_photo_key)
    return found["small"] if found else None


def clinic_card(c: Clinic, slots: list[DoctorClinic], statuses: dict, origin) -> dict:
    keys = department_keys(slots)
    distance = distance_km(origin, clinic_position(c))
    doctors = list({dc.doctor_id: dc for dc in slots}.values())
    return {
        "id": str(c.id),
        "name": c.name,
        "nameHi": c.name_hi,
        "type": c.type,
        "coverPhotoUrl": cover_url(c),
        "address": c.address,
        "areaName": c.area,
        "distanceKm": round(distance, 1) if distance is not None else None,
        "doctorCount": len(doctors),
        "departmentCount": len(keys),
        "departmentKeys": keys,
        "summary": summary(len(doctors), keys),
        **clinic_status([statuses[dc.id] for dc in slots]),
        "doctors": [doctor_card(dc) for dc in doctors[:5]],
    }


def clinic_details(c: Clinic, slots: list[DoctorClinic], statuses: dict) -> dict:
    keys = department_keys(slots)
    position = clinic_position(c)
    return {
        "id": str(c.id),
        "type": c.type,
        "name": c.name,
        "nameHi": c.name_hi,
        "address": c.address,
        "areaName": c.area,
        "lat": float(c.lat) if c.lat is not None else (position[0] if position else None),
        "lng": float(c.lng) if c.lng is not None else (position[1] if position else None),
        "locationIsApproximate": c.lat is None,
        "phone": c.phone,
        "opdHoursTextEn": c.opd_hours_text_en,
        "opdHoursTextHi": c.opd_hours_text_hi,
        "coverPhoto": images.urls(c.cover_photo_key),
        "photos": [{"id": str(p.id), **images.urls(p.key), "width": p.width, "height": p.height} for p in c.photos],
        **clinic_status([statuses[dc.id] for dc in slots]),
        "departments": [departments.BY_KEY[k].json() for k in keys],
        "doctorCount": len({dc.doctor_id for dc in slots}),
    }
