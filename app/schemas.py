"""Request bodies. Error messages here are shown to the doctor as is."""

import re
import uuid
from datetime import date
from typing import Annotated, Literal, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, model_validator

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
    "month": "Choose a month in the format YYYY-MM.",
    "q": "Keep the search under 100 characters.",
    "names": "Add 1 to 10 test names, each 2 to 100 characters.",
    "role": "Choose a role: receptionist or compounder.",
    "profile": "Add your professional details: experience, degrees, fee, registration number and photo.",
    "experienceYears": "Enter your years of experience, from 0 to 70.",
    "degrees": "Add at least one degree (up to 15).",
    "fee": "Enter your consultation fee in rupees, from 0 to 1,00,000.",
    "registrationNo": "Enter your medical registration number (3 to 30 letters, numbers, / . or -).",
    "medicalCouncil": "Keep the council name under 100 characters.",
    "photo": "Add a clear photo of yourself (JPEG, PNG or WebP, up to 2 MB).",
    "about": "Keep “About” under 1,500 characters.",
    "achievements": "Add up to 20 achievements, each 2 to 200 characters.",
    "expertise": "Add up to 20 areas of expertise, each 2 to 60 characters.",
    "specialisation": "Keep the specialisation under 80 characters.",
    "permissions": "Choose what this person can do.",
    "doctorClinicIds": "Choose at least one of your clinics.",
    "doctorId": "Choose a doctor.",
    "days": "Send all seven weekdays, each once.",
    "from": "Choose a date in the format YYYY-MM-DD.",
    "to": "Choose a date in the format YYYY-MM-DD.",
    "kind": "Choose whether the doctor is away or keeps different hours.",
    "limit": "Enter a daily patient limit from 1 to 500, or leave it empty for no limit.",
    "note": "Keep the note under 200 characters.",
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
    profile: "RegisterProfileIn"
    method: Literal["phone"]
    phone: Phone
    password: NewPassword


class RegisterEmail(_Body):
    full_name: FullName = Field(alias="fullName")
    profile: "RegisterProfileIn"
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
    # The desk confirmed adding a patient after the day's limit was reached.
    over_limit: StrictBool = Field(False, alias="overLimit")


class BookingStatusIn(_Body):
    status: Literal["DONE", "SENT_FOR_TESTS", "NO_SHOW"]


# --- doctor: tests advised ---------------------------------------------------------

TEST_NAMES_MSG = FIELD_MSGS["names"]
TEST_STATUS_MSG = "Choose a test status: done or not done."


def _test_names(v: object) -> list[str]:
    """Trimmed, without blanks or repeats (ignoring case), 1 to 10 names of 2-100 characters."""
    if not isinstance(v, list) or not all(isinstance(n, str) for n in v):
        raise ValueError(TEST_NAMES_MSG)
    names: list[str] = []
    for n in (" ".join(n.split()) for n in v):
        if n and n.lower() not in (x.lower() for x in names):
            names.append(n)
    if not 1 <= len(names) <= 10 or any(not 2 <= len(n) <= 100 for n in names):
        raise ValueError(TEST_NAMES_MSG)
    return names


def _test_status(v: object) -> str:
    if v not in ("PENDING", "DONE"):
        raise ValueError(TEST_STATUS_MSG)
    return v


class TestsIn(_Body):
    __test__ = False  # not a pytest class
    names: Annotated[list[str], BeforeValidator(_test_names)]


class TestStatusIn(_Body):
    __test__ = False
    status: Annotated[str, BeforeValidator(_test_status)]


# --- correcting patient details -------------------------------------------------------


class PatientCorrectionIn(_Body):
    """Only the fields sent are changed. heightCm/weightKg: null clears this visit's value."""

    name: PatientName = None
    age: Age = None
    gender: Gender = None
    height_cm: HeightCm = Field(None, alias="heightCm")
    weight_kg: WeightKg = Field(None, alias="weightKg")

    @model_validator(mode="after")
    def _something_to_change(self):
        if not self.model_fields_set:
            raise ValueError("Change at least one detail.")
        for key in ("name", "age", "gender"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(FIELD_MSGS[key])
        return self


# --- staff accounts ---------------------------------------------------------------------

STAFF_CONTACT_MSG = "Add the person's 10-digit mobile number or their email address."


def _optional_email(v: object) -> str | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return _email(v)


class StaffIn(_Body):
    full_name: FullName = Field(alias="fullName")
    phone: Annotated[str | None, BeforeValidator(_optional_phone)] = None
    email: Annotated[str | None, BeforeValidator(_optional_email)] = None
    role: Literal["RECEPTIONIST", "COMPOUNDER"]
    permissions: list[str] = Field(max_length=20)
    doctor_clinic_ids: list[uuid.UUID] = Field(alias="doctorClinicIds", min_length=1, max_length=50)

    @model_validator(mode="after")
    def _one_contact(self):
        if (self.phone is None) == (self.email is None):
            raise ValueError(STAFF_CONTACT_MSG)
        return self


class StaffUpdateIn(_Body):
    role: Literal["RECEPTIONIST", "COMPOUNDER"] = None
    permissions: list[str] = Field(None, max_length=20)
    doctor_clinic_ids: list[uuid.UUID] = Field(None, alias="doctorClinicIds", min_length=1, max_length=50)


class InviteAcceptIn(_Body):
    password: NewPassword


class WorkplaceIn(_Body):
    doctor_id: uuid.UUID = Field(alias="doctorId")


# --- OPD timings ------------------------------------------------------------------------

TIME_MSG = "Enter times as HH:MM, for example 10:00 or 17:30."
HOURS_MSG = "The end time must be after the start time."


def _hhmm(v: object):
    from datetime import time as _time

    if v is None:
        return None
    if isinstance(v, str) and re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", v.strip()):
        h, m = v.strip().split(":")
        return _time(int(h), int(m))
    raise ValueError(TIME_MSG)


def _limit(v: object) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 500:
        raise ValueError(FIELD_MSGS["limit"])
    return v


ClockTime = Annotated[object, BeforeValidator(_hhmm)]
Limit = Annotated[int | None, BeforeValidator(_limit)]


class WeekdayHoursIn(_Body):
    weekday: Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    open: StrictBool
    start: ClockTime = None
    end: ClockTime = None
    limit: Limit = None

    @model_validator(mode="after")
    def _hours(self):
        if self.open:
            if self.start is None or self.end is None:
                raise ValueError(f"Add start and end times for {self.weekday.title()}, or mark it as off.")
            if self.start >= self.end:
                raise ValueError(f"{self.weekday.title()}: {HOURS_MSG[0].lower()}{HOURS_MSG[1:]}")
        return self


class WeeklyHoursIn(_Body):
    days: list[WeekdayHoursIn] = Field(min_length=7, max_length=7)
    confirm_cancel: StrictBool = Field(False, alias="confirmCancel")

    @model_validator(mode="after")
    def _each_day_once(self):
        if len({d.weekday for d in self.days}) != 7:
            raise ValueError(FIELD_MSGS["days"])
        return self


class DateExceptionIn(_Body):
    """A day off (CLOSED) or different hours (HOURS) on one date, or each date from..to."""

    from_date: BookingDate = Field(alias="from")
    to_date: BookingDate = Field(None, alias="to")
    kind: Literal["CLOSED", "HOURS"]
    start: ClockTime = None
    end: ClockTime = None
    limit: Limit = None
    note: Annotated[str | None, BeforeValidator(lambda v: (v.strip() or None) if isinstance(v, str) else v)] = Field(None, max_length=200)
    confirm_cancel: StrictBool = Field(False, alias="confirmCancel")

    @model_validator(mode="after")
    def _valid(self):
        if self.to_date is not None and self.to_date < self.from_date:
            raise ValueError("The last date must be on or after the first date.")
        if self.kind == "HOURS":
            if self.start is None or self.end is None:
                raise ValueError("Add the start and end times for that day.")
            if self.start >= self.end:
                raise ValueError(HOURS_MSG)
        return self


# --- doctor profile ----------------------------------------------------------------------

import base64 as _base64
import binascii as _binascii

MAX_PHOTO_BYTES = 2 * 1024 * 1024
DEGREE_LEVELS = ("UG", "PG", "SUPER_SPECIALITY", "FELLOWSHIP", "DIPLOMA", "OTHER")
REG_NO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/.\- ]{1,28}[A-Za-z0-9]")


def _photo(v: object):
    """A data URL ("data:image/jpeg;base64,...") holding a real JPEG, PNG or WebP of at most 2 MB.

    The type is taken from the file's own first bytes, not from what the browser claims.
    Returns (bytes, content_type), or None when absent.
    """
    if v is None:
        return None
    msg = FIELD_MSGS["photo"]
    if not isinstance(v, str) or not v.startswith("data:image/") or ";base64," not in v[:40]:
        raise ValueError(msg)
    encoded = v.split(";base64,", 1)[1]
    if len(encoded) > MAX_PHOTO_BYTES * 4 // 3 + 4:
        raise ValueError(msg)
    try:
        data = _base64.b64decode(encoded, validate=True)
    except (_binascii.Error, ValueError):
        raise ValueError(msg) from None
    if data.startswith(b"\xff\xd8\xff"):
        kind = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "image/png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = "image/webp"
    else:
        raise ValueError(msg)
    if not 1000 <= len(data) <= MAX_PHOTO_BYTES:  # a few hundred bytes can't be a usable photo
        raise ValueError(msg)
    return data, kind


def _text(max_len: int, msg_key: str, min_len: int = 1):
    def check(v: object) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError(FIELD_MSGS[msg_key])
        v = " ".join(v.split()) if max_len <= 200 else v.strip()
        if not v:
            return None
        if not min_len <= len(v) <= max_len:
            raise ValueError(FIELD_MSGS[msg_key])
        return v

    return check


def _text_list(max_items: int, max_len: int, msg_key: str):
    def check(v: object) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(FIELD_MSGS[msg_key])
        items: list[str] = []
        for x in (" ".join(x.split()) for x in v):
            if x and x.lower() not in (i.lower() for i in items):
                items.append(x)
        if len(items) > max_items or any(not 2 <= len(x) <= max_len for x in items):
            raise ValueError(FIELD_MSGS[msg_key])
        return items

    return check


def _int_range(lo: int, hi: int, msg_key: str):
    def check(v: object) -> int:
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(FIELD_MSGS[msg_key])
        return v

    return check


def _reg_no(v: object) -> str:
    if not isinstance(v, str) or not REG_NO_RE.fullmatch(v.strip()):
        raise ValueError(FIELD_MSGS["registrationNo"])
    return " ".join(v.split()).upper()


class DegreeIn(_Body):
    """All optional here so a missing field gets a degree-specific message (below)."""

    level: str | None = None
    name: str | None = None
    institute: str | None = None
    year: object = None

    @model_validator(mode="after")
    def _valid(self):
        if self.level not in DEGREE_LEVELS:
            raise ValueError("Choose each degree's level: UG, PG, super speciality, fellowship, diploma or other.")
        name = " ".join((self.name or "").split()) if isinstance(self.name, str) else ""
        if not 2 <= len(name) <= 100:
            raise ValueError("Enter each degree's name, for example MBBS or MS (Orthopaedics).")
        self.name = name
        if self.institute is not None:
            inst = " ".join(self.institute.split())
            if len(inst) > 150:
                raise ValueError("Keep the institute name under 150 characters.")
            self.institute = inst or None
        if self.year is not None:
            from datetime import date as _date

            if isinstance(self.year, bool) or not isinstance(self.year, int) or not 1950 <= self.year <= _date.today().year:
                raise ValueError(f"Enter the year each degree was completed, from 1950 to {_date.today().year}.")
        return self


class ProfileIn(_Body):
    """The doctor's professional profile. Used to edit it: the photo may be left out to keep it."""

    specialisation: Annotated[str | None, BeforeValidator(_text(80, "specialisation", 2))] = None
    experience_years: Annotated[int, BeforeValidator(_int_range(0, 70, "experienceYears"))] = Field(alias="experienceYears")
    degrees: list[DegreeIn] = Field(min_length=1, max_length=15)
    fee: Annotated[int, BeforeValidator(_int_range(0, 100000, "fee"))]
    registration_no: Annotated[str, BeforeValidator(_reg_no)] = Field(alias="registrationNo")
    medical_council: Annotated[str | None, BeforeValidator(_text(100, "medicalCouncil", 2))] = Field(None, alias="medicalCouncil")
    photo: Annotated[object, BeforeValidator(_photo)] = None
    about: Annotated[str | None, BeforeValidator(_text(1500, "about"))] = None
    achievements: Annotated[list[str], BeforeValidator(_text_list(20, 200, "achievements"))] = []
    expertise: Annotated[list[str], BeforeValidator(_text_list(20, 60, "expertise"))] = []


class RegisterProfileIn(ProfileIn):
    @model_validator(mode="after")
    def _photo_required(self):
        if self.photo is None:
            raise ValueError(FIELD_MSGS["photo"])
        return self


RegisterPhone.model_rebuild()
RegisterEmail.model_rebuild()
