"""The doctor's professional profile: photo, degrees, experience, fee, registration and the rest.

Photos are stored under MEDIA_DIR with a random file name (never the uploaded name) and only
after their bytes were checked to be a JPEG, PNG or WebP (app/schemas.py `_photo`).
"""

import logging
import uuid
from pathlib import Path

from app.config import get_settings
from app.models import Doctor, DoctorDegree
from app.schemas import ProfileIn

log = logging.getLogger("clinically")

EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
LEVEL_ORDER = ["UG", "PG", "SUPER_SPECIALITY", "FELLOWSHIP", "DIPLOMA", "OTHER"]


def media_root() -> Path:
    return Path(get_settings().media_dir).resolve()


def photo_path(doctor: Doctor) -> Path | None:
    if not doctor.photo_key:
        return None
    root = media_root()
    path = (root / doctor.photo_key).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def save_photo(data: bytes, content_type: str) -> str:
    """Write the photo to a new random file and return its key."""
    key = f"doctors/{uuid.uuid4().hex}{EXTENSIONS[content_type]}"
    path = media_root() / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return key


def delete_photo(key: str | None) -> None:
    if not key:
        return
    root = media_root()
    path = (root / key).resolve()
    if path.is_relative_to(root):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("Couldn't delete old photo %s", key)


def apply(doctor: Doctor, body: ProfileIn) -> str | None:
    """Copy the profile onto the doctor. Saves a new photo if one was sent and returns the
    previous photo's key, to delete once the change is committed."""
    doctor.department = body.specialisation
    doctor.experience_years = body.experience_years
    doctor.consultation_fee = body.fee
    doctor.registration_no = body.registration_no
    doctor.medical_council = body.medical_council
    doctor.about = body.about
    doctor.achievements = body.achievements
    doctor.expertise = body.expertise
    doctor.degrees = [
        DoctorDegree(position=i, level=d.level, name=d.name, institute=d.institute, year=d.year)
        for i, d in enumerate(body.degrees)
    ]
    # Shown wherever one line is enough: "MBBS, MS (Orthopaedics)".
    doctor.qualification = ", ".join(d.name for d in sorted(body.degrees, key=lambda d: LEVEL_ORDER.index(d.level)))

    old = None
    if body.photo is not None:
        data, content_type = body.photo
        old = doctor.photo_key
        doctor.photo_key = save_photo(data, content_type)
        doctor.photo_content_type = content_type
    return old


def is_complete(doctor: Doctor) -> bool:
    """Everything registration now requires. Older accounts may be missing some of it."""
    return bool(
        doctor.photo_key
        and doctor.degrees
        and doctor.experience_years is not None
        and doctor.consultation_fee is not None
        and doctor.registration_no
    )


def _photo_url(doctor: Doctor, base: str) -> str | None:
    # The key changes with every new photo, so browsers never show a stale one.
    return f"{base}?v={Path(doctor.photo_key).stem[:12]}" if doctor.photo_key else None


def public_json(doctor: Doctor) -> dict:
    """What patients see. The registration number and contact details stay private."""
    return {
        "photoUrl": _photo_url(doctor, f"/api/public/doctors/{doctor.id}/photo"),
        "specialisation": doctor.department,
        "experienceYears": doctor.experience_years,
        "qualification": doctor.qualification,
        "degrees": [
            {"level": d.level, "name": d.name, "institute": d.institute, "year": d.year} for d in doctor.degrees
        ],
        "fee": doctor.consultation_fee,
        "about": doctor.about,
        "achievements": list(doctor.achievements or []),
        "expertise": list(doctor.expertise or []),
        "verified": doctor.is_verified,
    }


def own_json(doctor: Doctor) -> dict:
    """The doctor's own view, for the profile form."""
    return {
        **public_json(doctor),
        "fullName": doctor.full_name,
        "photoUrl": _photo_url(doctor, "/api/doctor/profile/photo"),
        "registrationNo": doctor.registration_no,
        "medicalCouncil": doctor.medical_council,
        "complete": is_complete(doctor),
    }
