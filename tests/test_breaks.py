from datetime import datetime, timedelta

from sqlalchemy import text

from app.timeutil import IST, now_ist
from tests.test_queue import J, act, opd  # noqa: F401  (opd is a fixture)


def pause(client, opd, **body):
    return client.post(f"/api/doctor/sessions/{opd['session']}/pause", headers=opd["auth"], json=body)


def status_of(client, booking):
    return client.get(f"/api/public/bookings/{booking['bookingId']}?code={booking['trackingCode']}").json()


def minutes_left(iso):
    return (datetime.fromisoformat(iso) - now_ist()).total_seconds() / 60


def test_break_has_a_set_length(client, opd):
    act(client, opd, "start")
    r = pause(client, opd, minutes=30)
    assert r.status_code == 200
    s = r.json()["session"]
    assert s["status"] == "ON_BREAK" and s["breakStartedAt"] is not None
    assert 29 < minutes_left(s["breakUntil"]) <= 30

    r = client.post(f"/api/doctor/sessions/{opd['session']}/resume", headers=opd["auth"])
    assert r.json()["session"]["breakUntil"] is None and r.json()["session"]["breakStartedAt"] is None


def test_break_without_a_length_is_15_minutes(client, opd):
    act(client, opd, "start")
    s = act(client, opd, "pause").json()["session"]
    assert 14 < minutes_left(s["breakUntil"]) <= 15


def test_break_length_is_checked(client, opd):
    act(client, opd, "start")
    for minutes in (0, 4, 181, "30", True):
        r = pause(client, opd, minutes=minutes)
        assert r.status_code == 400 and r.json() == {"message": "Choose a break from 5 to 180 minutes."}, minutes


def test_expected_times_move_by_the_break(client, opd, db):
    act(client, opd, "start")
    act(client, opd, "next")  # token 1 with the doctor; tokens 2 and 3 waiting
    before = status_of(client, opd["bookings"][2])
    # Usual 10 minutes each: what's left of token 1's time, then token 2's.
    assert before["patientsAhead"] == 1 and before["estimatedMinutes"] == 20

    pause(client, opd, minutes=30)
    during = status_of(client, opd["bookings"][2])
    assert during["sessionStatus"] == "ON_BREAK" and during["breakUntil"] is not None
    assert during["estimatedMinutes"] in (39, 40)  # 30 minutes of break + 10
    expected = datetime.combine(now_ist().date(), datetime.strptime(during["expectedAt"], "%H:%M").time(), tzinfo=IST)
    gap = (expected - now_ist()) % timedelta(days=1)  # the expected time may fall after midnight
    assert timedelta(minutes=34) <= gap <= timedelta(minutes=41)  # ~40 minutes, rounded down to 5

    day = client.get("/api/doctor/dashboard", headers=opd["auth"]).json()
    ids = [b["id"] for b in day["bookings"] if b["status"] == "WAITING"]
    assert list(day["expected"]) == ids
    assert day["expected"][ids[-1]] == during["expectedAt"]
    detail = client.get(f"/api/doctor/bookings/{ids[-1]}", headers=opd["auth"]).json()
    assert detail["expectedAt"] == during["expectedAt"]


def test_extend_and_overrun(client, opd, db):
    act(client, opd, "start")
    pause(client, opd, minutes=10)
    r = client.post(f"/api/doctor/sessions/{opd['session']}/break/extend", headers=opd["auth"], json={"minutes": 10})
    assert 19 < minutes_left(r.json()["session"]["breakUntil"]) <= 20

    # A break that has run over adds nothing: the doctor is due back now.
    with db.begin():
        db.execute(text("UPDATE opd_sessions SET break_until = now() - interval '5 minutes'"))
    assert status_of(client, opd["bookings"][1])["estimatedMinutes"] == 10
    # Extending an overrun break counts from now.
    r = client.post(f"/api/doctor/sessions/{opd['session']}/break/extend", headers=opd["auth"], json={"minutes": 10})
    assert 9 < minutes_left(r.json()["session"]["breakUntil"]) <= 10


def test_extend_needs_a_break_and_ending_clears_it(client, opd):
    act(client, opd, "start")
    r = client.post(f"/api/doctor/sessions/{opd['session']}/break/extend", headers=opd["auth"], json={"minutes": 10})
    assert r.status_code == 409
    pause(client, opd, minutes=20)
    s = act(client, opd, "end").json()["session"]
    assert s["status"] == "CLOSED" and s["breakUntil"] is None


def test_patients_page_hears_when_the_break_ends(client, opd, emitted):
    act(client, opd, "start")
    pause(client, opd, minutes=15)
    updates = [p for room, e, p in emitted if room == f"session:{opd['session']}" and e == "session.updated"]
    assert updates[-1]["status"] == "ON_BREAK" and updates[-1]["breakUntil"] is not None
