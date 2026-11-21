"""Moving a waiting token to another day."""

from datetime import timedelta

from app.timeutil import today_ist


def book(client, slot, day, name="Ravi Kumar", phone="9431000011"):
    r = client.post("/api/public/bookings", json={
        "doctorClinicId": str(slot.id), "date": day.isoformat(), "visitType": "NEW",
        "patient": {"name": name, "phone": phone, "age": 40, "gender": "MALE"}, "recordsConsent": False})
    assert r.status_code == 201, r.text
    return r.json()


def move(client, b, day):
    return client.post(f"/api/public/bookings/{b['bookingId']}/reschedule", json={"code": b["trackingCode"], "date": day.isoformat()})


def status(client, b):
    return client.get(f"/api/public/bookings/{b['bookingId']}", params={"code": b["trackingCode"]}).json()


def test_moving_gives_a_new_token_and_cancels_the_old_one(client, make_slot):
    slot = make_slot()
    today, tomorrow = today_ist(), today_ist() + timedelta(days=1)
    book(client, slot, tomorrow, name="Someone Else", phone="9431000099")  # tomorrow already has token 1
    old = book(client, slot, today)
    assert status(client, old)["doctorClinicId"] == str(slot.id)

    r = move(client, old, tomorrow)
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["token"] == 2 and new["date"] == tomorrow.isoformat() and new["trackingCode"]
    assert status(client, old)["status"] == "CANCELLED"
    s = status(client, new)
    assert s["status"] == "WAITING" and s["date"] == tomorrow.isoformat()

    # The old token can't be moved again, and the new one can come back to today.
    assert move(client, old, tomorrow).status_code == 409
    assert move(client, new, today).status_code == 201


def test_moving_is_refused_without_changing_anything(client, make_slot):
    slot = make_slot()
    today = today_ist()
    b = book(client, slot, today)
    assert move(client, b, today).status_code == 400  # same day
    assert move(client, b, today - timedelta(days=1)).status_code == 400  # in the past
    assert client.post(f"/api/public/bookings/{b['bookingId']}/reschedule",
                       json={"code": "wrong", "date": (today + timedelta(days=1)).isoformat()}).status_code == 404
    assert status(client, b)["status"] == "WAITING"


def test_a_full_day_keeps_the_old_token(client, make_slot):
    from datetime import time

    from app.db import SessionLocal
    from app.models import WEEKDAYS, WeeklyHours

    slot = make_slot()
    today, tomorrow = today_ist(), today_ist() + timedelta(days=1)
    b = book(client, slot, today)
    # Each day now allows one patient; tomorrow's place gets taken.
    with SessionLocal() as s:
        s.add_all([WeeklyHours(doctor_clinic_id=slot.id, weekday=d, start=time(0, 0), end=time(23, 59), patient_limit=1) for d in WEEKDAYS])
        s.commit()
    book(client, slot, tomorrow, name="Someone Else", phone="9431000099")
    r = move(client, b, tomorrow)
    assert r.status_code == 409, r.text
    assert status(client, b)["status"] == "WAITING"
