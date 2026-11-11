from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request, Response
from psycopg.errors import UniqueViolation
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app import security
from app.config import get_settings
from app.deps import CurrentDoctor, Db
from app.models import Doctor, LoginAttempt, Session
from app.schemas import LoginBody, LoginPhone, RegisterBody, RegisterPhone

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
    doctor = Doctor(
        full_name=body.full_name,
        phone=body.phone if is_phone else None,
        email=None if is_phone else body.email,
        password_hash=security.hash_password(body.password),
    )
    db.add(doctor)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
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

    column = Doctor.phone if isinstance(body, LoginPhone) else Doctor.email
    doctor = db.scalar(select(Doctor).where(column == identifier))
    # Always run a verify, even for unknown accounts, so timing is the same.
    ok = security.verify_password(doctor.password_hash if doctor else None, body.password)

    db.add(LoginAttempt(identifier=identifier, ip=ip, succeeded=ok))
    if not ok:
        db.commit()
        raise HTTPException(401, BAD_LOGIN_MSG)

    if security.needs_rehash(doctor.password_hash):
        doctor.password_hash = security.hash_password(body.password)

    token = security.new_session_token()
    db.add(
        Session(
            doctor_id=doctor.id,
            token_hash=security.hash_token(token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=get_settings().session_ttl_days),
        )
    )
    db.commit()
    response.headers.append("set-cookie", security.session_cookie(token))
    return _doctor_json(doctor.id, doctor.full_name)


@router.get("/me")
def me(doctor: CurrentDoctor):
    return _doctor_json(doctor.id, doctor.full_name)


@router.post("/logout", status_code=204)
def logout(request: Request, db: Db):
    token = request.cookies.get(security.COOKIE_NAME)
    if token:
        db.execute(delete(Session).where(Session.token_hash == security.hash_token(token)))
        db.commit()
    response = Response(status_code=204)
    response.headers.append("set-cookie", security.cleared_cookie())
    return response
