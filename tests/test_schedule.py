from datetime import timedelta

import pytest
from sqlalchemy import text

from app.models import WEEKDAYS
from app.timeutil import today_ist
from tests.test_doctor import sign_in
from tests.test_payments import pay, start_paid_booking
from tests.test_public import book
from tests.test_queue import J

ALL_DAY = {"start": "00:00", "end": "23:59"}


def week(limit=None, off=(), **hours):
    hours = hours or ALL_DAY
    return [
        {"weekday": wd, "open": wd not in off, **({**hours, "limit": limit} if wd not in off else {})}
        for wd in WEEKDAYS
    ]


@pytest.fixture
def doc(client, make_slot):
    slot = make_slot()
    return {"slot": slot, "auth": {**J, **sign_in(client, slot.doctor.phone, "secret123")}, "base": f"/api/doctor/clinics/{slot.id}"}


def put_weekly(client, doc, days, confirm=False):
    return client.put(f"{doc['base']}/weekly", headers=doc["auth"], json={"days": days, "confirmCancel": confirm})


def add_exc(client, doc, **body):
    return client.post(f"{doc['base']}/exceptions", headers=doc["auth"], json=body)


def wd(day):
    return WEEKDAYS[day.weekday()]


# --- weekly hours and limits -------------------------------------------------------------


def test_weekly_hours_are_saved_and_shown(client, doc):
    r = client.get(f"{doc['base']}/schedule", headers=doc["auth"])
    assert r.status_code == 200
    assert len(r.json()["weekly"]) == 7 and all(d["open"] for d in r.json()["weekly"])  # from the slot's old days

    days = week(limit=30, off=("SUN",), start="10:00", end="14:00")
    days[5] = {"weekday": "SAT", "open": True, "start": "10:00", "end": "12:00", "limit": 15}
    r = put_weekly(client, doc, days)
    assert r.status_code == 200, r.text
    weekly = {d["weekday"]: d for d in r.json()["weekly"]}
    assert weekly["MON"] == {"weekday": "MON", "open": True, "start": "10:00", "end": "14:00", "limit": 30}
    assert weekly["SAT"]["end"] == "12:00" and weekly["SAT"]["limit"] == 15
    assert weekly["SUN"] == {"weekday": "SUN", "open": False, "start": None, "end": None, "limit": None}
    assert r.json()["clinic"]["opdDays"] == ["MON", "TUE", "WED", "THU", "FRI", "SAT"]


@pytest.mark.parametrize("bad, message", [
    ({"weekday": "MON", "open": True, "start": "14:00", "end": "10:00"}, "Mon: the end time must be after the start time."),
    ({"weekday": "MON", "open": True, "start": "10"}, "Enter times as HH:MM, for example 10:00 or 17:30."),
    ({"weekday": "MON", "open": True, "start": "10:00", "end": "14:00", "limit": 0}, "Enter a daily patient limit from 1 to 500, or leave it empty for no limit."),
])
def test_weekly_validation(client, doc, bad, message):
    days = week()
    days[0] = bad
    r = put_weekly(client, doc, days)
    assert r.status_code == 400 and r.json() == {"message": message}


def test_online_booking_stops_at_the_limit(client, doc):
    put_weekly(client, doc, week(limit=2))
    first = book(client, doc["slot"], phone="9431000001").json()
    assert book(client, doc["slot"], phone="9431000002").status_code == 201
    r = book(client, doc["slot"], phone="9431000003")
    assert r.status_code == 409 and r.json()["message"] == "All tokens for that day are booked. Choose another day."
    # A cancellation frees a place.
    client.post(f"/api/public/bookings/{first['bookingId']}/cancel", json={"code": first["trackingCode"]})
    assert book(client, doc["slot"], phone="9431000003").status_code == 201


def test_paid_booking_is_refused_before_payment_when_full(client, make_slot, gateway):
    slot = make_slot(token_amount=100)
    auth = {**J, **sign_in(client, slot.doctor.phone, "secret123")}
    client.put(f"/api/doctor/clinics/{slot.id}/weekly", headers=auth, json={"days": week(limit=1)})
    pay(client, gateway, start_paid_booking(client, slot, phone="9431000001"))
    r = book(client, slot, phone="9431000002")  # refused before any payment is started
    assert r.status_code == 409 and "All tokens" in r.json()["message"]


def test_walk_in_over_the_limit_needs_confirmation(client, doc):
    put_weekly(client, doc, week(limit=1))
    book(client, doc["slot"], phone="9431000001")
    session = client.get("/api/doctor/dashboard", headers=doc["auth"]).json()["session"]["id"]
    walk_in = {"name": "Desk Patient", "age": 30, "gender": "FEMALE"}
    r = client.post(f"/api/doctor/sessions/{session}/walk-ins", headers=doc["auth"], json=walk_in)
    assert r.status_code == 409
    assert r.json() == {"message": "The limit of 1 patients for this day is reached.", "code": "LIMIT_REACHED", "limit": 1, "booked": 1}
    r = client.post(f"/api/doctor/sessions/{session}/walk-ins", headers=doc["auth"], json={**walk_in, "overLimit": True})
    assert r.status_code == 201


def test_dashboard_and_calendar_show_the_plan(client, doc):
    put_weekly(client, doc, week(limit=25, start="10:00", end="14:00"))
    day = client.get("/api/doctor/dashboard", headers=doc["auth"]).json()
    assert day["plan"]["start"] == "10:00" and day["plan"]["limit"] == 25 and day["plan"]["open"] is True
    cal = client.get("/api/doctor/calendar", headers=doc["auth"]).json()["days"]
    assert all(d["limit"] == 25 for d in cal)


# --- days off and different hours ----------------------------------------------------------


def test_day_off_with_bookings_cancels_and_refunds_after_confirming(client, make_slot, gateway, db, emitted):
    slot = make_slot(token_amount=100)
    doc = {"slot": slot, "auth": {**J, **sign_in(client, slot.doctor.phone, "secret123")}, "base": f"/api/doctor/clinics/{slot.id}"}
    tomorrow = today_ist() + timedelta(days=1)
    pay(client, gateway, start_paid_booking(client, slot, phone="9431000001", day=tomorrow))
    pay(client, gateway, start_paid_booking(client, slot, phone="9431000002", day=tomorrow), payment_id="pay_2")

    r = add_exc(client, doc, **{"from": tomorrow.isoformat(), "kind": "CLOSED", "note": "At a conference"})
    assert r.status_code == 409
    assert r.json()["code"] == "HAS_BOOKINGS" and r.json()["count"] == 2 and r.json()["dates"] == [tomorrow.isoformat()]
    assert db.scalar(text("SELECT count(*) FROM date_exceptions")) == 0  # nothing saved
    db.commit()

    r = add_exc(client, doc, **{"from": tomorrow.isoformat(), "kind": "CLOSED", "note": "At a conference", "confirmCancel": True})
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"] == 2 and r.json()["refunded"] == 2
    rows = db.execute(text("SELECT status, cancelled_by, cancel_reason FROM bookings")).all()
    assert set(rows) == {("CANCELLED", "CLINIC", "Doctor unavailable")}
    assert db.scalar(text("SELECT count(*) FROM payments WHERE status = 'REFUNDED'")) == 2
    assert len(gateway.refunds) == 2
    assert [e for _, e, _ in emitted].count("booking.cancelled") == 2

    # Patients can't book that day, and see the note.
    r = book(client, slot, phone="9431000003", day=tomorrow)
    assert r.status_code in (400, 409)
    days = client.get(f"/api/public/clinics/{slot.id}/availability").json()["days"]
    off = next(d for d in days if d["date"] == tomorrow.isoformat())
    assert off["open"] is False and off["bookable"] is False and off["note"] == "At a conference"


def test_different_hours_on_a_normally_off_day(client, doc):
    tomorrow = today_ist() + timedelta(days=1)
    put_weekly(client, doc, week(off=(wd(tomorrow),), start="10:00", end="14:00"))
    assert book(client, doc["slot"], day=tomorrow).status_code == 400
    r = add_exc(client, doc, **{"from": tomorrow.isoformat(), "kind": "HOURS", "start": "16:00", "end": "18:00", "limit": 10})
    assert r.status_code == 200, r.text
    assert book(client, doc["slot"], day=tomorrow).status_code == 201
    upcoming = next(d for d in r.json()["upcoming"] if d["date"] == tomorrow.isoformat())
    assert upcoming["start"] == "16:00" and upcoming["limit"] == 10 and upcoming["exception"] == "HOURS"

    # Removing it would close a booked day again: same confirmation.
    r = client.delete(f"{doc['base']}/exceptions/{tomorrow.isoformat()}", headers=doc["auth"])
    assert r.status_code == 409 and r.json()["count"] == 1
    r = client.delete(f"{doc['base']}/exceptions/{tomorrow.isoformat()}?confirmCancel=true", headers=doc["auth"])
    assert r.status_code == 200 and r.json()["cancelled"] == 1


def test_turning_off_a_booked_weekday_asks_first(client, doc):
    tomorrow = today_ist() + timedelta(days=1)
    book(client, doc["slot"], day=tomorrow)
    r = put_weekly(client, doc, week(off=(wd(tomorrow),)))
    assert r.status_code == 409 and r.json()["count"] == 1
    assert client.get(f"{doc['base']}/schedule", headers=doc["auth"]).json()["weekly"][tomorrow.weekday()]["open"] is True
    assert put_weekly(client, doc, week(off=(wd(tomorrow),)), confirm=True).json()["cancelled"] == 1


def test_date_range_and_limits(client, doc):
    start = today_ist() + timedelta(days=2)
    r = add_exc(client, doc, **{"from": start.isoformat(), "to": (start + timedelta(days=4)).isoformat(), "kind": "CLOSED"})
    assert r.status_code == 200 and len(r.json()["exceptions"]) == 5
    yesterday = (today_ist() - timedelta(days=1)).isoformat()
    assert add_exc(client, doc, **{"from": yesterday, "kind": "CLOSED"}).json() == {"message": "Choose today or a later date."}
    far = (today_ist() + timedelta(days=100)).isoformat()
    assert add_exc(client, doc, **{"from": today_ist().isoformat(), "to": far, "kind": "CLOSED"}).status_code == 400
    r = add_exc(client, doc, **{"from": start.isoformat(), "kind": "HOURS", "start": "12:00"})
    assert r.json() == {"message": "Add the start and end times for that day."}


# --- what patients see -----------------------------------------------------------------------


def test_public_availability(client, doc):
    put_weekly(client, doc, week(limit=2))
    book(client, doc["slot"], phone="9431000001")
    book(client, doc["slot"], phone="9431000002")
    days = client.get(f"/api/public/clinics/{doc['slot'].id}/availability").json()["days"]
    assert len(days) == 8
    today = days[0]
    assert today["booked"] == 2 and today["left"] == 0 and today["bookable"] is False and today["reason"] == "FULL"
    assert days[1]["left"] == 2 and days[1]["bookable"] is True


def test_patients_see_an_expected_time(client, doc):
    tomorrow = today_ist() + timedelta(days=1)
    # 10:00-14:00 for 24 patients: 10 minutes each.
    put_weekly(client, doc, week(limit=24, start="10:00", end="14:00"))
    made = [book(client, doc["slot"], phone=f"943100000{i}", name=f"P {i}", day=tomorrow).json() for i in range(1, 4)]
    third = made[2]
    status = client.get(f"/api/public/bookings/{third['bookingId']}?code={third['trackingCode']}").json()
    assert status["expectedAt"] == "10:20" and status["opdStart"] == "10:00" and status["opdEnd"] == "14:00"


# --- who may change timings -----------------------------------------------------------------


def test_only_doctor_or_granted_staff_change_timings(client, doc, make_slot):
    other = make_slot()
    assert client.get(f"/api/doctor/clinics/{other.id}/schedule", headers=doc["auth"]).status_code == 404

    r = client.post("/api/doctor/staff", headers=doc["auth"], json={
        "fullName": "Desk Person", "phone": "9876500021", "role": "RECEPTIONIST",
        "permissions": ["queue.view"], "doctorClinicIds": [str(doc["slot"].id)]})
    token = r.json()["inviteUrl"].rsplit("/", 1)[1]
    cookie = client.post(f"/api/auth/staff/invite/{token}", json={"password": "staff123"}).cookies["clinically_session"]
    client.cookies.clear()
    staff = {**J, "Cookie": f"clinically_session={cookie}"}
    assert client.get(f"{doc['base']}/schedule", headers=staff).status_code == 403

    member = r.json()["member"]["id"]
    client.patch(f"/api/doctor/staff/{member}", headers=doc["auth"], json={"permissions": ["queue.view", "schedule.manage"]})
    assert client.get(f"{doc['base']}/schedule", headers=staff).status_code == 200
    r = client.put(f"{doc['base']}/weekly", headers=staff, json={"days": week(limit=20)})
    assert r.status_code == 200
    entries = client.get("/api/doctor/activity", headers=doc["auth"]).json()["entries"]
    assert entries[0]["action"] == "schedule.weekly" and entries[0]["actorName"] == "Desk Person"
