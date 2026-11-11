import pytest
from sqlalchemy import text

from app import notifications
from app.db import SessionLocal
from app.messaging import _fake
from app.models import OpdSession
from scripts import seed
from tests.test_queue import J, act, opd  # noqa: F401  (opd is a fixture)


@pytest.fixture(autouse=True)
def fake_sender():
    _fake.sent.clear()
    _fake.fail = False
    yield _fake
    _fake.fail = False


def walk_in(client, opd, **body):
    return client.post(f"/api/doctor/sessions/{opd['session']}/walk-ins", headers=opd["auth"],
                       json={"name": "Desk Patient", "age": 40, "gender": "MALE", **body})


def test_start_opd_tells_every_waiting_patient(client, opd, db, fake_sender):
    walk_in(client, opd)  # no phone: can't be messaged
    b = opd["bookings"][2]
    client.post(f"/api/public/bookings/{b['bookingId']}/cancel", json={"code": b["trackingCode"]})

    r = act(client, opd, "start")
    assert r.status_code == 200
    assert r.json()["notified"] == 3  # tokens 1, 2 and the walk-in; not the cancelled token 3
    # The reply is sent before the messages go out in the background.
    assert r.json()["notifications"] == {"pending": 2, "sent": 0, "failed": 0, "skipped": 1}

    phones = [p for p, _ in fake_sender.sent]
    assert phones == ["9431000001", "9431000002"]
    first, second = (t for _, t in fake_sender.sent)
    assert "has started OPD at Test Clinic" in first and "Your token is 1." in first and "You're next" in first
    assert "Your token is 2." in second and "1 patient before you." in second

    rows = db.execute(text("SELECT status, channel FROM notifications ORDER BY created_at")).all()
    assert sorted(rows) == [("SENT", "fake"), ("SENT", "fake"), ("SKIPPED", None)]

    day = client.get("/api/doctor/dashboard", headers=opd["auth"]).json()
    assert day["notifications"] == {"pending": 0, "sent": 2, "failed": 0, "skipped": 1}


def test_patient_sees_the_message_on_their_booking_page(client, opd):
    act(client, opd, "start")
    b = opd["bookings"][0]
    status = client.get(f"/api/public/bookings/{b['bookingId']}?code={b['trackingCode']}").json()
    assert status["sessionStatus"] == "IN_OPD" and status["sessionStartedAt"] is not None
    [message] = status["messages"]
    assert message["kind"] == "OPD_STARTED" and message["status"] == "SENT" and "Your token is 1." in message["text"]


def test_each_patient_is_told_only_once(client, opd, db, fake_sender):
    act(client, opd, "start")
    act(client, opd, "end")
    act(client, opd, "reopen")
    with SessionLocal() as s:
        session = s.get(OpdSession, opd["session"])
        assert notifications.queue_opd_started(s, session) == 0
        s.commit()
    notifications.deliver_due()
    assert len(fake_sender.sent) == 3
    assert db.scalar(text("SELECT count(*) FROM notifications")) == 3


def test_failed_sends_are_retried_then_given_up(client, opd, db, fake_sender):
    fake_sender.fail = True
    act(client, opd, "start")
    row = db.execute(text("SELECT status, attempts, last_error, next_attempt_at > now() FROM notifications LIMIT 1")).one()
    db.commit()
    assert row == ("PENDING", 1, "Provider unavailable (test)", True)

    # Nothing is due yet: the retry waits.
    assert notifications.deliver_due() == 0
    for _ in range(4):
        with db.begin():
            db.execute(text("UPDATE notifications SET next_attempt_at = now() - interval '1 second'"))
        notifications.deliver_due()
    assert set(db.execute(text("SELECT status, attempts FROM notifications")).all()) == {("FAILED", 5)}
    db.commit()

    # A provider coming back doesn't resend FAILED ones; PENDING ones go out.
    fake_sender.fail = False
    assert notifications.deliver_due() == 0


def test_recovery_sends_after_a_failure(client, opd, db, fake_sender):
    fake_sender.fail = True
    act(client, opd, "start")
    fake_sender.fail = False
    with db.begin():
        db.execute(text("UPDATE notifications SET next_attempt_at = now() - interval '1 second'"))
    assert notifications.deliver_due() == 3
    assert {s for (s,) in db.execute(text("SELECT status FROM notifications")).all()} == {"SENT"}


def test_demo_day_starts_not_started(db):
    seed.main()
    assert db.scalar(text("SELECT status FROM opd_sessions")) == "NOT_STARTED"
    assert db.scalar(text("SELECT count(*) FROM bookings WHERE status = 'WAITING'")) == 11
