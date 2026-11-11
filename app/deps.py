from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app import security
from app.db import get_db
from app.models import Doctor, Session

SIGN_IN_MSG = "Please sign in."

Db = Annotated[DbSession, Depends(get_db)]


def get_current_doctor(request: Request, db: Db) -> Doctor:
    """The signed-in doctor, from the session cookie. One indexed query (sessions → doctors)."""
    token = request.cookies.get(security.COOKIE_NAME)
    if not token:
        raise HTTPException(401, SIGN_IN_MSG)

    row = db.execute(
        select(Session.id, Session.expires_at, Doctor)
        .join(Doctor, Doctor.id == Session.doctor_id)
        .where(Session.token_hash == security.hash_token(token))
    ).first()
    if row is None:
        raise HTTPException(401, SIGN_IN_MSG)

    session_id, expires_at, doctor = row
    if expires_at <= datetime.now(timezone.utc):
        db.execute(delete(Session).where(Session.id == session_id))
        db.commit()
        raise HTTPException(401, SIGN_IN_MSG)
    return doctor


CurrentDoctor = Annotated[Doctor, Depends(get_current_doctor)]
