from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app.timeutil import today_ist
from tests.test_doctor import sign_in
from tests.test_public import book

J = {"Content-Type": "application/json"}


@pytest.fixture
def opd(client, make_slot, db):
    """A doctor with today's session holding three waiting online bookings (tokens 1-3)."""
    slot = make_slot()
    codes = [book(client, slot, phone=f"943100000{i}", name=f"Patient {i}").json() for i in (1, 2, 3)]
    session_id = db.scalar(text("SELECT id::text FROM opd_sessions"))
    db.commit()  # end the read transaction so tests can use db.begin()
    auth = {**J, **sign_in(client, slot.doctor.phone, "secret123")}
    return {"slot": slot, "session": session_id, "auth": auth, "bookings": codes}


def act(client, opd, action, session=None):
    return client.post(f"/api/doctor/sessions/{session or opd['session']}/{action}", headers=opd["auth"])


def statuses(db):
    return dict(db.execute(text("SELECT token, status FROM bookings ORDER BY token")).all())


# --- session status ---------------------------------------------------------------


def test_session_lifecycle(client, opd, emitted):
    r = act(client, opd, "start")
    assert r.status_code == 200
    assert r.json()["session"]["status"] == "IN_OPD"
    assert r.json()["session"]["startedAt"] is not None
    assert act(client, opd, "start").json() == {"message": "OPD has already started."}

    assert act(client, opd, "pause").json()["session"]["status"] == "ON_BREAK"
    assert act(client, opd, "pause").status_code == 409
    assert act(client, opd, "resume").json()["session"]["status"] == "IN_OPD"
    assert act(client, opd, "resume").status_code == 409
    assert act(client, opd, "end").json()["session"]["status"] == "CLOSED"
    for action in ("start", "pause", "resume", "end", "next"):
        assert act(client, opd, action).status_code == 409, action

    events = [(room, e) for room, e, _ in emitted]
    assert (f"doctor:{opd['slot'].doctor_id}", "session.updated") in events
    assert (f"session:{opd['session']}", "session.updated") in events


def test_only_todays_session_can_be_run(client, make_slot, db):
    slot = make_slot()
    book(client, slot, day=today_ist() + timedelta(days=1))
    session = db.scalar(text("SELECT id::text FROM opd_sessions"))
    auth = {**J, **sign_in(client, slot.doctor.phone, "secret123")}
    r = client.post(f"/api/doctor/sessions/{session}/start", headers=auth)
    assert r.status_code == 409 and "today's OPD" in r.json()["message"]
    # Ending a future day works: that's how a doctor closes it for bookings.
    assert client.post(f"/api/doctor/sessions/{session}/end", headers=auth).status_code == 200
    assert book(client, slot, phone="9431000009", day=today_ist() + timedelta(days=1)).status_code == 409


def test_other_doctors_session_is_not_found(client, opd, make_slot):
    other = make_slot()
    other_auth = {**J, **sign_in(client, other.doctor.phone, "secret123")}
    r = client.post(f"/api/doctor/sessions/{opd['session']}/start", headers=other_auth)
    assert r.status_code == 404
    assert client.post(f"/api/doctor/sessions/{opd['session']}/start", headers=J).status_code == 401


# --- call next --------------------------------------------------------------------


def test_call_next(client, opd, db, emitted):
    r = act(client, opd, "next")
    assert r.status_code == 409 and r.json()["message"] == "Start the OPD first."
    act(client, opd, "start")

    r = act(client, opd, "next")
    assert r.status_code == 200
    body = r.json()
    assert body["finished"] is None
    assert body["current"]["token"] == 1 and body["current"]["status"] == "WITH_DOCTOR"
    assert body["session"]["currentToken"] == 1
    assert statuses(db) == {1: "WITH_DOCTOR", 2: "WAITING", 3: "WAITING"}

    # Token 2 cancels; "next" finishes 1 and skips straight to 3.
    two = opd["bookings"][1]
    client.post(f"/api/public/bookings/{two['bookingId']}/cancel", json={"code": two["trackingCode"]})
    body = act(client, opd, "next").json()
    assert body["finished"]["token"] == 1 and body["finished"]["status"] == "DONE"
    assert body["current"]["token"] == 3
    assert statuses(db) == {1: "DONE", 2: "CANCELLED", 3: "WITH_DOCTOR"}

    # The patient's tracking page sees the queue move.
    three = opd["bookings"][2]
    s = client.get(f"/api/public/bookings/{three['bookingId']}", params={"code": three["trackingCode"]}).json()
    assert s["nowServing"] == 3 and s["status"] == "WITH_DOCTOR"
    assert (f"booking:{three['bookingId']}", "booking.updated") in [(room, e) for room, e, _ in emitted]

    # Queue empty: the last patient is finished and nobody is called.
    body = act(client, opd, "next").json()
    assert body["finished"]["token"] == 3 and body["current"] is None
    assert statuses(db)[3] == "DONE"


def test_call_next_on_break_is_refused(client, opd):
    act(client, opd, "start")
    act(client, opd, "pause")
    r = act(client, opd, "next")
    assert r.status_code == 409 and "break" in r.json()["message"]


def test_double_clicked_next_calls_patients_in_order(opd, db):
    with TestClient(app) as c:
        act(c, opd, "start")

    def click(_):
        with TestClient(app) as c:
            return act(c, opd, "next").status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(click, range(2))) == [200, 200]
    assert statuses(db) == {1: "DONE", 2: "WITH_DOCTOR", 3: "WAITING"}
    assert db.scalar(text("SELECT count(*) FROM bookings WHERE status = 'WITH_DOCTOR'")) == 1


def test_wait_estimate_uses_todays_consult_times(client, opd, db):
    act(client, opd, "start")
    act(client, opd, "next")  # token 1 with the doctor
    act(client, opd, "next")  # token 1 done, token 2 with the doctor
    with db.begin():  # make token 1's consult take exactly 6 minutes
        db.execute(text("UPDATE bookings SET completed_at = called_at + interval '6 minutes' WHERE token = 1"))
    three = opd["bookings"][2]
    s = client.get(f"/api/public/bookings/{three['bookingId']}", params={"code": three["trackingCode"]}).json()
    assert s["patientsAhead"] == 0  # token 2 is with the doctor, not waiting
    assert s["estimatedMinutes"] == 0

    act(client, opd, "end")
    s = client.get(f"/api/public/bookings/{three['bookingId']}", params={"code": three["trackingCode"]}).json()
    assert s["sessionStatus"] == "CLOSED" and s["estimatedMinutes"] is None


def test_end_finishes_current_patient_and_leaves_waiting(client, opd, db):
    act(client, opd, "start")
    act(client, opd, "next")
    act(client, opd, "end")
    assert statuses(db) == {1: "DONE", 2: "WAITING", 3: "WAITING"}


# --- outcomes ------------------------------------------------------------------------


def test_set_booking_status(client, opd, db, emitted):
    act(client, opd, "start")
    current = act(client, opd, "next").json()["current"]
    url = f"/api/doctor/bookings/{current['id']}/status"

    r = client.patch(url, json={"status": "SENT_FOR_TESTS"}, headers=opd["auth"])
    assert r.status_code == 200 and r.json()["status"] == "SENT_FOR_TESTS"
    assert db.scalar(text("SELECT completed_at IS NOT NULL FROM bookings WHERE token = 1"))
    assert (f"doctor:{opd['slot'].doctor_id}", "booking.updated") in [(room, e) for room, e, _ in emitted]

    # Waiting patient who never turned up.
    two = opd["bookings"][1]["bookingId"]
    r = client.patch(f"/api/doctor/bookings/{two}/status", json={"status": "NO_SHOW"}, headers=opd["auth"])
    assert r.json()["status"] == "NO_SHOW"
    # "Next" skips them.
    assert act(client, opd, "next").json()["current"]["token"] == 3

    r = client.patch(url, json={"status": "WAITING"}, headers=opd["auth"])
    assert r.status_code == 400 and "no-show" in r.json()["message"]


def test_cannot_change_a_cancelled_booking(client, opd):
    one = opd["bookings"][0]
    client.post(f"/api/public/bookings/{one['bookingId']}/cancel", json={"code": one["trackingCode"]})
    r = client.patch(f"/api/doctor/bookings/{one['bookingId']}/status", json={"status": "DONE"}, headers=opd["auth"])
    assert r.status_code == 409


def test_other_doctor_cannot_change_booking(client, opd, make_slot):
    other = make_slot()
    other_auth = {**J, **sign_in(client, other.doctor.phone, "secret123")}
    one = opd["bookings"][0]["bookingId"]
    r = client.patch(f"/api/doctor/bookings/{one}/status", json={"status": "DONE"}, headers=other_auth)
    assert r.status_code == 404


# --- walk-ins -------------------------------------------------------------------------


def test_walk_in_gets_next_token(client, opd, db, emitted):
    url = f"/api/doctor/sessions/{opd['session']}/walk-ins"
    r = client.post(url, json={"name": "Vikash Prasad", "age": 45, "gender": "MALE"}, headers=opd["auth"])
    assert r.status_code == 201
    data = r.json()
    assert data["booking"]["token"] == 4
    assert data["booking"]["source"] == "WALK_IN"
    assert data["booking"]["visitType"] == "NEW"
    assert data["booking"]["patient"]["phone"] is None
    assert data["trackingCode"]
    assert (f"doctor:{opd['slot'].doctor_id}", "booking.created") in [(room, e) for room, e, _ in emitted]

    # Online bookings continue after the walk-in.
    assert book(client, opd["slot"], phone="9431000009").json()["token"] == 5

    r = client.post(
        url,
        json={"name": "Kavita Jha", "phone": "12345", "age": 58, "gender": "FEMALE"},
        headers=opd["auth"],
    )
    assert r.status_code == 400 and "10-digit" in r.json()["message"]


def test_walk_in_for_patient_already_booked_is_refused(client, opd, db):
    # Patient 1 booked online; reception tries to add them again as a walk-in. Consent is untouched.
    url = f"/api/doctor/sessions/{opd['session']}/walk-ins"
    r = client.post(
        url, json={"name": "patient 1", "phone": "9431000001", "age": 54, "gender": "MALE"}, headers=opd["auth"]
    )
    assert r.status_code == 409 and "already has token 1" in r.json()["message"]
    assert db.scalar(text("SELECT count(*) FROM patients WHERE records_consent_at IS NOT NULL")) == 3


def test_walk_ins_only_today_and_not_after_closing(client, opd, make_slot, db):
    url = f"/api/doctor/sessions/{opd['session']}/walk-ins"
    act(client, opd, "end")
    r = client.post(url, json={"name": "Late Patient", "age": 30, "gender": "OTHER"}, headers=opd["auth"])
    assert r.status_code == 409

    book(client, opd["slot"], phone="9431000008", day=today_ist() + timedelta(days=2))
    future = db.scalar(text("SELECT id::text FROM opd_sessions WHERE date > :d"), {"d": today_ist()})
    r = client.post(
        f"/api/doctor/sessions/{future}/walk-ins",
        json={"name": "Early Patient", "age": 30, "gender": "OTHER"},
        headers=opd["auth"],
    )
    assert r.status_code == 409 and "today's OPD" in r.json()["message"]
