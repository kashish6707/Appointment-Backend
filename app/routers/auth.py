from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request, Response
from psycopg.errors import UniqueViolation
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app import profile, security
from app.config import get_settings
from app.access import CurrentIdentity, memberships_of
from app.deps import Db
from app.models import Doctor, LoginAttempt, Session, Staff, StaffMembership
from app.schemas import InviteAcceptIn, LoginBody, LoginPhone, RegisterBody, RegisterPhone, WorkplaceIn

router = APIRouter(prefix="/api/auth")

DUPLICATE_MSG = "An account with this phone number or email already exists. Sign in instead."
BAD_LOGIN_MSG = "That mobile number, email or password is wrong. Check and try again."
TOO_MANY_MSG = "Too many attempts. Wait a few minutes and try again."

RATE_WINDOW = timedelta(minutes=15)
MAX_FAILS_PER_IDENTIFIER = 5
MAX_FAILS_PER_IP = 20


def client_ip(request: Request) -> str:
    # The Next.js proxy sets X-Forwarded-For; its first value is the browser.
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    ip = forwarded or (request.client.host if request.client else "unknown")
    return ip[:64]


def _doctor_json(doctor_id, full_name: str) -> dict:
    return {"doctor": {"id": str(doctor_id), "fullName": full_name}}


@router.post("/doctor/register", status_code=201)
def register(body: Annotated[RegisterBody, Body(discriminator="method")], db: Db):
    is_phone = isinstance(body, RegisterPhone)
    # A phone number or email signs in to one account only, doctor or staff.
    staff_column = Staff.phone if is_phone else Staff.email
    if db.scalar(select(Staff.id).where(staff_column == (body.phone if is_phone else body.email))):
        raise HTTPException(409, DUPLICATE_MSG)
    doctor = Doctor(
        full_name=body.full_name,
        phone=body.phone if is_phone else None,
        email=None if is_phone else body.email,
        password_hash=security.hash_password(body.password),
    )
    profile.apply(doctor, body.profile)
    db.add(doctor)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        profile.delete_photo(doctor.photo_key)  # saved before the insert failed
        if isinstance(e.orig, UniqueViolation):
            raise HTTPException(409, DUPLICATE_MSG) from None
        raise
    return {"id": str(doctor.id)}


def _recent_failures(db: DbSession, column, value: str) -> int:
    cutoff = datetime.now(timezone.utc) - RATE_WINDOW
    return db.scalar(
        select(func.count())
        .select_from(LoginAttempt)
        .where(column == value, LoginAttempt.succeeded.is_(False), LoginAttempt.created_at > cutoff)
    )


def _start_session(db: DbSession, response: Response, **owner) -> None:
    token = security.new_session_token()
    db.add(
        Session(
            token_hash=security.hash_token(token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=get_settings().session_ttl_days),
            **owner,
        )
    )
    db.commit()
    response.headers.append("set-cookie", security.session_cookie(token))


@router.post("/login")
@router.post("/doctor/login")
def login(body: Annotated[LoginBody, Body(discriminator="method")], request: Request, response: Response, db: Db):
    identifier = body.phone if isinstance(body, LoginPhone) else body.email
    ip = client_ip(request)

    if (
        _recent_failures(db, LoginAttempt.identifier, identifier) >= MAX_FAILS_PER_IDENTIFIER
        or _recent_failures(db, LoginAttempt.ip, ip) >= MAX_FAILS_PER_IP
    ):
        db.add(LoginAttempt(identifier=identifier, ip=ip, succeeded=False))
        db.commit()
        raise HTTPException(429, TOO_MANY_MSG)

    by_phone = isinstance(body, LoginPhone)
    doctor = db.scalar(select(Doctor).where((Doctor.phone if by_phone else Doctor.email) == identifier))
    staff = None
    if doctor is None:
        staff = db.scalar(select(Staff).where((Staff.phone if by_phone else Staff.email) == identifier))
    account = doctor or staff
    # Always run a verify, even for unknown accounts (or staff who haven't set a password yet),
    # so timing is the same.
    ok = security.verify_password(account.password_hash if account else None, body.password)

    db.add(LoginAttempt(identifier=identifier, ip=ip, succeeded=ok))
    if not ok:
        db.commit()
        raise HTTPException(401, BAD_LOGIN_MSG)

    if security.needs_rehash(account.password_hash):
        account.password_hash = security.hash_password(body.password)

    if doctor is not None:
        _start_session(db, response, doctor_id=doctor.id)
        return _doctor_json(doctor.id, doctor.full_name)
    _start_session(db, response, staff_id=staff.id)
    return {"staff": {"id": str(staff.id), "fullName": staff.full_name}}


@router.get("/me")
def me(identity: CurrentIdentity):
    if identity.doctor is not None:
        return _doctor_json(identity.doctor.id, identity.doctor.full_name)
    return {"staff": {"id": str(identity.staff.id), "fullName": identity.staff.full_name}}


# --- staff: invites and choosing a doctor ---------------------------------------------

INVITE_INVALID = "This invite link isn't valid any more. Ask the doctor to send you a new one."


def _invited(db: DbSession, token: str, *, lock: bool = False) -> Staff:
    query = select(Staff).where(Staff.invite_token_hash == security.hash_token(token), Staff.password_hash.is_(None))
    staff = db.scalar(query.with_for_update() if lock else query)
    if staff is None or staff.invite_expires_at is None or staff.invite_expires_at <= datetime.now(timezone.utc):
        raise HTTPException(404, INVITE_INVALID)
    return staff


@router.get("/staff/invite/{token}")
def invite_details(token: str, db: Db):
    staff = _invited(db, token)
    return {
        "fullName": staff.full_name,
        "phone": staff.phone,
        "email": staff.email,
        "doctors": [m.doctor.full_name for m in memberships_of(db, staff)],
        "expiresAt": staff.invite_expires_at.isoformat(),
    }


@router.post("/staff/invite/{token}")
def accept_invite(token: str, body: InviteAcceptIn, response: Response, db: Db):
    """Set a password (the link works once) and sign in."""
    staff = _invited(db, token, lock=True)
    staff.password_hash = security.hash_password(body.password)
    staff.invite_token_hash = None
    staff.invite_expires_at = None
    _start_session(db, response, staff_id=staff.id)
    return {"staff": {"id": str(staff.id), "fullName": staff.full_name}}


@router.post("/staff/workplace", status_code=204)
def choose_workplace(body: WorkplaceIn, identity: CurrentIdentity, db: Db):
    """A staff member working for several doctors picks whose OPD this browser works on."""
    if identity.staff is None:
        raise HTTPException(409, "Only staff accounts work for a doctor.")
    allowed = db.scalar(
        select(StaffMembership.id).where(
            StaffMembership.staff_id == identity.staff.id, StaffMembership.doctor_id == body.doctor_id
        )
    )
    if allowed is None:
        raise HTTPException(404, "You don't work for this doctor on Clinically.")
    identity.session.acting_doctor_id = body.doctor_id
    db.commit()
    return Response(status_code=204)


@router.post("/logout", status_code=204)
def logout(request: Request, db: Db):
    token = request.cookies.get(security.COOKIE_NAME)
    if token:
        db.execute(delete(Session).where(Session.token_hash == security.hash_token(token)))
        db.commit()
    response = Response(status_code=204)
    response.headers.append("set-cookie", security.cleared_cookie())
    return response
