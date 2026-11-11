from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app import profile
from app.access import CHOOSE_DOCTOR_MSG, NO_ACCESS_MSG, CurrentIdentity, get_actor, memberships_of
from app.deps import Db
from app.models import DoctorClinic
from app.serializers import clinic_slot

router = APIRouter(prefix="/api/doctor")


@router.get("/me")
def me(identity: CurrentIdentity, db: Db):
    """Who is signed in, which doctor's OPD they're working on, and what they may do there.

    viewer.type is DOCTOR (root: every permission) or STAFF. A staff member who works for several
    doctors and hasn't picked one yet gets doctor = null and their workplaces to choose from.
    """
    workplaces = []
    if identity.staff is not None:
        workplaces = [
            {"doctorId": str(m.doctor_id), "doctorName": m.doctor.full_name, "role": m.role}
            for m in memberships_of(db, identity.staff)
        ]
    try:
        actor = get_actor(identity, db)
    except HTTPException as e:
        if e.detail not in (CHOOSE_DOCTOR_MSG, NO_ACCESS_MSG):
            raise
        actor = None

    viewer = {
        "type": "DOCTOR" if identity.doctor else "STAFF",
        "id": str((identity.doctor or identity.staff).id),
        "fullName": (identity.doctor or identity.staff).full_name,
        "role": actor.membership.role if actor and actor.membership else None,
        "permissions": sorted(actor.permissions) if actor else [],
    }
    if actor is None:
        return {"viewer": viewer, "doctor": None, "workplaces": workplaces}

    doctor = actor.doctor
    clinics = db.scalars(
        select(DoctorClinic)
        .options(joinedload(DoctorClinic.clinic))
        .where(actor.slot_filter())
        .order_by(DoctorClinic.opd_start)
    ).all()
    root = actor.is_root
    return {
        "viewer": viewer,
        "workplaces": workplaces,
        "doctor": {
            "id": str(doctor.id),
            "fullName": doctor.full_name,
            # The doctor's own contact and registration details aren't shown to staff.
            "phone": doctor.phone if root else None,
            "email": doctor.email if root else None,
            "department": doctor.department,
            "qualification": doctor.qualification,
            "experienceYears": doctor.experience_years,
            "registrationNo": doctor.registration_no if root else None,
            "verified": doctor.is_verified,
            # The doctor sees their own photo; staff only once it's public (verified doctor).
            "photoUrl": profile.own_json(doctor)["photoUrl"] if root else (profile.public_json(doctor)["photoUrl"] if doctor.is_verified else None),
            # Older accounts registered before the full profile was required.
            "profileComplete": profile.is_complete(doctor),
            "clinics": [clinic_slot(dc) for dc in clinics],
        },
    }
