from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session as DbSession

from app.db import get_db

Db = Annotated[DbSession, Depends(get_db)]

# Who is signed in and what they may do lives in app/access.py.
