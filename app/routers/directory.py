"""Patient-facing directory: home feed, clinic page, a clinic's doctors, search, public images."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import or_, select

from app import departments, directory
from app.deps import Db
from app.models import Area, Clinic, Doctor
from app.storage import PUBLIC_PREFIX, get_storage

router = APIRouter()

CLINIC_NOT_FOUND = "We couldn't find that clinic or hospital."


def _origin(db: Db, area_id: uuid.UUID | None, lat: float | None, lng: float | None) -> tuple[float, float]:
    """Where to measure distance from: the patient's location, else their area, else the city centre."""
    if (lat is None) != (lng is None):
        raise HTTPException(400, "Send both lat and lng, or neither.")
    if lat is not None:
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise HTTPException(400, "That location isn't valid.")
        return lat, lng
    if area_id is not None:
        area = db.get(Area, area_id)
        if area is None:
            raise HTTPException(400, "Choose an area from the list.")
        return float(area.lat), float(area.lng)
    return departments.CITY_CENTRE


def _by_clinic(slots):
    grouped: dict = {}
    for dc in slots:
        grouped.setdefault(dc.clinic_id, []).append(dc)
    return grouped


@router.get("/api/public/areas")
def areas(db: Db):
    return {"areas": [{"id": str(a.id), "key": a.key, "name": a.name, "nameHi": a.name_hi} for a in db.scalars(select(Area).order_by(Area.name))]}


@router.get("/api/public/home")
def home(
    db: Db,
    area_id: Annotated[uuid.UUID | None, Query(alias="areaId")] = None,
    lat: float | None = None,
    lng: float | None = None,
    type: Literal["all", "clinic", "hospital"] = "all",
    open_now: Annotated[bool, Query(alias="openNow")] = False,
    department: str | None = None,
    q: Annotated[str, Query(max_length=100)] = "",
    cursor: str | None = None,
):
    """Departments, and clinics and hospitals nearest first (10 per page).

    `department` keeps clinics with a doctor in that department; `q` keeps clinics whose name or
    area, or one of whose doctors' name or department, contains the words.
    """
    origin = _origin(db, area_id, lat, lng)
    if department and department not in departments.BY_KEY:
        raise HTTPException(400, "Choose a department from the list.")
    words = " ".join(q.split()).lower()
    slots = directory.listed_slots(db)
    grouped = _by_clinic(slots)
    statuses = directory.slot_statuses(db, slots)

    clinics = []
    for clinic_slots in grouped.values():
        c = clinic_slots[0].clinic
        if type != "all" and c.type != type.upper():
            continue
        status = directory.clinic_status([statuses[dc.id] for dc in clinic_slots])
        if open_now and status["opdStatus"] != "OPEN_NOW":
            continue
        if department and not any(dc.doctor.department_key == department for dc in clinic_slots):
            continue
        if words and not _matches(words, c, clinic_slots):
            continue
        clinics.append((directory.distance_km(origin, directory.clinic_position(c)), c, clinic_slots))
    # Nearest first; clinics with no known position last, by name.
    clinics.sort(key=lambda x: (x[0] is None, x[0] or 0, x[1].name.lower()))

    shown, next_cursor = directory.page(clinics, cursor)
    return {
        "departments": [d.json() for d in departments.DEPARTMENTS],
        "clinics": [directory.clinic_card(c, s, statuses, origin) for _, c, s in shown],
        "nextCursor": next_cursor,
    }


def _matches(words: str, c: Clinic, slots) -> bool:
    texts = [c.name, c.name_hi, c.area]
    for dc in slots:
        d = dc.doctor
        dep = departments.BY_KEY.get(d.department_key)
        texts += [d.full_name, d.name_hi, d.department, dep.name_en if dep else None, dep.name_hi if dep else None]
    return any(words in t.lower() for t in texts if t)


def _listed_clinic(db: Db, clinic_id: uuid.UUID):
    slots = directory.listed_slots(db, [clinic_id])
    if not slots:  # unknown, or no verified doctor sits there
        raise HTTPException(404, CLINIC_NOT_FOUND)
    return slots[0].clinic, slots


@router.get("/api/public/clinics/{clinic_id}")
def clinic_page(clinic_id: uuid.UUID, db: Db):
    clinic, slots = _listed_clinic(db, clinic_id)
    return directory.clinic_details(clinic, slots, directory.slot_statuses(db, slots))


@router.get("/api/public/clinics/{clinic_id}/doctors")
def clinic_doctors(
    clinic_id: uuid.UUID,
    db: Db,
    department: str | None = None,
    cursor: str | None = None,
):
    """The clinic's doctors (one entry per OPD slot, so each can be booked), 10 per page."""
    _, slots = _listed_clinic(db, clinic_id)
    if department:
        if department not in departments.BY_KEY:
            raise HTTPException(400, "Choose a department from the list.")
        slots = [dc for dc in slots if dc.doctor.department_key == department]
    statuses = directory.slot_statuses(db, slots)
    shown, next_cursor = directory.page(slots, cursor)
    return {"doctors": [directory.doctor_card(dc, statuses[dc.id]) for dc in shown], "nextCursor": next_cursor}


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/api/public/search")
def search(db: Db, q: Annotated[str, Query(max_length=100)] = ""):
    """Doctors, clinics and hospitals matching the words, in one list (up to 20).

    Each result has `type`: "doctor", "clinic" or "hospital". Names starting with the words come
    first. Department names (e.g. "bone", "हड्डी") also find their doctors.
    """
    q = " ".join(q.split())
    if len(q) < 2:
        return {"results": []}
    pattern = _like(q)
    keys = [d.key for d in departments.DEPARTMENTS if q.lower() in d.name_en.lower() or q in d.name_hi]
    matched_key = departments.match(q)
    if matched_key:
        keys.append(matched_key)

    slots = directory.listed_slots(db)
    clinic_ids = {dc.clinic_id for dc in slots}
    results = []

    clinics = db.scalars(
        select(Clinic).where(
            Clinic.id.in_(clinic_ids),
            or_(Clinic.name.ilike(pattern), Clinic.name_hi.ilike(pattern), Clinic.area.ilike(pattern)),
        )
    ).all()
    grouped = _by_clinic(slots)
    for c in clinics:
        n = len({dc.doctor_id for dc in grouped[c.id]})
        results.append({
            "type": c.type.lower(),
            "id": str(c.id),
            "name": c.name,
            "nameHi": c.name_hi,
            "subtitle": f"{c.area} · {n} doctor{'s' if n != 1 else ''}",
            "photoUrl": directory.cover_url(c),
            "clinicId": str(c.id),
        })

    doctor_ids = {dc.doctor_id for dc in slots}
    doctors = db.scalars(
        select(Doctor).where(
            Doctor.id.in_(doctor_ids),
            or_(
                Doctor.full_name.ilike(pattern),
                Doctor.name_hi.ilike(pattern),
                Doctor.department.ilike(pattern),
                Doctor.department_key.in_(keys or [""]),
            ),
        )
    ).all()
    first_slot = {}
    for dc in slots:
        first_slot.setdefault(dc.doctor_id, dc)
    for d in doctors:
        card = directory.doctor_card(first_slot[d.id])
        results.append({
            "type": "doctor",
            "id": card["id"],
            "name": card["name"],
            "nameHi": card["nameHi"],
            "subtitle": " · ".join(x for x in (card["department"], first_slot[d.id].clinic.name) if x),
            "photoUrl": card["photoUrl"],
            "initials": card["initials"],
            "doctorClinicId": card["doctorClinicId"],
            "clinicId": str(first_slot[d.id].clinic_id),
        })

    ql = q.lower()
    results.sort(key=lambda r: (not r["name"].lower().removeprefix("dr. ").startswith(ql), ql not in r["name"].lower(), r["name"].lower()))
    return {"results": results[:20]}


@router.get("/api/media/{key:path}")
def media(key: str):
    """Public images stored locally (in production they come from the CDN instead)."""
    data = get_storage().get(key) if key.startswith(PUBLIC_PREFIX) and key.endswith(".jpg") and ".." not in key else None
    if data is None:
        raise HTTPException(404, "There's no such image.")
    return Response(
        data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"},
    )
