"""Request bodies. Error messages here are shown to the doctor as is."""

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

# [0-9] rather than \d: Python's \d also matches non-ASCII digits.
PHONE_RE = re.compile(r"[6-9][0-9]{9}")
EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}")

NAME_MSG = "Enter your full name (at least 3 characters)."
PHONE_MSG = "Enter a 10-digit mobile number starting with 6, 7, 8 or 9."
EMAIL_MSG = "Enter a valid email address, like name@example.com."
PASSWORD_MSG = "Use a password of at least 8 characters with at least one letter and one number."
LOGIN_PASSWORD_MSG = "Enter your password."
METHOD_MSG = "Choose whether to use your mobile number or email."

# Message for a field that is missing or has the wrong type, keyed by its JSON name.
FIELD_MSGS = {
    "fullName": NAME_MSG,
    "phone": PHONE_MSG,
    "email": EMAIL_MSG,
    "password": LOGIN_PASSWORD_MSG,
    "method": METHOD_MSG,
}

MAX_PASSWORD = 128


def _full_name(v: object) -> str:
    if not isinstance(v, str) or len(v.strip()) < 3:
        raise ValueError(NAME_MSG)
    v = v.strip()
    if len(v) > 100:
        raise ValueError("Full name must be 100 characters or fewer.")
    return v


def _phone(v: object) -> str:
    if not isinstance(v, str) or not PHONE_RE.fullmatch(v.strip()):
        raise ValueError(PHONE_MSG)
    return v.strip()


def _email(v: object) -> str:
    if not isinstance(v, str) or len(v) > 254 or not EMAIL_RE.fullmatch(v.strip()):
        raise ValueError(EMAIL_MSG)
    return v.strip().lower()


def _new_password(v: object) -> str:
    if (
        not isinstance(v, str)
        or len(v) < 8
        or not re.search(r"[A-Za-z]", v)
        or not re.search(r"[0-9]", v)
    ):
        raise ValueError(PASSWORD_MSG)
    if len(v) > MAX_PASSWORD:
        raise ValueError(f"Password must be {MAX_PASSWORD} characters or fewer.")
    return v


def _password(v: object) -> str:
    if not isinstance(v, str) or not v or len(v) > MAX_PASSWORD:
        raise ValueError(LOGIN_PASSWORD_MSG)
    return v


FullName = Annotated[str, BeforeValidator(_full_name)]
Phone = Annotated[str, BeforeValidator(_phone)]
Email = Annotated[str, BeforeValidator(_email)]
NewPassword = Annotated[str, BeforeValidator(_new_password)]
Password = Annotated[str, BeforeValidator(_password)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterPhone(_Body):
    full_name: FullName = Field(alias="fullName")
    method: Literal["phone"]
    phone: Phone
    password: NewPassword


class RegisterEmail(_Body):
    full_name: FullName = Field(alias="fullName")
    method: Literal["email"]
    email: Email
    password: NewPassword


class LoginPhone(_Body):
    method: Literal["phone"]
    phone: Phone
    password: Password


class LoginEmail(_Body):
    method: Literal["email"]
    email: Email
    password: Password


RegisterBody = Union[RegisterPhone, RegisterEmail]
LoginBody = Union[LoginPhone, LoginEmail]
