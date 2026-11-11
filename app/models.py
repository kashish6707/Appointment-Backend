import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Allowed values for the text "enum" columns. Stored as text + CHECK constraint so adding a
# value later is a one-line migration (Postgres native enums are awkward to change).
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
GENDERS = ("MALE", "FEMALE", "OTHER")
SESSION_STATUSES = ("NOT_STARTED", "IN_OPD", "ON_BREAK", "CLOSED")
VISIT_TYPES = ("NEW", "FOLLOW_UP", "REPORT_REVIEW")
BOOKING_SOURCES = ("ONLINE", "WALK_IN")
BOOKING_STATUSES = ("WAITING", "WITH_DOCTOR", "DONE", "SENT_FOR_TESTS", "NO_SHOW", "CANCELLED")
CANCELLED_BY = ("PATIENT", "CLINIC")
TEST_STATUSES = ("PENDING", "DONE")
RECORD_KINDS = ("PRESCRIPTION", "REPORT")
ADDED_BY = ("PATIENT", "CLINIC")


def one_of(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def fk(target: str, **kwargs) -> Mapped:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, ondelete="CASCADE"), index=True, **kwargs)


class Base(DeclarativeBase):
    pass


# --- auth -------------------------------------------------------------------


class Doctor(Base):
    __tablename__ = "doctors"
    __table_args__ = (
        CheckConstraint("phone IS NOT NULL OR email IS NOT NULL", name="doctors_phone_or_email"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    full_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text, unique=True)
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    # Profile: filled in after registration (registration only asks for name + contact).
    department: Mapped[str | None] = mapped_column(Text)
    qualification: Mapped[str | None] = mapped_column(Text)
    experience_years: Mapped[int | None] = mapped_column(SmallInteger)
    registration_no: Mapped[str | None] = mapped_column(Text)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = created_at()

    clinics: Mapped[list["DoctorClinic"]] = relationship(back_populates="doctor")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    doctor_id: Mapped[uuid.UUID] = fk("doctors.id")
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_identifier_created_at", "identifier", "created_at"),
        Index("ix_login_attempts_ip_created_at", "ip", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    identifier: Mapped[str] = mapped_column(Text)
    ip: Mapped[str] = mapped_column(Text)
    succeeded: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = created_at()


# --- clinics and OPD ----------------------------------------------------------


class Clinic(Base):
    __tablename__ = "clinics"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    area: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()


class DoctorClinic(Base):
    """One doctor's OPD slot at one clinic. A doctor can sit at several clinics."""

    __tablename__ = "doctor_clinics"
    __table_args__ = (
        CheckConstraint("fee >= 0", name="doctor_clinics_fee_nonnegative"),
        CheckConstraint("opd_start < opd_end", name="doctor_clinics_opd_hours"),
        CheckConstraint(
            "opd_days <@ ARRAY['MON','TUE','WED','THU','FRI','SAT','SUN']::text[]",
            name="doctor_clinics_opd_days",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    doctor_id: Mapped[uuid.UUID] = fk("doctors.id")
    clinic_id: Mapped[uuid.UUID] = fk("clinics.id")
    fee: Mapped[int] = mapped_column(Integer)  # whole rupees
    opd_days: Mapped[list[str]] = mapped_column(ARRAY(Text))  # e.g. ["MON", ..., "SAT"]
    opd_start: Mapped[time] = mapped_column(Time)  # Asia/Kolkata wall-clock time
    opd_end: Mapped[time] = mapped_column(Time)

    doctor: Mapped[Doctor] = relationship(back_populates="clinics")
    clinic: Mapped[Clinic] = relationship()


class OpdSession(Base):
    __tablename__ = "opd_sessions"
    __table_args__ = (
        UniqueConstraint("doctor_clinic_id", "date", name="opd_sessions_doctor_clinic_date"),
        one_of("status", SESSION_STATUSES, "opd_sessions_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    doctor_clinic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("doctor_clinics.id", ondelete="CASCADE")
    )
    date: Mapped[date] = mapped_column(Date)  # the Asia/Kolkata calendar date
    status: Mapped[str] = mapped_column(Text, default="NOT_STARTED", server_default="NOT_STARTED")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_token: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at()

    doctor_clinic: Mapped[DoctorClinic] = relationship()


# --- patients and bookings ----------------------------------------------------


class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = (
        one_of("gender", GENDERS, "patients_gender"),
        CheckConstraint("age BETWEEN 0 AND 120", name="patients_age"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text, index=True)  # walk-ins may have none
    age: Mapped[int] = mapped_column(SmallInteger)
    gender: Mapped[str] = mapped_column(Text)
    height_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    weight_kg: Mapped[float | None] = mapped_column(Numeric(5, 1))
    records_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        UniqueConstraint("session_id", "token", name="bookings_session_token"),
        one_of("visit_type", VISIT_TYPES, "bookings_visit_type"),
        one_of("source", BOOKING_SOURCES, "bookings_source"),
        one_of("status", BOOKING_STATUSES, "bookings_status"),
        CheckConstraint(
            "cancelled_by IS NULL OR cancelled_by IN ('PATIENT', 'CLINIC')", name="bookings_cancelled_by"
        ),
        CheckConstraint("token > 0", name="bookings_token_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # No separate index: the (session_id, token) unique constraint covers it.
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opd_sessions.id", ondelete="CASCADE")
    )
    token: Mapped[int] = mapped_column(Integer)
    patient_id: Mapped[uuid.UUID] = fk("patients.id")
    visit_type: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="WAITING", server_default="WAITING")
    booked_at: Mapped[datetime] = created_at()
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    cancelled_by: Mapped[str | None] = mapped_column(Text)
    # Only the SHA-256 of the patient's tracking code is stored, like session tokens.
    tracking_code_hash: Mapped[str] = mapped_column(Text, unique=True)

    session: Mapped[OpdSession] = relationship()
    patient: Mapped[Patient] = relationship()


class MedicalRecord(Base):
    """A file plus a title. Never store diagnoses or medicine lists here."""

    __tablename__ = "medical_records"
    __table_args__ = (
        one_of("kind", RECORD_KINDS, "medical_records_kind"),
        one_of("added_by", ADDED_BY, "medical_records_added_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = fk("patients.id")
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    date: Mapped[date] = mapped_column(Date)
    added_by: Mapped[str] = mapped_column(Text)
    file_key: Mapped[str] = mapped_column(Text, unique=True)  # key in the storage backend
    content_type: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()


class TestAdvice(Base):
    __tablename__ = "test_advice"
    __table_args__ = (one_of("status", TEST_STATUSES, "test_advice_status"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    booking_id: Mapped[uuid.UUID] = fk("bookings.id")
    booking: Mapped[Booking] = relationship()
    patient_id: Mapped[uuid.UUID] = fk("patients.id")
    name: Mapped[str] = mapped_column(Text)
    advised_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, default="PENDING", server_default="PENDING")
    record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medical_records.id", ondelete="SET NULL")
    )
