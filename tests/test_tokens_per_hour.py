"""Tokens per hour: patients pick an hour, each hour has its own token numbers and limit."""

from datetime import time, timedelta

from app.timeutil import today_ist
from tests.test_doctor import sign_in
from tests.test_queue import J

PHONES = [f"94320000{i:02d}" for i in range(1, 30)]


def tomorrow():
    return today_ist() + timedelta(days=1)


def book(client, slot, i, hour=None, day=None):
    body = {"doctorClinicId": str(slot.id), "date": (day or tomorrow()).isoformat(), "visitType": "NEW",
            "patient": {"name": f"Patient {i}", "phone": PHONES[i], "age": 40, "gender": "MALE"}, "recordsConsent": False}
    if hour:
        body["slotStart"] = hour
    return client.post("/api/public/bookings", json=body)


def set_per_hour(client, slot, n):
    auth = {**J, **sign_in(client, slot.doctor.phone, "secret123")}
    r = client.put(f"/api/doctor/clinics/{slot.id}/tokens-per-hour", headers=auth, json={"tokensPerHour": n})
    assert r.status_code == 200, r.text
    assert r.json()["tokensPerHour"] == n
    return auth


def hours_of(client, slot, day=None):
    days = client.get(f"/api/public/clinics/{slot.id}/availability").json()["days"]
    return next(d for d in days if d["date"] == (day or tomorrow()).isoformat())


def test_hours_are_listed_with_places_left(client, make_slot):
    slot = make_slot(start=time(10, 0), end=time(12, 30))
    set_per_hour(client, slot, 3)
    d = hours_of(client, slot)
    assert [(h["start"], h["end"], h["capacity"]) for h in d["hours"]] == [("10:00", "11:00", 3), ("11:00", "12:00", 3), ("12:00", "12:30", 2)]
    assert d["left"] == 8


def test_token_numbers_follow_the_hour_and_hours_fill_up(client, make_slot):
    slot = make_slot(start=time(10, 0), end=time(12, 0))
    set_per_hour(client, slot, 2)
    assert book(client, slot, 1, "11:00").json()["token"] == 3  # 11-12 gets tokens 3-4
    assert book(client, slot, 2, "10:00").json()["token"] == 1
    assert book(client, slot, 3, "11:00").json()["token"] == 4
    r = book(client, slot, 4, "11:00")
    assert r.status_code == 409 and "full" in r.json()["message"].lower()
    assert book(client, slot, 5).status_code == 400  # must choose an hour
    assert book(client, slot, 6, "10:30").status_code == 400  # not one of the hours
    d = hours_of(client, slot)
    assert [h["left"] for h in d["hours"]] == [1, 0] and d["left"] == 1


def test_a_cancelled_place_can_be_booked_again(client, make_slot):
    slot = make_slot(start=time(10, 0), end=time(11, 0))
    set_per_hour(client, slot, 1)
    first = book(client, slot, 1, "10:00").json()
    assert book(client, slot, 2, "10:00").status_code == 409
    client.post(f"/api/public/bookings/{first['bookingId']}/cancel", json={"code": first["trackingCode"]})
    again = book(client, slot, 2, "10:00")
    assert again.status_code == 201 and again.json()["token"] == 1


def test_tracking_shows_the_chosen_hour(client, make_slot):
    slot = make_slot(start=time(10, 0), end=time(12, 0))
    set_per_hour(client, slot, 5)
    b = book(client, slot, 1, "11:00").json()
    s = client.get(f"/api/public/bookings/{b['bookingId']}", params={"code": b["trackingCode"]}).json()
    assert (s["slotStart"], s["slotEnd"]) == ("11:00", "12:00")
    assert s["expectedAt"] >= "11:00"


def test_walk_ins_take_the_next_hour_with_room_then_go_last(client, make_slot, db):
    from sqlalchemy import text

    slot = make_slot()  # all day, so "now" is inside OPD today
    auth = set_per_hour(client, slot, 1)
    r = client.post("/api/public/bookings", json={"doctorClinicId": str(slot.id), "date": today_ist().isoformat(), "visitType": "NEW",
                    "patient": {"name": "Online One", "phone": PHONES[20], "age": 30, "gender": "MALE"}, "recordsConsent": False,
                    "slotStart": hours_of(client, slot, today_ist())["hours"][-1]["start"]})
    assert r.status_code == 201, r.text
    session = db.scalar(text("SELECT id::text FROM opd_sessions"))
    db.commit()
    walk = client.post(f"/api/doctor/sessions/{session}/walk-ins", headers=auth, json={"name": "Desk One", "age": 50, "gender": "FEMALE"})
    assert walk.status_code == 201, walk.text
    assert walk.json()["booking"]["slotStart"] is not None


def test_moving_a_token_needs_an_hour_too(client, make_slot):
    slot = make_slot(start=time(10, 0), end=time(12, 0))
    set_per_hour(client, slot, 2)
    b = book(client, slot, 1, "10:00").json()
    later = (tomorrow() + timedelta(days=1)).isoformat()
    url = f"/api/public/bookings/{b['bookingId']}/reschedule"
    assert client.post(url, json={"code": b["trackingCode"], "date": later}).status_code == 400
    r = client.post(url, json={"code": b["trackingCode"], "date": later, "slotStart": "11:00"})
    assert r.status_code == 201 and r.json()["token"] == 3


def test_turning_the_limit_off(client, make_slot):
    slot = make_slot(start=time(10, 0), end=time(12, 0))
    set_per_hour(client, slot, 2)
    set_per_hour(client, slot, None)
    assert hours_of(client, slot)["hours"] == []
    assert book(client, slot, 1).json()["token"] == 1


def test_switching_on_mid_day_keeps_earlier_patients_first(client, make_slot, db):
    from sqlalchemy import text

    slot = make_slot()  # all day, so "now" is inside OPD today
    today = today_ist()
    early = [book(client, slot, i, day=today).json()["token"] for i in (1, 2, 3)]
    assert early == [1, 2, 3]
    auth = set_per_hour(client, slot, 2)
    session = db.scalar(text("SELECT id::text FROM opd_sessions"))
    db.commit()
    walk = client.post(f"/api/doctor/sessions/{session}/walk-ins", headers=auth, json={"name": "Desk One", "age": 50, "gender": "FEMALE"})
    assert walk.status_code == 201 and walk.json()["booking"]["token"] > 3
    last_hour = hours_of(client, slot, today)["hours"][-1]["start"]
    online = book(client, slot, 4, last_hour, day=today)
    assert online.status_code == 201 and online.json()["token"] > walk.json()["booking"]["token"]


def test_hour_numbers_start_at_one_on_a_fresh_day(client, make_slot):
    slot = make_slot(start=time(10, 0), end=time(12, 0))
    set_per_hour(client, slot, 2)
    assert book(client, slot, 1, "10:00").json()["token"] == 1
