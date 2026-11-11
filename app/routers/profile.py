"""The doctor's own profile (root only) and the public doctor photo."""

import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field
from sqlalchemy import select

from app import profile
from app.access import Root, audit
from app.deps import Db
from app.models import Doctor
from app.schemas import FullName, ProfileIn

router = APIRouter()

PHOTO_NOT_FOUND = "There's no photo."


def _photo_response(doctor: Doctor, cache: str) -> FileResponse:
    path = profile.photo_path(doctor)
    if path is None:
        raise HTTPException(404, PHOTO_NOT_FOUND)
    return FileResponse(
        path,
        media_type=doctor.photo_content_type,
        headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"},
    )


class ProfileUpdateIn(ProfileIn):
    full_name: FullName = Field(None, alias="fullName")  # optional: change the display name too


@router.get("/api/doctor/profile")
def get_profile(actor: Root):
    return profile.own_json(actor.doctor)


@router.put("/api/doctor/profile")
def update_profile(body: ProfileUpdateIn, actor: Root, db: Db):
    """Update the profile. Leave out `photo` to keep the current one."""
    doctor = actor.doctor
    if "full_name" in body.model_fields_set and body.full_name:
        doctor.full_name = body.full_name
    old_photo = profile.apply(doctor, body)
    audit(db, actor, "profile.update", f"doctor:{doctor.id}", photo=body.photo is not None)
    db.commit()
    profile.delete_photo(old_photo)
    return profile.own_json(doctor)


@router.get("/api/doctor/profile/photo")
def own_photo(actor: Root):
    return _photo_response(actor.doctor, "private, max-age=86400")


@router.get("/api/public/doctors/{doctor_id}/photo")
def public_photo(doctor_id: uuid.UUID, db: Db):
    """Only verified doctors' photos are public, like the rest of their profile."""
    doctor = db.scalar(select(Doctor).where(Doctor.id == doctor_id, Doctor.is_verified.is_(True)))
    if doctor is None:
        raise HTTPException(404, PHOTO_NOT_FOUND)
    # The URL carries ?v=<photo id>, so it can be cached for a long time.
    return _photo_response(doctor, "public, max-age=604800")
