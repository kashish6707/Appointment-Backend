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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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
STAFF_ROLES = ("RECEPTIONIST", "COMPOUNDER")
DEGREE_LEVELS = ("UG", "PG", "SUPER_SPECIALITY", "FELLOWSHIP", "DIPLOMA", "OTHER")
NOTIFICATION_STATUSES = ("PENDING", "SENT", "FAILED", "SKIPPED")
PAYMENT_STATUSES = ("CREATED", "PAID", "CANCELLED", "REFUND_PENDING", "REFUNDED", "REFUND_FAILED")


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
        CheckConstraint("consultation_fee IS NULL OR consultation_fee BETWEEN 0 AND 100000", name="doctors_fee"),
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
    medical_council: Mapped[str | None] = mapped_column(Text)  # e.g. "Bihar Council of Medicine"
    # Public profile, filled in at registration (older accounts complete it on the profile page).
    photo_key: Mapped[str | None] = mapped_column(Text)  # file under MEDIA_DIR
    photo_content_type: Mapped[str | None] = mapped_column(Text)
    consultation_fee: Mapped[int | None] = mapped_column(Integer)  # whole rupees; clinics start from it
    about: Mapped[str | None] = mapped_column(Text)
    achievements: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    expertise: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = created_at()

    clinics: Mapped[list["DoctorClinic"]] = relationship(back_populates="doctor")
    degrees: Mapped[list["DoctorDegree"]] = relationship(
        order_by="DoctorDegree.position", cascade="all, delete-orphan", lazy="selectin"
    )


class DoctorDegree(Base):
    __tablename__ = "doctor_degrees"
    __table_args__ = (one_of("level", DEGREE_LEVELS, "doctor_degrees_level"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    doctor_id: Mapped[uuid.UUID] = fk("doctors.id")
    position: Mapped[int] = mapped_column(SmallInteger)
    level: Mapped[str] = mapped_column(Text)  # UG, PG, SUPER_SPECIALITY, FELLOWSHIP, DIPLOMA, OTHER
    name: Mapped[str] = mapped_column(Text)  # e.g. MBBS, MS (Orthopaedics), DNB
    institute: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(SmallInteger)


class Session(Base):
    """A signed-in browser: a doctor's, or a staff member's (who picks which doctor they work for)."""

    __tablename__ = "sessions"
    __table_args__ = (CheckConstraint("(doctor_id IS NULL) <> (staff_id IS NULL)", name="sessions_one_owner"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    doctor_id: Mapped[uuid.UUID | None] = fk("doctors.id", nullable=True)
    staff_id: Mapped[uuid.UUID | None] = fk("staff.id", nullable=True)
    acting_doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("doctors.id", ondelete="SET NULL")
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Staff(Base):
    """A receptionist or compounder. One account, which several doctors may each grant access to."""

    __tablename__ = "staff"
    __table_args__ = (CheckConstraint("phone IS NOT NULL OR email IS NOT NULL", name="staff_phone_or_email"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    full_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text, unique=True)
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text)  # null until the invite is accepted
    invite_token_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class StaffMembership(Base):
    """One doctor's grant to one staff member: a role preset, the exact permissions, and which
    of the doctor's clinic slots it covers. The doctor (root) is the only one who can change it."""

    __tablename__ = "staff_memberships"
    __table_args__ = (
        UniqueConstraint("staff_id", "doctor_id", name="staff_memberships_staff_doctor"),
        one_of("role", STAFF_ROLES, "staff_memberships_role"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    staff_id: Mapped[uuid.UUID] = fk("staff.id")
    doctor_id: Mapped[uuid.UUID] = fk("doctors.id")
    role: Mapped[str] = mapped_column(Text)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(Text))
    doctor_clinic_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    created_at: Mapped[datetime] = created_at()

    staff: Mapped[Staff] = relationship()
    doctor: Mapped["Doctor"] = relationship()


class AuditEntry(Base):
    """Who did what, for the doctor's activity log. Never updated or deleted by the app."""

    __tablename__ = "audit_log"
    __table_args__ = (
        one_of("actor_kind", ("DOCTOR", "STAFF"), "audit_log_actor_kind"),
        Index("ix_audit_log_doctor_created_at", "doctor_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("doctors.id", ondelete="CASCADE"))
    actor_kind: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    actor_name: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = created_at()


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
        CheckConstraint("token_amount BETWEEN 0 AND fee", name="doctor_clinics_token_amount"),
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
    # Paid online when booking; the rest (fee - token_amount) is paid at reception. 0 = no online payment.
    token_amount: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    opd_days: Mapped[list[str]] = mapped_column(ARRAY(Text))  # e.g. ["MON", ..., "SAT"]
    opd_start: Mapped[time] = mapped_column(Time)  # Asia/Kolkata wall-clock time
    opd_end: Mapped[time] = mapped_column(Time)

    doctor: Mapped[Doctor] = relationship(back_populates="clinics")
    clinic: Mapped[Clinic] = relationship()


class WeeklyHours(Base):
    """A clinic slot's regular OPD on one weekday, with the most patients the doctor will see.

    No row for a weekday: no OPD that day. A slot with no rows at all hasn't set weekly hours
    yet and uses DoctorClinic.opd_days / opd_start / opd_end with no limit (see app/schedule.py).
    """

    __tablename__ = "weekly_hours"
    __table_args__ = (
        UniqueConstraint("doctor_clinic_id", "weekday", name="weekly_hours_slot_weekday"),
        one_of("weekday", WEEKDAYS, "weekly_hours_weekday"),
        CheckConstraint('start < "end"', name="weekly_hours_hours"),
        CheckConstraint("patient_limit IS NULL OR patient_limit BETWEEN 1 AND 500", name="weekly_hours_limit"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    doctor_clinic_id: Mapped[uuid.UUID] = fk("doctor_clinics.id")
    weekday: Mapped[str] = mapped_column(Text)
    start: Mapped[time] = mapped_column(Time)
    end: Mapped[time] = mapped_column(Time)
    patient_limit: Mapped[int | None] = mapped_column(SmallInteger)


class DateException(Base):
    """One date that differs from the weekly hours: the doctor is away (CLOSED) or keeps
    different hours and/or limit (HOURS)."""

    __tablename__ = "date_exceptions"
    __table_args__ = (
        UniqueConstraint("doctor_clinic_id", "date", name="date_exceptions_slot_date"),
        one_of("kind", ("CLOSED", "HOURS"), "date_exceptions_kind"),
        CheckConstraint(
            "kind = 'CLOSED' OR (start IS NOT NULL AND \"end\" IS NOT NULL AND start < \"end\")",
            name="date_exceptions_hours",
        ),
        CheckConstraint("patient_limit IS NULL OR patient_limit BETWEEN 1 AND 500", name="date_exceptions_limit"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    doctor_clinic_id: Mapped[uuid.UUID] = fk("doctor_clinics.id")
    date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(Text)
    start: Mapped[time | None] = mapped_column(Time)
    end: Mapped[time | None] = mapped_column(Time)
    patient_limit: Mapped[int | None] = mapped_column(SmallInteger)
    note: Mapped[str | None] = mapped_column(Text)  # shown to patients, e.g. "Doctor at a conference"
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()


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
    # When "End OPD" was pressed. The patient it marked DONE has completed_at equal to this,
    # which is how "Reopen OPD" finds them again.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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

    corrections: Mapped[list["PatientCorrection"]] = relationship(lazy="selectin")


class PatientCorrection(Base):
    """One doctor's corrections to a patient's name, age or gender.

    Patients are shared by every doctor they book, and identified by phone + name, so a doctor's
    edit never changes the shared row: it only changes what that doctor (and their staff) see.
    A null field means "use the patient's own value".
    """

    __tablename__ = "patient_corrections"
    __table_args__ = (
        UniqueConstraint("patient_id", "doctor_id", name="patient_corrections_patient_doctor"),
        CheckConstraint("age IS NULL OR age BETWEEN 0 AND 120", name="patient_corrections_age"),
        CheckConstraint("gender IS NULL OR gender IN ('MALE', 'FEMALE', 'OTHER')", name="patient_corrections_gender"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = fk("patients.id")
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("doctors.id", ondelete="CASCADE"))
    name: Mapped[str | None] = mapped_column(Text)
    age: Mapped[int | None] = mapped_column(SmallInteger)
    gender: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
    # When the patient was called in, and when their consult ended. Used for wait estimates.
    called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    cancelled_by: Mapped[str | None] = mapped_column(Text)
    # Measured at this visit by the clinic; the patient's own values stay on patients.
    height_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    weight_kg: Mapped[float | None] = mapped_column(Numeric(5, 1))
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


# --- payments -------------------------------------------------------------------


class Payment(Base):
    """An online token-amount payment for a booking.

    The booking row is only created once the payment is confirmed, so unpaid attempts never take
    a token. Until then the requested patient details wait in `request`, which is cleared once
    the booking exists. `booking_id` is chosen up front so the patient's tracking link works
    before and after payment (no FK, because the booking doesn't exist yet).
    """

    __tablename__ = "payments"
    __table_args__ = (
        one_of("status", PAYMENT_STATUSES, "payments_status"),
        CheckConstraint("amount_paise > 0", name="payments_amount_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    doctor_clinic_id: Mapped[uuid.UUID] = fk("doctor_clinics.id")
    date: Mapped[date] = mapped_column(Date)
    visit_type: Mapped[str] = mapped_column(Text)
    request: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))  # patient details + consent, until booked
    tracking_code_hash: Mapped[str] = mapped_column(Text, unique=True)
    amount_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(Text, default="INR", server_default="INR")
    gateway: Mapped[str] = mapped_column(Text)
    gateway_order_id: Mapped[str] = mapped_column(Text, unique=True)
    gateway_payment_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="CREATED", server_default="CREATED")
    created_at: Mapped[datetime] = created_at()
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refund_id: Mapped[str | None] = mapped_column(Text)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)  # why it was refunded or why a refund failed

    doctor_clinic: Mapped[DoctorClinic] = relationship()


# --- messages to patients ---------------------------------------------------------------


class Notification(Base):
    """One message to one patient about one booking (an outbox: written in the same transaction
    as the change, then sent in the background and retried). A booking gets each kind at most
    once, so starting OPD twice never messages anyone twice. Walk-ins without a phone are SKIPPED."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("booking_id", "kind", name="notifications_booking_kind"),
        one_of("status", NOTIFICATION_STATUSES, "notifications_status"),
        Index("ix_notifications_due", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    booking_id: Mapped[uuid.UUID] = fk("bookings.id")
    session_id: Mapped[uuid.UUID] = fk("opd_sessions.id")
    kind: Mapped[str] = mapped_column(Text)  # e.g. OPD_STARTED
    phone: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="PENDING", server_default="PENDING")
    channel: Mapped[str | None] = mapped_column(Text)  # which sender delivered it: log, msg91, …
    provider_id: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = created_at()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
