"""The doctor (root) managing staff access: add, change, remove, invite links, activity log.

Every route here is root only. A staff member can never see or change grants, their own included.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import or_, select, update

from app import realtime, security
from app.access import PERMISSIONS, PRESETS, Root, audit, normalize_permissions
from app.deps import Db
from app.models import AuditEntry, Doctor, DoctorClinic, Session, Staff, StaffMembership
from app.schemas import StaffIn, StaffUpdateIn
from app.serializers import iso

router = APIRouter(prefix="/api/doctor")

INVITE_TTL = timedelta(hours=48)
NOT_FOUND = "We couldn't find that staff member."


def _check_clinics(db: Db, actor, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    mine = set(db.scalars(select(DoctorClinic.id).where(DoctorClinic.doctor_id == actor.doctor.id)))
    ids = list(dict.fromkeys(ids))
    if not ids or not set(ids) <= mine:
        raise HTTPException(400, "Choose at least one of your clinics.")
    return ids


def _check_permissions(permissions: list[str]) -> list[str]:
    unknown = set(permissions) - set(PERMISSIONS)
    if unknown:
        raise HTTPException(400, f"Unknown permission: {sorted(unknown)[0]}.")
    return normalize_permissions(permissions)


def _new_invite(staff: Staff) -> str:
    token = security.new_session_token()
    staff.invite_token_hash = security.hash_token(token)
    staff.invite_expires_at = datetime.now(timezone.utc) + INVITE_TTL
    return f"/staff/invite/{token}"


def _member_json(m: StaffMembership) -> dict:
    s = m.staff
    if s.password_hash is not None:
        status = "ACTIVE"
    elif s.invite_expires_at and s.invite_expires_at > datetime.now(timezone.utc):
        status = "INVITED"
    else:
        status = "INVITE_EXPIRED"
    return {
        "id": str(m.id),
        "staffId": str(s.id),
        "fullName": s.full_name,
        "phone": s.phone,
        "email": s.email,
        "role": m.role,
        "permissions": normalize_permissions(m.permissions),
        "doctorClinicIds": [str(i) for i in m.doctor_clinic_ids],
        "status": status,
        "inviteExpiresAt": iso(s.invite_expires_at) if status != "ACTIVE" else None,
        "addedAt": iso(m.created_at),
    }


def _membership(db: Db, actor, membership_id: uuid.UUID) -> StaffMembership:
    m = db.scalar(
        select(StaffMembership).where(StaffMembership.id == membership_id, StaffMembership.doctor_id == actor.doctor.id)
    )
    if m is None:
        raise HTTPException(404, NOT_FOUND)
    return m


@router.get("/staff")
def list_staff(actor: Root, db: Db):
    members = db.scalars(
        select(StaffMembership)
        .where(StaffMembership.doctor_id == actor.doctor.id)
        .order_by(StaffMembership.created_at)
    ).all()
    return {
        "staff": [_member_json(m) for m in members],
        "permissions": [{"key": k, "label": v} for k, v in PERMISSIONS.items()],
        "presets": PRESETS,
    }


@router.post("/staff", status_code=201)
def add_staff(body: StaffIn, actor: Root, db: Db):
    """Give someone access. A new person gets an invite link; someone who already has a staff
    account (working for another doctor) just gets this doctor's access added."""
    clinics = _check_clinics(db, actor, body.doctor_clinic_ids)
    permissions = _check_permissions(body.permissions)
    column, value = (Staff.phone, body.phone) if body.phone else (Staff.email, body.email)

    doctor_column = Doctor.phone if body.phone else Doctor.email
    if db.scalar(select(Doctor.id).where(doctor_column == value)) is not None:
        raise HTTPException(409, "This mobile number or email belongs to a doctor's account. Use another one.")

    staff = db.scalar(select(Staff).where(column == value).with_for_update())
    invite_url = None
    if staff is None:
        staff = Staff(full_name=body.full_name, phone=body.phone, email=body.email)
        db.add(staff)
        db.flush()
    elif db.scalar(
        select(StaffMembership.id).where(StaffMembership.staff_id == staff.id, StaffMembership.doctor_id == actor.doctor.id)
    ):
        raise HTTPException(409, "This person is already on your staff.")
    if staff.password_hash is None:
        invite_url = _new_invite(staff)

    member = StaffMembership(
        staff_id=staff.id, doctor_id=actor.doctor.id, role=body.role, permissions=permissions, doctor_clinic_ids=clinics
    )
    db.add(member)
    db.flush()
    audit(db, actor, "staff.add", f"staff:{staff.id}", name=staff.full_name, role=body.role, permissions=permissions)
    db.commit()
    return {"member": _member_json(member), "inviteUrl": invite_url}


@router.patch("/staff/{membership_id}")
def update_staff(membership_id: uuid.UUID, body: StaffUpdateIn, actor: Root, db: Db):
    m = _membership(db, actor, membership_id)
    before = _member_json(m)
    fields = body.model_fields_set
    if "role" in fields and body.role:
        m.role = body.role
    if "permissions" in fields and body.permissions is not None:
        m.permissions = _check_permissions(body.permissions)
    if "doctor_clinic_ids" in fields and body.doctor_clinic_ids is not None:
        m.doctor_clinic_ids = _check_clinics(db, actor, body.doctor_clinic_ids)
    after = _member_json(m)
    changes = {k: {"old": before[k], "new": after[k]} for k in ("role", "permissions", "doctorClinicIds") if before[k] != after[k]}
    if changes:
        audit(db, actor, "staff.update", f"staff:{m.staff_id}", name=m.staff.full_name, changes=changes)
    db.commit()
    # Open dashboards reconnect, so live updates follow the new clinics and permissions at once.
    realtime.kick(m.staff_id, actor.doctor.id)
    return after


@router.delete("/staff/{membership_id}", status_code=204)
def remove_staff(membership_id: uuid.UUID, actor: Root, db: Db):
    m = _membership(db, actor, membership_id)
    staff_id, name = m.staff_id, m.staff.full_name
    db.delete(m)
    # Their signed-in browsers stop acting for this doctor straight away.
    db.execute(
        update(Session)
        .where(Session.staff_id == staff_id, Session.acting_doctor_id == actor.doctor.id)
        .values(acting_doctor_id=None)
    )
    audit(db, actor, "staff.remove", f"staff:{staff_id}", name=name)
    db.commit()
    realtime.kick(staff_id, actor.doctor.id)
    return Response(status_code=204)


@router.post("/staff/{membership_id}/invite")
def new_invite_link(membership_id: uuid.UUID, actor: Root, db: Db):
    """A fresh invite link (the old one stops working). Only before they've set a password."""
    m = _membership(db, actor, membership_id)
    if m.staff.password_hash is not None:
        raise HTTPException(409, "They've already set up their account. They can sign in with it.")
    url = _new_invite(m.staff)
    audit(db, actor, "staff.invite", f"staff:{m.staff_id}", name=m.staff.full_name)
    db.commit()
    return {"inviteUrl": url, "inviteExpiresAt": iso(m.staff.invite_expires_at)}


@router.get("/activity")
def activity(actor: Root, db: Db, limit: int = Query(50, ge=1, le=200), staff_id: uuid.UUID | None = Query(None, alias="staffId")):
    """The doctor's activity log, newest first: everything done by them and their staff."""
    query = (
        select(AuditEntry)
        .where(AuditEntry.doctor_id == actor.doctor.id)
        .order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc())
        .limit(limit)
    )
    if staff_id is not None:
        query = query.where(or_(AuditEntry.actor_id == staff_id, AuditEntry.target == f"staff:{staff_id}"))
    return {
        "entries": [
            {
                "id": e.id,
                "at": iso(e.created_at),
                "actorKind": e.actor_kind,
                "actorName": e.actor_name,
                "action": e.action,
                "target": e.target,
                "detail": e.detail,
            }
            for e in db.scalars(query)
        ]
    }
