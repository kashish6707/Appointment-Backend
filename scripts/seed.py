"""Demo data for local development.

    python -m scripts.seed           # adds the demo doctor, clinic, today's session and bookings
    python -m scripts.seed --reset   # empties EVERY table first (dev databases only!)
"""

import sys
from datetime import datetime, time, timedelta

from sqlalchemy import select, text

from app import security
from app.db import SessionLocal
from app.models import (
    Base,
    Booking,
    Clinic,
    Doctor,
    DoctorClinic,
    OpdSession,
    Patient,
    Payment,
    TestAdvice,
)
from app.timeutil import IST, now_ist, today_ist

DEMO_PHONE = "9801234567"
DEMO_PASSWORD = "demo1234"

# (name, phone, age, gender, visit type, source, status, consent)
PATIENTS = [
    ("Ramesh Kumar", "9431012345", 54, "MALE", "FOLLOW_UP", "ONLINE", "WAITING", True),
    ("Sunita Devi", "9934123456", 47, "FEMALE", "NEW", "ONLINE", "WAITING", False),
    ("Amit Singh", "9572234567", 32, "MALE", "NEW", "WALK_IN", "WAITING", True),
    ("Priya Kumari", "9835345678", 26, "FEMALE", "REPORT_REVIEW", "ONLINE", "WAITING", True),
    ("Manoj Yadav", "9122456789", 61, "MALE", "FOLLOW_UP", "ONLINE", "WAITING", True),
    ("Anjali Sinha", "9709567890", 38, "FEMALE", "NEW", "ONLINE", "WAITING", False),
    ("Vikash Prasad", None, 45, "MALE", "NEW", "WALK_IN", "WAITING", False),
    ("Kavita Jha", "9006678901", 58, "FEMALE", "FOLLOW_UP", "ONLINE", "WAITING", True),
    ("Rohit Raj", "8789789012", 19, "MALE", "NEW", "ONLINE", "WAITING", False),
    ("Pooja Sharma", "7004890123", 29, "FEMALE", "NEW", "ONLINE", "CANCELLED", False),
    ("Sanjay Gupta", "9470901234", 50, "MALE", "REPORT_REVIEW", "WALK_IN", "WAITING", True),
    ("Neha Verma", "6200012345", 34, "FEMALE", "NEW", "ONLINE", "WAITING", False),
]


def main() -> None:
    reset = "--reset" in sys.argv
    with SessionLocal() as db:
        if reset:
            tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
            db.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        elif db.scalar(select(Doctor).where(Doctor.phone == DEMO_PHONE)):
            print("Demo data already exists. Run with --reset to wipe the database and reseed.")
            return

        doctor = Doctor(
            full_name="Dr. Rajeev Anand",
            phone=DEMO_PHONE,
            email="rajeev.anand@example.com",
            password_hash=security.hash_password(DEMO_PASSWORD),
            department="Orthopaedics",
            qualification="MBBS, MS (Orthopaedics)",
            experience_years=18,
            registration_no="BMC-24817",
            is_verified=True,
        )
        clinic = Clinic(
            name="Anand Ortho Care",
            area="Kankarbagh",
            address="Plot 14, Main Road, Kankarbagh, Patna, Bihar 800020",
            phone="9801234500",
        )
        doctor_clinic = DoctorClinic(
            doctor=doctor,
            clinic=clinic,
            fee=500,
            token_amount=50,
            opd_days=["MON", "TUE", "WED", "THU", "FRI", "SAT"],
            opd_start=time(10, 0),
            opd_end=time(14, 0),
        )
        today = today_ist()
        opd_start = datetime.combine(today, time(10, 0), tzinfo=IST)
        # Today's OPD hasn't started: the doctor presses Start OPD, and booked patients are told.
        session = OpdSession(doctor_clinic=doctor_clinic, date=today, status="NOT_STARTED")
        db.add_all([doctor, clinic, doctor_clinic, session])

        codes = {}
        for token, (name, phone, age, gender, visit, source, status, consent) in enumerate(PATIENTS, 1):
            booked_at = opd_start - timedelta(hours=14) + timedelta(minutes=37 * token)
            patient = Patient(
                name=name,
                phone=phone,
                age=age,
                gender=gender,
                height_cm=165 if consent else None,
                weight_kg=68 if consent else None,
                records_consent_at=booked_at if consent else None,
            )
            code = security.new_tracking_code()
            codes[token] = code
            booking = Booking(
                session=session,
                token=token,
                patient=patient,
                visit_type=visit,
                source=source,
                status=status,
                booked_at=booked_at,
                tracking_code_hash=security.hash_token(code),
            )
            if status == "CANCELLED":
                booking.cancelled_at = booked_at + timedelta(hours=2)
                booking.cancelled_by = "PATIENT"
                booking.cancel_reason = "Not feeling well enough to travel"
            db.add_all([patient, booking])
            if source == "ONLINE":
                db.flush()
                db.add(
                    Payment(
                        booking_id=booking.id,
                        doctor_clinic=doctor_clinic,
                        date=today,
                        visit_type=visit,
                        tracking_code_hash=booking.tracking_code_hash,
                        amount_paise=doctor_clinic.token_amount * 100,
                        gateway="fake",
                        gateway_order_id=f"order_seed{token:04d}",
                        gateway_payment_id=f"pay_seed{token:04d}",
                        status="PAID",
                        paid_at=booked_at,
                    )
                )
            if status == "SENT_FOR_TESTS":
                db.flush()
                for test in ("X-ray left knee AP/lateral", "Serum vitamin D"):
                    db.add(TestAdvice(booking=booking, patient_id=patient.id, name=test, advised_on=today))
        db.commit()

        print(f"Seeded {len(PATIENTS)} bookings for {today} ({now_ist():%H:%M} IST).")
        print(f"Doctor sign-in: phone {DEMO_PHONE}, password {DEMO_PASSWORD}")
        print(f"Tracking code for token 5 (Manoj Yadav): {codes[5]}")


if __name__ == "__main__":
    main()
