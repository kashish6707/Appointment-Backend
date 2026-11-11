from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta

import pytest

from app.db import SessionLocal
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import bookings
from app.main import app
from app.models import WEEKDAYS
from app.timeutil import IST, today_ist


def booking_body(slot, *, phone="9431012345", name="Ramesh Kumar", day=None, consent=True, **patient):
    return {
        "doctorClinicId": str(slot.id),
        "date": (day or today_ist()).isoformat(),
        "visitType": "NEW",
        "patient": {"name": name, "phone": phone, "age": 54, "gender": "MALE", **patient},
        "recordsConsent": consent,
    }


def book(client, slot, **kwargs):
    return client.post("/api/public/bookings", json=booking_body(slot, **kwargs))


# --- doctor listing -----------------------------------------------------------


def test_list_shows_only_verified_doctors(client, make_slot):
    shown = make_slot(full_name="Dr Rajeev Anand")
    make_slot(full_name="Dr Not Verified", verified=False)
    r = client.get("/api/public/doctors")
    assert r.status_code == 200
    [doctor] = r.json()
    assert doctor["fullName"] == "Dr Rajeev Anand"
    assert doctor["clinic"]["doctorClinicId"] == str(shown.id)
    assert doctor["fee"] == 400
    assert doctor["waitingToday"] == 0
    assert "phone" not in doctor  # the doctor's own phone isn't public


def test_list_search_and_department(client, make_slot):
    make_slot(full_name="Dr Rajeev Anand", department="Orthopaedics")
    make_slot(full_name="Dr Meera Sinha", department="Paediatrics")
    names = lambda r: [d["fullName"] for d in r.json()]  # noqa: E731
    assert names(client.get("/api/public/doctors?search=meera")) == ["Dr Meera Sinha"]
    assert names(client.get("/api/public/doctors?department=orthopaedics")) == ["Dr Rajeev Anand"]
    assert len(client.get("/api/public/doctors?search=boring road").json()) == 2  # clinic area
    assert client.get("/api/public/doctors?search=%25").json() == []  # % is literal, not a wildcard


def test_waiting_today_counts_waiting_bookings(client, make_slot):
    slot = make_slot()
    book(client, slot, phone="9431000001")
    book(client, slot, phone="9431000002")
    assert client.get("/api/public/doctors").json()[0]["waitingToday"] == 2


def test_get_doctor(client, make_slot):
    slot = make_slot(full_name="Dr Rajeev Anand")
    r = client.get(f"/api/public/doctors/{slot.doctor_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["fullName"] == "Dr Rajeev Anand"
    [clinic] = data["clinics"]
    assert clinic["todaySessionStatus"] == "NOT_STARTED"

    hidden = make_slot(verified=False)
    assert client.get(f"/api/public/doctors/{hidden.doctor_id}").status_code == 404
    assert client.get("/api/public/doctors/not-a-uuid").status_code == 404


# --- booking ---------------------------------------------------------------


def test_booking_gives_sequential_tokens(client, make_slot, db, emitted):
    slot = make_slot()
    first = book(client, slot, phone="9431000001")
    second = book(client, slot, phone="9431000002")
    assert first.status_code == second.status_code == 201
    assert first.json()["token"] == 1 and second.json()["token"] == 2
    code = first.json()["trackingCode"]
    assert len(code) >= 20
    stored = db.execute(text("SELECT tracking_code_hash FROM bookings")).scalars().all()
    assert code not in stored  # only the hash is stored

    rooms = [(room, event) for room, event, _ in emitted]
    assert (f"doctor:{slot.doctor_id}", "booking.created") in rooms
    payload = next(p for room, e, p in emitted if e == "booking.created")
    assert payload["patient"]["name"] == "Ramesh Kumar" and "heightCm" not in payload["patient"]


def test_parallel_bookings_never_share_a_token(make_slot, db):
    slot = make_slot()

    def one(i):
        with TestClient(app) as c:
            return c.post("/api/public/bookings", json=booking_body(slot, phone=f"94310{i:05d}", name=f"Patient {i}"))

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(one, range(25)))
    assert [r.status_code for r in results] == [201] * 25
    assert sorted(r.json()["token"] for r in results) == list(range(1, 26))
    assert db.scalar(text("SELECT count(*) FROM opd_sessions")) == 1


def test_same_patient_is_reused_by_phone_and_name(client, make_slot, db):
    slot = make_slot()
    tomorrow = today_ist() + timedelta(days=1)
    assert book(client, slot, name="Ramesh Kumar").status_code == 201
    assert book(client, slot, name="ramesh  KUMAR", day=tomorrow, weightKg=70).status_code == 201
    assert db.scalar(text("SELECT count(*) FROM patients")) == 1
    assert db.scalar(text("SELECT weight_kg FROM patients")) == 70

    # Same phone, different name (e.g. a family member) is a different patient.
    assert book(client, slot, name="Sita Kumar").status_code == 201
    assert db.scalar(text("SELECT count(*) FROM patients")) == 2


def test_patient_cannot_hold_two_tokens_in_one_session(client, make_slot):
    slot = make_slot()
    assert book(client, slot).status_code == 201
    r = book(client, slot)
    assert r.status_code == 409
    assert r.json() == {"message": "Ramesh Kumar already has token 1 for this OPD session."}


def test_consent_is_granted_and_withdrawn(client, make_slot, db):
    slot = make_slot()
    tomorrow = today_ist() + timedelta(days=1)
    book(client, slot, consent=True)
    assert db.scalar(text("SELECT records_consent_at IS NOT NULL FROM patients"))
    book(client, slot, consent=False, day=tomorrow)
    assert db.scalar(text("SELECT records_consent_at IS NULL FROM patients"))


def test_booking_date_rules(client, make_slot):
    slot = make_slot()
    today = today_ist()
    r = book(client, slot, day=today - timedelta(days=1))
    assert r.status_code == 400 and "up to 7 days ahead" in r.json()["message"]
    assert book(client, slot, day=today + timedelta(days=8)).status_code == 400
    assert book(client, slot, day=today + timedelta(days=7)).status_code == 201

    only_tomorrow = make_slot(days=[WEEKDAYS[(today + timedelta(days=1)).weekday()]])
    r = book(client, only_tomorrow)
    assert r.status_code == 400 and "doesn't hold OPD" in r.json()["message"]


def test_cannot_book_today_after_opd_hours(make_slot):
    slot = make_slot(start=time(10, 0), end=time(14, 0))
    today = today_ist()
    evening = datetime.combine(today, time(14, 30), tzinfo=IST)
    with SessionLocal() as s:
        with pytest.raises(HTTPException) as exc:
            bookings.check_bookable_date(s, slot, today, evening)
        assert exc.value.status_code == 400
        bookings.check_bookable_date(s, slot, today, datetime.combine(today, time(13, 0), tzinfo=IST))


def test_cannot_book_closed_session_or_unverified_doctor(client, make_slot, db):
    slot = make_slot()
    book(client, slot, phone="9431000001")
    with db.begin():
        db.execute(text("UPDATE opd_sessions SET status = 'CLOSED'"))
    r = book(client, slot, phone="9431000002")
    assert r.status_code == 409 and "closed" in r.json()["message"]

    assert book(client, make_slot(verified=False)).status_code == 404


@pytest.mark.parametrize(
    "patient, message_part",
    [
        ({"age": 130}, "age"),
        ({"age": "54"}, "age"),
        ({"gender": "M"}, "gender"),
        ({"phone": "12345"}, "10-digit mobile"),
        ({"name": " A "}, "patient's name"),
        ({"heightCm": 900}, "centimetres"),
    ],
)
def test_booking_validation(client, make_slot, patient, message_part):
    r = book(client, make_slot(), **patient)
    assert r.status_code == 400
    assert message_part in r.json()["message"]


def test_booking_validation_top_level(client, make_slot):
    slot = make_slot()
    for change, part in [
        ({"recordsConsent": "yes"}, "records"),
        ({"visitType": "EMERGENCY"}, "visit type"),
        ({"date": "11/11/2026"}, "YYYY-MM-DD"),
        ({"doctorClinicId": "nope"}, "doctor and clinic"),
    ]:
        r = client.post("/api/public/bookings", json={**booking_body(slot), **change})
        assert r.status_code == 400, change
        assert part in r.json()["message"], (change, r.json())


# --- status and cancel ----------------------------------------------------------


def test_booking_status(client, make_slot):
    slot = make_slot(full_name="Dr Rajeev Anand")
    first = book(client, slot, phone="9431000001").json()
    third = None
    book(client, slot, phone="9431000002")
    third = book(client, slot, phone="9431000003").json()

    r = client.get(f"/api/public/bookings/{third['bookingId']}", params={"code": third["trackingCode"]})
    assert r.status_code == 200
    data = r.json()
    assert data["token"] == 3
    assert data["status"] == "WAITING"
    assert data["patientsAhead"] == 2
    assert data["estimatedMinutes"] == 20  # 2 ahead x 10-minute default
    assert data["sessionStatus"] == "NOT_STARTED"
    assert data["nowServing"] is None
    assert data["doctorName"] == "Dr Rajeev Anand"

    # The other patient's code doesn't open this booking; neither does no code.
    r = client.get(f"/api/public/bookings/{third['bookingId']}", params={"code": first["trackingCode"]})
    assert r.status_code == 404
    assert client.get(f"/api/public/bookings/{third['bookingId']}").status_code == 400


def test_cancel_booking(client, make_slot, db, emitted):
    slot = make_slot()
    first = book(client, slot, phone="9431000001").json()
    second = book(client, slot, phone="9431000002").json()
    url = f"/api/public/bookings/{first['bookingId']}/cancel"

    assert client.post(url, json={"code": "wrong"}).status_code == 404
    r = client.post(url, json={"code": first["trackingCode"], "reason": "Feeling better"})
    assert r.status_code == 200
    assert r.json()["status"] == "CANCELLED"
    row = db.execute(text("SELECT cancelled_by, cancel_reason FROM bookings WHERE token = 1")).one()
    assert tuple(row) == ("PATIENT", "Feeling better")
    assert (f"doctor:{slot.doctor_id}", "booking.cancelled") in [(r, e) for r, e, _ in emitted]
    assert ("session:" + db.scalar(text("SELECT id::text FROM opd_sessions")), "session.updated") in [
        (r, e) for r, e, _ in emitted
    ]

    again = client.post(url, json={"code": first["trackingCode"]})
    assert again.status_code == 409

    # The second patient moves up the queue.
    status = client.get(f"/api/public/bookings/{second['bookingId']}", params={"code": second["trackingCode"]})
    assert status.json()["patientsAhead"] == 0

    # Cancelled tokens are never reused.
    assert book(client, slot, phone="9431000003").json()["token"] == 3
