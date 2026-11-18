"""A doctor editing the clinics they sit at: type, Hindi name, address, location, phone, OPD
hours text, and public photos (cover and gallery). Any doctor with an OPD slot at the clinic can
edit it; every change goes into that doctor's activity log with who made it.
"""

import uuid

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import func, select

from app import directory, images
from app.access import Root, audit
from app.deps import Db
from app.models import WEEKDAYS, Area, Clinic, ClinicPhoto, DoctorClinic, WeeklyHours
from app.schemas import ClinicCreateIn, ClinicPhotoIn, ClinicUpdateIn, CoverIn, PhotoOrderIn
from app.serializers import clinic_slot

router = APIRouter(prefix="/api/doctor/clinic-details")

MAX_PHOTOS = 12


def _clinic(db: Db, actor, clinic_id: uuid.UUID) -> Clinic:
    sits_here = db.scalar(
        select(DoctorClinic.id).where(DoctorClinic.clinic_id == clinic_id, DoctorClinic.doctor_id == actor.doctor.id).limit(1)
    )
    clinic = db.scalar(select(Clinic).where(Clinic.id == clinic_id).with_for_update(of=Clinic)) if sits_here else None
    if clinic is None:
        raise HTTPException(404, "We couldn't find that clinic among yours.")
    return clinic


def _json(db: Db, c: Clinic) -> dict:
    return {
        "id": str(c.id),
        "type": c.type,
        "name": c.name,
        "nameHi": c.name_hi,
        "address": c.address,
        "areaName": c.area,
        "areaKey": c.area_ref.key if c.area_ref else None,
        "lat": float(c.lat) if c.lat is not None else None,
        "lng": float(c.lng) if c.lng is not None else None,
        "phone": c.phone,
        "opdHoursTextEn": c.opd_hours_text_en,
        "opdHoursTextHi": c.opd_hours_text_hi,
        "coverPhotoId": next((str(p.id) for p in c.photos if p.key == c.cover_photo_key), None),
        "photos": [{"id": str(p.id), **images.urls(p.key), "width": p.width, "height": p.height, "addedBy": p.created_by} for p in c.photos],
        "maxPhotos": MAX_PHOTOS,
        "areas": [{"key": a.key, "name": a.name, "nameHi": a.name_hi} for a in db.scalars(select(Area).order_by(Area.name))],
    }


def _done(db: Db, c: Clinic) -> dict:
    db.commit()
    directory.clear_cache()
    return _json(db, c)


MAX_CLINICS = 10


@router.post("", status_code=201)
def add_clinic(body: ClinicCreateIn, actor: Root, db: Db):
    """A doctor adds a clinic they sit at, with their fee and OPD days and hours there.

    Bookings open straight away (for a verified doctor). Weekly hours rows are written too, so
    the daily limit applies and the timings page shows the same days.
    """
    count = db.scalar(select(func.count()).select_from(DoctorClinic).where(DoctorClinic.doctor_id == actor.doctor.id))
    if count >= MAX_CLINICS:
        raise HTTPException(409, f"You can add up to {MAX_CLINICS} clinics.")
    area = db.scalar(select(Area).where(Area.key == body.area_key))
    if area is None:
        raise HTTPException(400, "Choose an area from the list.")
    days = [d for d in WEEKDAYS if d in body.days]
    clinic = Clinic(type=body.type, name=body.name, address=body.address, area=area.name, area_id=area.id, phone=body.phone)
    dc = DoctorClinic(doctor_id=actor.doctor.id, clinic=clinic, fee=body.fee, opd_days=days, opd_start=body.start, opd_end=body.end)
    db.add(dc)
    db.flush()
    for d in days:
        db.add(WeeklyHours(doctor_clinic_id=dc.id, weekday=d, start=body.start, end=body.end, patient_limit=body.limit))
    audit(db, actor, "clinic.add", f"clinic:{clinic.id}", clinic=clinic.name, fee=body.fee, days=",".join(days))
    db.commit()
    directory.clear_cache()
    return clinic_slot(dc)


@router.get("/{clinic_id}")
def get_clinic(clinic_id: uuid.UUID, actor: Root, db: Db):
    c = _clinic(db, actor, clinic_id)
    result = _json(db, c)
    db.commit()
    return result


@router.patch("/{clinic_id}")
def update_clinic(clinic_id: uuid.UUID, body: ClinicUpdateIn, actor: Root, db: Db):
    c = _clinic(db, actor, clinic_id)
    fields = body.model_fields_set
    changes = {}
    for attr, json_name in (("type", "type"), ("name", "name"), ("name_hi", "nameHi"), ("address", "address"), ("phone", "phone"),
                            ("opd_hours_text_en", "opdHoursTextEn"), ("opd_hours_text_hi", "opdHoursTextHi"), ("lat", "lat"), ("lng", "lng")):
        if attr in fields:
            value = getattr(body, attr)
            if attr in ("type", "name", "address") and value is None:
                raise HTTPException(400, f"{json_name} can't be empty.")
            changes[json_name] = value
            setattr(c, attr, value)
    if "area_key" in fields:
        if body.area_key is None:
            c.area_id = None
        else:
            area = db.scalar(select(Area).where(Area.key == body.area_key))
            if area is None:
                raise HTTPException(400, "Choose an area from the list.")
            c.area_id, c.area = area.id, area.name
        changes["areaKey"] = body.area_key
    if changes:
        audit(db, actor, "clinic.update", f"clinic:{c.id}", clinic=c.name, changes={k: str(v) for k, v in changes.items()})
    return _done(db, c)


@router.post("/{clinic_id}/photos", status_code=201)
def add_photo(clinic_id: uuid.UUID, body: ClinicPhotoIn, actor: Root, db: Db):
    """Add a photo (resized to 1200 and 400 px). `cover: true` also makes it the cover."""
    c = _clinic(db, actor, clinic_id)
    if len(c.photos) >= MAX_PHOTOS:
        raise HTTPException(409, f"A clinic can have up to {MAX_PHOTOS} photos. Remove one first.")
    try:
        stored = images.store(images.decode_data_url(body.image), f"clinics/{c.id}")
    except images.ImageError as e:
        raise HTTPException(400, str(e)) from None
    photo = ClinicPhoto(key=stored.key, width=stored.width, height=stored.height, position=len(c.photos), created_by=actor.name)
    c.photos.append(photo)
    if body.cover or c.cover_photo_key is None:  # the first photo is the cover until another is chosen
        c.cover_photo_key = stored.key
    audit(db, actor, "clinic.photo_add", f"clinic:{c.id}", clinic=c.name, cover=c.cover_photo_key == stored.key)
    return _done(db, c)


@router.delete("/{clinic_id}/photos/{photo_id}")
def remove_photo(clinic_id: uuid.UUID, photo_id: uuid.UUID, actor: Root, db: Db):
    c = _clinic(db, actor, clinic_id)
    photo = next((p for p in c.photos if p.id == photo_id), None)
    if photo is None:
        raise HTTPException(404, "We couldn't find that photo.")
    c.photos.remove(photo)
    for i, p in enumerate(c.photos):
        p.position = i
    if c.cover_photo_key == photo.key:
        c.cover_photo_key = c.photos[0].key if c.photos else None
    audit(db, actor, "clinic.photo_remove", f"clinic:{c.id}", clinic=c.name)
    result = _done(db, c)
    images.delete(photo.key)
    return result


@router.put("/{clinic_id}/photos/order")
def reorder_photos(clinic_id: uuid.UUID, body: PhotoOrderIn, actor: Root, db: Db):
    c = _clinic(db, actor, clinic_id)
    by_id = {p.id: p for p in c.photos}
    if len(body.photo_ids) != len(by_id) or set(body.photo_ids) != set(by_id):
        raise HTTPException(400, "Send every photo of this clinic once, in the new order.")
    for i, pid in enumerate(body.photo_ids):
        by_id[pid].position = i
    c.photos.sort(key=lambda p: p.position)
    return _done(db, c)


@router.put("/{clinic_id}/cover")
def set_cover(clinic_id: uuid.UUID, body: CoverIn, actor: Root, db: Db):
    c = _clinic(db, actor, clinic_id)
    photo = next((p for p in c.photos if p.id == body.photo_id), None)
    if photo is None:
        raise HTTPException(404, "We couldn't find that photo.")
    c.cover_photo_key = photo.key
    audit(db, actor, "clinic.cover", f"clinic:{c.id}", clinic=c.name)
    return _done(db, c)
