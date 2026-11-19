"""Patient accounts: sign up, sign in, forgot password (email), family profiles with health
details, their prescriptions and reports, and their bookings.

A patient account has its own cookie (clinically_patient), separate from doctors' and staff's.
Every route below checks that the profile, record or booking belongs to the signed-in account;
anything else answers 404, the same as something that doesn't exist.

Phone numbers aren't verified yet (no SMS codes), so a new account starts empty: it never picks
up bookings or records made earlier with the same number.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from psycopg.errors import UniqueViolation
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app import bookings, email, security
from app.config import get_settings
from app.db import get_db
from app.deps import Db
from app.models import (
    Booking,
    DoctorClinic,
    LoginAttempt,
    MedicalRecord,
    OpdSession,
    PasswordReset,
    Patient,
    PatientAccount,
    PatientSession,
)
from app.routers.auth import (
    BAD_LOGIN_MSG,
    DUPLICATE_MSG,
    MAX_FAILS_PER_IDENTIFIER,
    MAX_FAILS_PER_IP,
    TOO_MANY_MSG,
    _recent_failures,
    client_ip,
)
from app.routers.dashboard import record_response
from app.schemas import (
    ForgotPasswordIn,
    LoginBody,
    LoginPhone,
    MemberIn,
    PatientRegisterBody,
    PatientRegisterPhone,
    RecordIn,
    ResetPasswordIn,
)
from app.serializers import iso
from app.storage import get_record_storage
from app.timeutil import now_ist

router = APIRouter(prefix="/api/patient")

SIGN_IN_MSG = "Please sign in."
MEMBER_NOT_FOUND = "We couldn't find that profile in your account."
RECORD_NOT_FOUND = "We couldn't find that file."
MAX_MEMBERS = 8
MAX_RECORDS_PER_MEMBER = 100
RESET_HOURS = 1
MAX_RESETS_PER_HOUR = 3
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}


# --- who is signed in -------------------------------------------------------------------


def get_account(request: Request, db=Depends(get_db)) -> PatientAccount:
    token = request.cookies.get(security.PATIENT_COOKIE)
    if not token:
        raise HTTPException(401, SIGN_IN_MSG)
    session = db.scalar(select(PatientSession).where(PatientSession.token_hash == security.hash_token(token)))
    if session is None:
        raise HTTPException(401, SIGN_IN_MSG)
    if session.expires_at <= datetime.now(timezone.utc):
        db.execute(delete(PatientSession).where(PatientSession.id == session.id))
        db.commit()
        raise HTTPException(401, SIGN_IN_MSG)
    return db.get(PatientAccount, session.account_id)


def optional_account(request: Request, db=Depends(get_db)) -> PatientAccount | None:
    """The signed-in patient, or None (e.g. guest booking)."""
    try:
        return get_account(request, db)
    except HTTPException:
        return None


Account = Annotated[PatientAccount, Depends(get_account)]


def _start_session(db, response: Response, account: PatientAccount) -> None:
    token = security.new_session_token()
    db.add(
        PatientSession(
            account_id=account.id,
            token_hash=security.hash_token(token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=get_settings().session_ttl_days),
        )
    )
    db.commit()
    response.headers.append("set-cookie", security.patient_cookie(token))


# --- shapes -----------------------------------------------------------------------------


def _age_from(dob: date | None) -> int | None:
    if dob is None:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def member_json(p: Patient, record_count: int = 0) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "relation": p.relation,
        "phone": p.phone,
        "dateOfBirth": p.date_of_birth.isoformat() if p.date_of_birth else None,
        "age": _age_from(p.date_of_birth) if p.date_of_birth else p.age,
        "gender": p.gender,
        "heightCm": float(p.height_cm) if p.height_cm is not None else None,
        "weightKg": float(p.weight_kg) if p.weight_kg is not None else None,
        "bloodGroup": p.blood_group,
        "allergies": p.allergies,
        "conditions": p.conditions,
        "surgeries": p.surgeries,
        "medicines": p.medicines,
        "emergencyName": p.emergency_name,
        "emergencyRelation": p.emergency_relation,
        "emergencyPhone": p.emergency_phone,
        "addressArea": p.address_area,
        "addressCity": p.address_city,
        "recordsConsent": p.records_consent_at is not None,
        "recordCount": record_count,
        "updatedAt": iso(p.profile_updated_at),
    }


def _members(db, account: PatientAccount) -> list[Patient]:
    # Self first, then family in the order they were added.
    return list(
        db.scalars(
            select(Patient)
            .where(Patient.account_id == account.id)
            .order_by((Patient.relation != "SELF"), Patient.created_at)
        )
    )


def _member(db, account: PatientAccount, member_id: uuid.UUID, *, lock: bool = False) -> Patient:
    query = select(Patient).where(Patient.id == member_id, Patient.account_id == account.id)
    if lock:
        query = query.with_for_update()
    member = db.scalar(query)
    if member is None:
        raise HTTPException(404, MEMBER_NOT_FOUND)
    return member


def _me(db, account: PatientAccount) -> dict:
    members = _members(db, account)
    counts = dict(
        db.execute(
            select(MedicalRecord.patient_id, func.count())
            .where(MedicalRecord.patient_id.in_([m.id for m in members]))
            .group_by(MedicalRecord.patient_id)
        ).all()
    ) if members else {}
    return {
        "account": {"id": str(account.id), "fullName": account.full_name, "phone": account.phone, "email": account.email},
        "members": [member_json(m, counts.get(m.id, 0)) for m in members],
    }


# --- sign up, sign in, sign out ---------------------------------------------------------


@router.post("/auth/register", status_code=201)
def register(body: Annotated[PatientRegisterBody, Body(discriminator="method")], response: Response, db: Db):
    is_phone = isinstance(body, PatientRegisterPhone)
    account = PatientAccount(
        full_name=body.full_name,
        phone=body.phone if is_phone else None,
        email=None if is_phone else body.email,
        password_hash=security.hash_password(body.password),
    )
    db.add(account)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        if isinstance(e.orig, UniqueViolation):
            raise HTTPException(409, DUPLICATE_MSG) from None
        raise
    # Their own profile, filled in later (age and gender are asked at the first booking at the latest).
    db.add(Patient(name=body.full_name, phone=account.phone, account_id=account.id, relation="SELF"))
    db.flush()
    _start_session(db, response, account)
    return _me(db, account)


@router.post("/auth/login")
def login(body: Annotated[LoginBody, Body(discriminator="method")], request: Request, response: Response, db: Db):
    by_phone = isinstance(body, LoginPhone)
    value = body.phone if by_phone else body.email
    identifier = f"patient:{value}"  # counted apart from doctor sign-ins with the same number
    ip = client_ip(request)
    if (
        _recent_failures(db, LoginAttempt.identifier, identifier) >= MAX_FAILS_PER_IDENTIFIER
        or _recent_failures(db, LoginAttempt.ip, ip) >= MAX_FAILS_PER_IP
    ):
        db.add(LoginAttempt(identifier=identifier, ip=ip, succeeded=False))
        db.commit()
        raise HTTPException(429, TOO_MANY_MSG)
    account = db.scalar(select(PatientAccount).where((PatientAccount.phone if by_phone else PatientAccount.email) == value))
    ok = security.verify_password(account.password_hash if account else None, body.password)
    db.add(LoginAttempt(identifier=identifier, ip=ip, succeeded=ok))
    if not ok:
        db.commit()
        raise HTTPException(401, BAD_LOGIN_MSG)
    if security.needs_rehash(account.password_hash):
        account.password_hash = security.hash_password(body.password)
    _start_session(db, response, account)
    return _me(db, account)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, db: Db):
    token = request.cookies.get(security.PATIENT_COOKIE)
    if token:
        db.execute(delete(PatientSession).where(PatientSession.token_hash == security.hash_token(token)))
        db.commit()
    response.headers.append("set-cookie", security.cleared_patient_cookie())


@router.get("/me")
def me(account: Account, db: Db):
    return _me(db, account)


# --- forgot password (email accounts) ----------------------------------------------------

FORGOT_DONE = "If an account uses this email, we've sent a link to set a new password. It works for 1 hour."


@router.post("/auth/forgot")
def forgot_password(body: ForgotPasswordIn, db: Db):
    """Always the same answer, so nobody can find out which emails have accounts."""
    account = db.scalar(select(PatientAccount).where(PatientAccount.email == body.email))
    if account is not None:
        hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        recent = db.scalar(
            select(func.count()).where(PasswordReset.account_id == account.id, PasswordReset.created_at > hour_ago)
        )
        if recent < MAX_RESETS_PER_HOUR:
            token = security.new_session_token()
            db.add(
                PasswordReset(
                    account_id=account.id,
                    token_hash=security.hash_token(token),
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=RESET_HOURS),
                )
            )
            db.commit()
            link = f"{get_settings().site_url.rstrip('/')}/reset-password?token={token}"
            try:
                email.send(
                    account.email,
                    "Set a new Clinically password",
                    f"Hello {account.full_name},\n\nOpen this link to set a new password for Clinically:\n{link}\n\n"
                    f"It works once, for {RESET_HOURS} hour. If you didn't ask for this, ignore this email.",
                )
            except email.EmailError:
                raise HTTPException(503, "We couldn't send the email just now. Try again in a few minutes.") from None
    return {"message": FORGOT_DONE}


@router.post("/auth/reset")
def reset_password(body: ResetPasswordIn, response: Response, db: Db):
    reset = db.scalar(
        select(PasswordReset).where(PasswordReset.token_hash == security.hash_token(body.token)).with_for_update()
    )
    if reset is None or reset.used_at is not None or reset.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(400, "This link has expired or was already used. Ask for a new one.")
    account = db.get(PatientAccount, reset.account_id)
    account.password_hash = security.hash_password(body.password)
    reset.used_at = datetime.now(timezone.utc)
    # Sign out everywhere else: whoever knew the old password loses access.
    db.execute(delete(PatientSession).where(PatientSession.account_id == account.id))
    _start_session(db, response, account)
    return _me(db, account)


# --- family profiles --------------------------------------------------------------------


def _apply(member: Patient, body: MemberIn, account: PatientAccount) -> None:
    member.name = body.name
    member.relation = "SELF" if member.relation == "SELF" else body.relation
    member.phone = body.phone or account.phone
    member.date_of_birth = body.date_of_birth
    member.age = _age_from(body.date_of_birth) if body.date_of_birth else body.age
    member.gender = body.gender
    member.height_cm = body.height_cm
    member.weight_kg = body.weight_kg
    member.blood_group = body.blood_group
    member.allergies = body.allergies
    member.conditions = body.conditions
    member.surgeries = body.surgeries
    member.medicines = body.medicines
    member.emergency_name = body.emergency_name
    member.emergency_relation = body.emergency_relation
    member.emergency_phone = body.emergency_phone
    member.address_area = body.address_area
    member.address_city = body.address_city
    if body.records_consent is True and member.records_consent_at is None:
        member.records_consent_at = now_ist()
    elif body.records_consent is False:
        member.records_consent_at = None
    member.profile_updated_at = datetime.now(timezone.utc)


@router.post("/members", status_code=201)
def add_member(body: MemberIn, account: Account, db: Db):
    if body.relation == "SELF":
        raise HTTPException(400, "Your own profile is already in your account. Choose how this person is related to you.")
    count = db.scalar(select(func.count()).select_from(Patient).where(Patient.account_id == account.id))
    if count >= MAX_MEMBERS:
        raise HTTPException(409, f"An account can have up to {MAX_MEMBERS} people.")
    member = Patient(account_id=account.id, relation=body.relation)
    _apply(member, body, account)
    db.add(member)
    db.commit()
    return member_json(member)


@router.put("/members/{member_id}")
def update_member(member_id: uuid.UUID, body: MemberIn, account: Account, db: Db):
    member = _member(db, account, member_id, lock=True)
    if member.relation != "SELF" and body.relation == "SELF":
        raise HTTPException(400, "Only your own profile can be \"Self\".")
    _apply(member, body, account)
    db.commit()
    count = db.scalar(select(func.count()).where(MedicalRecord.patient_id == member.id))
    return member_json(member, count)


@router.delete("/members/{member_id}", status_code=204)
def remove_member(member_id: uuid.UUID, account: Account, db: Db):
    """Takes a family member out of the account. Files they uploaded are deleted; clinics keep
    their own visit history, but no longer see this person's records."""
    member = _member(db, account, member_id, lock=True)
    if member.relation == "SELF":
        raise HTTPException(400, "You can't remove your own profile.")
    records = db.scalars(select(MedicalRecord).where(MedicalRecord.patient_id == member.id, MedicalRecord.added_by == "PATIENT")).all()
    keys = [r.file_key for r in records]
    for r in records:
        db.delete(r)
    member.account_id = None
    member.records_consent_at = None
    db.commit()
    storage = get_record_storage()
    for key in keys:
        storage.delete(key)


# --- prescriptions and reports ----------------------------------------------------------


def _record_json(r: MedicalRecord) -> dict:
    return {
        "id": str(r.id),
        "kind": r.kind,
        "title": r.title,
        "date": r.date.isoformat(),
        "addedBy": r.added_by,
        "contentType": r.content_type,
        "fileUrl": f"/api/patient/records/{r.id}/file",
    }


@router.get("/members/{member_id}/records")
def list_records(member_id: uuid.UUID, account: Account, db: Db):
    member = _member(db, account, member_id)
    rows = db.scalars(
        select(MedicalRecord)
        .where(MedicalRecord.patient_id == member.id)
        .order_by(MedicalRecord.date.desc(), MedicalRecord.created_at.desc())
    ).all()
    return {"records": [_record_json(r) for r in rows]}


@router.post("/members/{member_id}/records", status_code=201)
def add_record(member_id: uuid.UUID, body: RecordIn, account: Account, db: Db):
    member = _member(db, account, member_id)
    count = db.scalar(select(func.count()).where(MedicalRecord.patient_id == member.id))
    if count >= MAX_RECORDS_PER_MEMBER:
        raise HTTPException(409, f"A profile can hold up to {MAX_RECORDS_PER_MEMBER} files. Remove old ones first.")
    data, content_type = body.file
    key = f"records/{member.id}/{uuid.uuid4().hex}{EXTENSIONS[content_type]}"
    get_record_storage().put(key, data, content_type)
    record = MedicalRecord(
        patient_id=member.id, kind=body.kind, title=body.title, date=body.date, added_by="PATIENT",
        file_key=key, content_type=content_type,
    )
    db.add(record)
    try:
        db.commit()
    except Exception:
        get_record_storage().delete(key)
        raise
    return _record_json(record)


def _own_record(db, account: PatientAccount, record_id: uuid.UUID) -> MedicalRecord:
    record = db.scalar(
        select(MedicalRecord)
        .join(Patient, MedicalRecord.patient_id == Patient.id)
        .where(MedicalRecord.id == record_id, Patient.account_id == account.id)
    )
    if record is None:
        raise HTTPException(404, RECORD_NOT_FOUND)
    return record


@router.get("/records/{record_id}/file")
def record_file(record_id: uuid.UUID, account: Account, db: Db):
    return record_response(_own_record(db, account, record_id))


@router.delete("/records/{record_id}", status_code=204)
def delete_record(record_id: uuid.UUID, account: Account, db: Db):
    record = _own_record(db, account, record_id)
    if record.added_by != "PATIENT":
        raise HTTPException(403, "This file was added by the clinic. Ask them to remove it.")
    key = record.file_key
    db.delete(record)
    db.commit()
    get_record_storage().delete(key)


# --- bookings ---------------------------------------------------------------------------


@router.get("/bookings")
def my_bookings(account: Account, db: Db):
    """Bookings for every profile in the account, newest day first."""
    rows = db.scalars(
        select(Booking)
        .join(Patient, Booking.patient_id == Patient.id)
        .join(OpdSession, Booking.session_id == OpdSession.id)
        .options(joinedload(Booking.session).joinedload(OpdSession.doctor_clinic).joinedload(DoctorClinic.doctor),
                 joinedload(Booking.session).joinedload(OpdSession.doctor_clinic).joinedload(DoctorClinic.clinic),
                 joinedload(Booking.patient))
        .where(Patient.account_id == account.id)
        .order_by(OpdSession.date.desc(), Booking.token)
        .limit(50)
    ).unique().all()
    return {
        "bookings": [
            {
                "bookingId": str(b.id),
                "token": b.token,
                "status": b.status,
                "date": b.session.date.isoformat(),
                "memberId": str(b.patient_id),
                "patientName": b.patient.name,
                "doctorName": b.session.doctor_clinic.doctor.full_name,
                "clinicName": b.session.doctor_clinic.clinic.name,
            }
            for b in rows
        ]
    }


@router.post("/bookings/{booking_id}/link")
def tracking_link(booking_id: uuid.UUID, account: Account, db: Db):
    """A tracking code for this device. Links shared earlier keep working."""
    booking = db.scalar(
        select(Booking).join(Patient, Booking.patient_id == Patient.id).where(Booking.id == booking_id, Patient.account_id == account.id)
    )
    if booking is None:
        raise HTTPException(404, bookings.BOOKING_NOT_FOUND)
    code = bookings.new_tracking_link(db, booking)
    db.commit()
    return {"bookingId": str(booking.id), "code": code}
