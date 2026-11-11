"""Request bodies. Error messages here are shown to the doctor as is."""

import re
import uuid
from datetime import date
from typing import Annotated, Literal, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool

# [0-9] rather than \d: Python's \d also matches non-ASCII digits.
PHONE_RE = re.compile(r"[6-9][0-9]{9}")
EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}")

NAME_MSG = "Enter your full name (at least 3 characters)."
PHONE_MSG = "Enter a 10-digit mobile number starting with 6, 7, 8 or 9."
EMAIL_MSG = "Enter a valid email address, like name@example.com."
PASSWORD_MSG = "Use a password of at least 8 characters with at least one letter and one number."
LOGIN_PASSWORD_MSG = "Enter your password."
METHOD_MSG = "Choose whether to use your mobile number or email."

PATIENT_NAME_MSG = "Enter the patient's name (at least 2 characters)."
AGE_MSG = "Enter the patient's age in years, from 0 to 120."
GENDER_MSG = "Choose the patient's gender: male, female or other."
HEIGHT_MSG = "Enter height in centimetres, from 30 to 250."
WEIGHT_MSG = "Enter weight in kilograms, from 1 to 300."
DATE_MSG = "Choose a date in the format YYYY-MM-DD."
VISIT_MSG = "Choose the visit type: new, follow-up or report review."
CONSENT_MSG = "Say whether the doctor may see the patient's records (true or false)."
DOCTOR_CLINIC_MSG = "Choose a doctor and clinic."
CODE_MSG = "Add the tracking code from your booking link."
REASON_MSG = "Keep the reason under 300 characters."

# Message for a field that is missing or has the wrong type, keyed by its JSON name.
FIELD_MSGS = {
    "fullName": NAME_MSG,
    "phone": PHONE_MSG,
    "email": EMAIL_MSG,
    "password": LOGIN_PASSWORD_MSG,
    "method": METHOD_MSG,
    "name": PATIENT_NAME_MSG,
    "age": AGE_MSG,
    "gender": GENDER_MSG,
    "heightCm": HEIGHT_MSG,
    "weightKg": WEIGHT_MSG,
    "date": DATE_MSG,
    "visitType": VISIT_MSG,
    "recordsConsent": CONSENT_MSG,
    "doctorClinicId": DOCTOR_CLINIC_MSG,
    "patient": "Add the patient's details.",
    "code": CODE_MSG,
    "reason": REASON_MSG,
    "status": "Choose a status: done, sent for tests or no-show.",
    "razorpayOrderId": "The payment details are incomplete. Please try again.",
    "razorpayPaymentId": "The payment details are incomplete. Please try again.",
    "razorpaySignature": "The payment details are incomplete. Please try again.",
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


# --- bookings -------------------------------------------------------------------


def _patient_name(v: object) -> str:
    if not isinstance(v, str):
        raise ValueError(PATIENT_NAME_MSG)
    v = " ".join(v.split())  # trim and collapse inner spaces, so "Ramesh  Kumar" matches later
    if len(v) < 2:
        raise ValueError(PATIENT_NAME_MSG)
    if len(v) > 100:
        raise ValueError("Keep the patient's name under 100 characters.")
    return v


def _age(v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 120:
        raise ValueError(AGE_MSG)
    return v


def _range(lo: float, hi: float, msg: str):
    def check(v: object) -> float | None:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(msg)
        return round(float(v), 1)

    return check


def _date(v: object) -> date:
    if not isinstance(v, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
        raise ValueError(DATE_MSG)
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ValueError(DATE_MSG) from None


def _reason(v: object) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or len(v) > 300:
        raise ValueError(REASON_MSG)
    return v.strip() or None


PatientName = Annotated[str, BeforeValidator(_patient_name)]
Age = Annotated[int, BeforeValidator(_age)]
Gender = Literal["MALE", "FEMALE", "OTHER"]
HeightCm = Annotated[float | None, BeforeValidator(_range(30, 250, HEIGHT_MSG))]
WeightKg = Annotated[float | None, BeforeValidator(_range(1, 300, WEIGHT_MSG))]
BookingDate = Annotated[date, BeforeValidator(_date)]
VisitType = Literal["NEW", "FOLLOW_UP", "REPORT_REVIEW"]
Reason = Annotated[str | None, BeforeValidator(_reason)]


class PatientIn(_Body):
    name: PatientName
    phone: Phone
    age: Age
    gender: Gender
    height_cm: HeightCm = Field(None, alias="heightCm")
    weight_kg: WeightKg = Field(None, alias="weightKg")


class BookingIn(_Body):
    doctor_clinic_id: uuid.UUID = Field(alias="doctorClinicId")
    date: BookingDate
    visit_type: VisitType = Field(alias="visitType")
    patient: PatientIn
    records_consent: StrictBool = Field(alias="recordsConsent")


class CancelIn(_Body):
    code: str = Field(min_length=1, max_length=100)
    reason: Reason = None


class PaymentConfirmIn(_Body):
    """Razorpay Checkout's success handler result, plus the patient's tracking code."""

    code: str = Field(min_length=1, max_length=100)
    razorpay_order_id: str = Field(alias="razorpayOrderId", min_length=1, max_length=100)
    razorpay_payment_id: str = Field(alias="razorpayPaymentId", min_length=1, max_length=100)
    razorpay_signature: str = Field(alias="razorpaySignature", min_length=1, max_length=200)


# --- doctor: queue ----------------------------------------------------------------


def _optional_phone(v: object) -> str | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return _phone(v)


class WalkInIn(_Body):
    name: PatientName
    phone: Annotated[str | None, BeforeValidator(_optional_phone)] = None
    age: Age
    gender: Gender
    visit_type: VisitType = Field("NEW", alias="visitType")


class BookingStatusIn(_Body):
    status: Literal["DONE", "SENT_FOR_TESTS", "NO_SHOW"]
