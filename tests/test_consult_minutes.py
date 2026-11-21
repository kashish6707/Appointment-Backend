"""Minutes per patient: the clinic's usual time, a time per patient, and the messages that follow."""

from datetime import timedelta

from sqlalchemy import text

from app import notifications
from app.messaging import _fake
from tests.test_whatsapp import PHONES, book, sent  # noqa: F401  (fixture helpers)
from tests.test_doctor import sign_in
from tests.test_queue import J

import pytest


@pytest.fixture(autouse=True)
def fake_sender():
    _fake.sent.clear()
    _fake.messages.clear()
    _fake.fail = False


@pytest.fixture
def opd(client, make_slot, db):
    slot = make_slot()
    bookings = [book(client, slot, i) for i in (1, 2, 3, 4, 5)]
    session = db.scalar(text("SELECT id::text FROM opd_sessions"))
    db.commit()
    auth = {**J, **sign_in(client, slot.doctor.phone, "secret123")}
    do = lambda action, **body: client.post(f"/api/doctor/sessions/{session}/{action}", headers=auth, json=body or {})
    return {"slot": slot, "bookings": bookings, "do": do, "auth": auth, "session": session}


def wait(client, b):
    return client.get(f"/api/public/bookings/{b['bookingId']}", params={"code": b["trackingCode"]}).json()["estimatedMinutes"]


def usual(client, opd, minutes):
    r = client.put(f"/api/doctor/clinics/{opd['slot'].id}/minutes-per-patient", headers=opd["auth"], json={"minutesPerPatient": minutes})
    assert r.status_code == 200 and r.json()["minutesPerPatient"] == minutes, r.text


def needs(client, opd, b, minutes):
    r = client.put(f"/api/doctor/bookings/{b['bookingId']}/consult-minutes", headers=opd["auth"], json={"minutes": minutes})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_clinic_time_and_a_patients_time_set_the_wait(client, opd):
    usual(client, opd, 5)
    opd["do"]("start")
    opd["do"]("next")  # token 1 with the doctor
    b = opd["bookings"]
    assert [wait(client, x) for x in b[1:]] == [5, 10, 15, 20]
    assert needs(client, opd, b[2], 30)["consultMinutes"] == 30  # token 3 is a long case
    assert [wait(client, x) for x in b[1:]] == [5, 10, 40, 45]
    needs(client, opd, b[2], None)  # back to the usual time
    assert wait(client, b[4]) == 20


def test_a_long_case_tells_later_patients_their_new_time(client, opd):
    usual(client, opd, 10)
    opd["do"]("start")
    opd["do"]("next")  # token 1 in; 2 is next; 3 at 20 min (got "OPD started"); 4 at 30; 5 at 40
    assert [p for p, _ in sent("RUNNING_LATE")] == []
    needs(client, opd, opd["bookings"][0], 40)  # token 1 needs 40 minutes: everyone moves 30 later
    late = sent("RUNNING_LATE")
    assert [p for p, _ in late] == PHONES[2:5]  # tokens 3, 4, 5 (2 is next: "you're next" covers it)
    assert "is running late" in late[0][1] and "your token 3" in late[0][1]
    needs(client, opd, opd["bookings"][0], 60)  # 20 minutes more: but they were told just now
    assert len(sent("RUNNING_LATE")) == 3


def test_get_ready_goes_as_time_passes(client, opd, db):
    usual(client, opd, 10)
    opd["do"]("start")
    opd["do"]("next")  # token 1 in at 0 min; token 4 is 30 minutes away
    assert sent("GET_READY") == []
    # 15 minutes later, still with token 1 (who needs 10): token 4 is now ~20 minutes away.
    with db.begin():
        db.execute(text("UPDATE bookings SET called_at = called_at - interval '15 minutes' WHERE status = 'WITH_DOCTOR'"))
    notifications.tick()
    notifications.deliver_due()
    assert [p for p, _ in sent("GET_READY")] == [PHONES[3]]


def test_tick_leaves_an_idle_database_alone(client, opd, monkeypatch):
    notifications.tick()  # nothing running, nothing to send: both flags clear
    assert not notifications._opd_running.is_set() and not notifications._messages_waiting.is_set()
    calls = []
    monkeypatch.setattr(notifications, "SessionLocal", lambda: calls.append(1))
    notifications.tick()
    assert calls == []
    monkeypatch.undo()
    opd["do"]("start")  # an OPD starts: the tick looks again
    assert notifications._opd_running.is_set()


def test_only_active_patients_get_a_time(client, opd):
    opd["do"]("start")
    b = opd["bookings"][0]
    client.post(f"/api/public/bookings/{b['bookingId']}/cancel", json={"code": b["trackingCode"]})
    r = client.put(f"/api/doctor/bookings/{b['bookingId']}/consult-minutes", headers=opd["auth"], json={"minutes": 20})
    assert r.status_code == 409
    r = client.put(f"/api/doctor/bookings/{opd['bookings'][1]['bookingId']}/consult-minutes", headers=opd["auth"], json={"minutes": 500})
    assert r.status_code == 400
