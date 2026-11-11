"""Who is making a request, and what they may do. Modelled on AWS IAM:

- The doctor is the root account: every permission, every clinic, and the only one who can
  manage staff. Root can't be restricted.
- A staff member (receptionist, compounder) acts *for* one doctor at a time, with that doctor's
  grant (StaffMembership): a list of permissions and the clinic slots it covers.
- Every check happens here on the server. The frontend hides what you can't do, but that's
  only for convenience.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import and_, delete, select

from app import security
from app.db import get_db
from app.models import AuditEntry, Doctor, DoctorClinic, Session, Staff, StaffMembership

# --- the permission catalog ------------------------------------------------------------

PERMISSIONS: dict[str, str] = {
    "queue.view": "See the day's bookings, calendar and patient search",
    "queue.call": "Call the next patient, or a chosen patient, in",
    "opd.manage": "Start, pause, end and reopen OPD",
    "walkins.add": "Add walk-in tokens",
    "bookings.outcome": "Mark patients as seen or didn't come",
    "tests.manage": "Send patients for tests and mark tests done",
    "records.view": "Open patients' prescriptions and reports (when the patient agreed)",
    "patients.correct": "Correct patients' name, age, gender, height and weight",
    "schedule.manage": "Change OPD timings, days off and daily patient limits",
}
# Every grant includes this: the rest of the dashboard is built on it.
BASE_PERMISSION = "queue.view"

PRESETS: dict[str, list[str]] = {
    "RECEPTIONIST": ["queue.view", "queue.call", "walkins.add", "patients.correct"],
    "COMPOUNDER": ["queue.view", "queue.call", "walkins.add", "bookings.outcome"],
}

SIGN_IN_MSG = "Please sign in."
CHOOSE_DOCTOR_MSG = "Choose which doctor you're working for."
NO_ACCESS_MSG = "You no longer have access to this doctor's OPD. Ask the doctor if you need it."
ROOT_ONLY_MSG = "Only the doctor can do this."


def normalize_permissions(permissions: list[str]) -> list[str]:
    """Known permissions only, in catalog order, always including the base one."""
    wanted = set(permissions) | {BASE_PERMISSION}
    return [p for p in PERMISSIONS if p in wanted]


# --- the signed-in account --------------------------------------------------------------


@dataclass
class Identity:
    """The signed-in account, before choosing which doctor a staff member works for."""

    session: Session
    doctor: Doctor | None = None
    staff: Staff | None = None


def get_identity(request: Request, db=Depends(get_db)) -> Identity:
    token = request.cookies.get(security.COOKIE_NAME)
    if not token:
        raise HTTPException(401, SIGN_IN_MSG)
    session = db.scalar(select(Session).where(Session.token_hash == security.hash_token(token)))
    if session is None:
        raise HTTPException(401, SIGN_IN_MSG)
    if session.expires_at <= datetime.now(timezone.utc):
        db.execute(delete(Session).where(Session.id == session.id))
        db.commit()
        raise HTTPException(401, SIGN_IN_MSG)
    if session.doctor_id is not None:
        return Identity(session, doctor=db.get(Doctor, session.doctor_id))
    return Identity(session, staff=db.get(Staff, session.staff_id))


@dataclass
class Actor:
    """Whoever is acting on a doctor's data, with what they're allowed to do."""

    doctor: Doctor  # whose data this is (the account owner)
    staff: Staff | None  # None: the doctor themselves (root)
    membership: StaffMembership | None
    permissions: frozenset[str]
    clinic_ids: frozenset[uuid.UUID] | None  # None: all the doctor's clinics

    @property
    def is_root(self) -> bool:
        return self.staff is None

    @property
    def kind(self) -> str:
        return "DOCTOR" if self.is_root else "STAFF"

    @property
    def id(self) -> uuid.UUID:
        return self.doctor.id if self.is_root else self.staff.id

    @property
    def name(self) -> str:
        return self.doctor.full_name if self.is_root else self.staff.full_name

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def slot_filter(self):
        """SQL condition: the DoctorClinic row is one this actor may work with."""
        cond = DoctorClinic.doctor_id == self.doctor.id
        if self.clinic_ids is not None:
            cond = and_(cond, DoctorClinic.id.in_(self.clinic_ids))
        return cond


def root_actor(doctor: Doctor) -> Actor:
    return Actor(doctor, None, None, frozenset(PERMISSIONS), None)


def staff_actor(membership: StaffMembership) -> Actor:
    return Actor(
        membership.doctor,
        membership.staff,
        membership,
        frozenset(normalize_permissions(membership.permissions)),
        frozenset(membership.doctor_clinic_ids),
    )


def memberships_of(db, staff: Staff) -> list[StaffMembership]:
    return list(
        db.scalars(
            select(StaffMembership).where(StaffMembership.staff_id == staff.id).order_by(StaffMembership.created_at)
        ).all()
    )


def get_actor(identity: Identity = Depends(get_identity), db=Depends(get_db)) -> Actor:
    if identity.doctor is not None:
        return root_actor(identity.doctor)

    session = identity.session
    if session.acting_doctor_id is None:
        # Working for only one doctor: pick them without asking.
        mine = memberships_of(db, identity.staff)
        if len(mine) != 1:
            raise HTTPException(409, CHOOSE_DOCTOR_MSG if mine else NO_ACCESS_MSG)
        session.acting_doctor_id = mine[0].doctor_id
        db.commit()

    membership = db.scalar(
        select(StaffMembership).where(
            StaffMembership.staff_id == identity.staff.id, StaffMembership.doctor_id == session.acting_doctor_id
        )
    )
    if membership is None:  # the doctor removed them
        session.acting_doctor_id = None
        db.commit()
        raise HTTPException(403, NO_ACCESS_MSG)
    return staff_actor(membership)


def require(permission: str):
    if permission not in PERMISSIONS:
        raise ValueError(f"Unknown permission {permission}")

    def check(actor: Actor = Depends(get_actor)) -> Actor:
        if not actor.can(permission):
            raise HTTPException(403, f"You don't have permission to do this ({PERMISSIONS[permission].lower()}). Ask the doctor if you need it.")
        return actor

    return check


def require_root(actor: Actor = Depends(get_actor)) -> Actor:
    if not actor.is_root:
        raise HTTPException(403, ROOT_ONLY_MSG)
    return actor


# Route parameter types, e.g. `actor: Can["opd.manage"]` isn't possible in Python, so:
CurrentIdentity = Annotated[Identity, Depends(get_identity)]
AnyActor = Annotated[Actor, Depends(get_actor)]
Root = Annotated[Actor, Depends(require_root)]
CanView = Annotated[Actor, Depends(require("queue.view"))]
CanCall = Annotated[Actor, Depends(require("queue.call"))]
CanManageOpd = Annotated[Actor, Depends(require("opd.manage"))]
CanAddWalkIns = Annotated[Actor, Depends(require("walkins.add"))]
CanSetOutcome = Annotated[Actor, Depends(require("bookings.outcome"))]
CanManageTests = Annotated[Actor, Depends(require("tests.manage"))]
CanViewRecords = Annotated[Actor, Depends(require("records.view"))]
CanCorrect = Annotated[Actor, Depends(require("patients.correct"))]
CanManageSchedule = Annotated[Actor, Depends(require("schedule.manage"))]


# --- the activity log --------------------------------------------------------------------


def audit(db, actor: Actor, action: str, target: str | None = None, **detail) -> None:
    """Record an action in the doctor's activity log. Added to the caller's transaction."""
    db.add(
        AuditEntry(
            doctor_id=actor.doctor.id,
            actor_kind=actor.kind,
            actor_id=actor.id,
            actor_name=actor.name,
            action=action,
            target=target,
            detail=detail or None,
        )
    )
