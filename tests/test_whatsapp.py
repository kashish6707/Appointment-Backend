"""WhatsApp token updates: which messages go out when, opt-in, Hindi, and the Cloud API call."""

import pytest
from sqlalchemy import text

from app import messaging, notifications
from app.messaging import PermanentSendError, SendError, WhatsAppSender, _fake
from app.timeutil import today_ist
from tests.test_doctor import sign_in
from tests.test_queue import J

PHONES = [f"94310000{i:02d}" for i in range(1, 6)]


@pytest.fixture(autouse=True)
def fake_sender():
    _fake.sent.clear()
    _fake.messages.clear()
    _fake.fail = False
    yield _fake


def book(client, slot, i, **over):
    r = client.post("/api/public/bookings", json={
        "doctorClinicId": str(slot.id), "date": today_ist().isoformat(), "visitType": "NEW",
        "patient": {"name": f"Patient {i}", "phone": PHONES[i - 1], "age": 40, "gender": "MALE"}, "recordsConsent": False, **over})
    assert r.status_code == 201, r.text
    return r.json()


def sent(kind):
    """(phone, body) of messages of this template that were sent, in order."""
    return [(m.phone, m.body) for m in _fake.messages if m.template == notifications.KINDS[kind].template]


@pytest.fixture
def clinic(client, make_slot, db):
    slot = make_slot()
    bookings = [book(client, slot, i) for i in (1, 2, 3, 4)]
    session = db.scalar(text("SELECT id::text FROM opd_sessions"))
    db.commit()
    auth = {**J, **sign_in(client, slot.doctor.phone, "secret123")}
    do = lambda action, **body: client.post(f"/api/doctor/sessions/{session}/{action}", headers=auth, json=body or {})
    return {"slot": slot, "bookings": bookings, "do": do, "auth": auth, "session": session}


def test_booking_sends_the_token_with_a_live_link(clinic):
    [(phone, body)] = sent("BOOKED")[:1]
    assert phone == PHONES[0]
    assert "your token number is 1 for Dr Test" in body and "Test Clinic" in body and "Please come by 12 am" in body
    link = body.split("live: ")[1].split(" ")[0]
    assert link.startswith("http://localhost:3000/track/")
    assert len(sent("BOOKED")) == 4


def test_the_queue_moving_tells_the_right_patients(clinic):
    do = clinic["do"]
    do("start")
    assert [p for p, _ in sent("OPD_STARTED")] == PHONES[:4]
    # 10 minutes each: tokens 2 and 3 are within 20 minutes, so "OPD started" covers them too.
    assert sent("NEXT") == [] and sent("GET_READY") == []

    do("next")  # token 1 goes in
    assert sent("YOUR_TURN") == [(PHONES[0], "Clinically: It's your turn. Token 1, please go in to see Dr Test 1 now. Thank you")]
    assert [p for p, _ in sent("NEXT")] == [PHONES[1]]  # token 2 is next
    assert sent("GET_READY") == []  # token 4 is still 30 minutes away

    do("next")  # token 2 goes in fast: 3 minutes each now (the least), so token 4 is close
    assert [p for p, _ in sent("YOUR_TURN")] == PHONES[:2]
    assert [p for p, _ in sent("NEXT")] == PHONES[1:3]
    [(phone, body)] = sent("GET_READY")
    assert phone == PHONES[3] and "your turn for token 4 with Dr Test" in body and "is in about 6 minutes" in body


def test_breaks_and_closing(clinic):
    do = clinic["do"]
    do("start")
    do("pause", minutes=15)
    breaks = sent("BREAK")
    assert len(breaks) == 4 and "on a short break of 15 minutes" in breaks[0][1]
    do("resume")
    do("pause", minutes=5)  # a second break gets its own message
    assert len(sent("BREAK")) == 8 and "of 5 minutes" in sent("BREAK")[-1][1]
    do("resume")
    do("end")
    assert sorted(p for p, _ in sent("OPD_CLOSED")) == PHONES[:4]


def test_no_whatsapp_without_opt_in_or_number(client, make_slot, db):
    slot = make_slot()
    book(client, slot, 1, whatsapp=False)
    assert sent("BOOKED") == []
    status, reason = db.execute(text("SELECT status, last_error FROM notifications WHERE kind = 'BOOKED'")).one()
    assert (status, reason) == ("SKIPPED", "Didn't ask for WhatsApp updates")


def test_hindi_bookings_get_hindi_messages(client, make_slot):
    slot = make_slot()
    book(client, slot, 1, lang="hi")
    [m] = [m for m in _fake.messages if m.template == "clinically_token_booked"]
    assert m.lang == "hi" and "आपका टोकन नंबर 1 है" in m.body


def test_walk_in_with_phone_gets_messages(clinic, client):
    r = client.post(f"/api/doctor/sessions/{clinic['session']}/walk-ins", headers=clinic["auth"],
                    json={"name": "Desk Patient", "age": 50, "gender": "FEMALE", "phone": PHONES[4]})
    assert r.status_code == 201
    assert sent("BOOKED")[-1][0] == PHONES[4]
    r = client.post(f"/api/doctor/sessions/{clinic['session']}/walk-ins", headers=clinic["auth"],
                    json={"name": "No Phone", "age": 50, "gender": "FEMALE"})
    assert r.status_code == 201 and len(sent("BOOKED")) == 5


def test_clinic_cancelling_a_day_tells_patients(clinic, client):
    slot_id = str(clinic["slot"].id)
    r = client.post(f"/api/doctor/clinics/{slot_id}/exceptions", headers=clinic["auth"],
                    json={"from": today_ist().isoformat(), "kind": "CLOSED", "confirmCancel": True})
    assert r.status_code in (200, 201), r.text
    notifications.deliver_due()
    cancelled = sent("CANCELLED")
    assert len(cancelled) == 4 and "is cancelled because the doctor is not available" in cancelled[0][1]


# --- the WhatsApp Cloud API -------------------------------------------------------------


class Msg:
    phone, template, lang, body = "9431000001", "clinically_your_turn", "hi", "x"
    params = ["7", "Dr Anand"]


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def test_whatsapp_sender_sends_the_template(monkeypatch):
    calls = []
    monkeypatch.setattr(messaging.httpx, "post", lambda url, **kw: calls.append((url, kw)) or Resp(200, {"messages": [{"id": "wamid.1"}]}))
    assert WhatsAppSender("TOKEN", "12345").send(Msg()) == "wamid.1"
    url, kw = calls[0]
    assert url.endswith("/12345/messages") and kw["headers"]["Authorization"] == "Bearer TOKEN"
    tpl = kw["json"]["template"]
    assert kw["json"]["to"] == "919431000001" and tpl["name"] == "clinically_your_turn" and tpl["language"] == {"code": "hi"}
    assert tpl["components"][0]["parameters"] == [{"type": "text", "text": "7"}, {"type": "text", "text": "Dr Anand"}]


def test_whatsapp_errors_retry_or_give_up(monkeypatch):
    sender = WhatsAppSender("TOKEN", "12345")
    monkeypatch.setattr(messaging.httpx, "post", lambda url, **kw: Resp(400, {"error": {"code": 131026, "message": "Not on WhatsApp"}}))
    with pytest.raises(PermanentSendError):
        sender.send(Msg())
    monkeypatch.setattr(messaging.httpx, "post", lambda url, **kw: Resp(429, {"error": {"code": 130429, "message": "Rate limit"}}))
    with pytest.raises(SendError) as e:
        sender.send(Msg())
    assert not isinstance(e.value, PermanentSendError)


def test_templates_and_texts_match():
    """Every message has English and Hindi with the same numbered parameters."""
    import re

    for kind in notifications.KINDS.values():
        en, hi = (sorted(set(re.findall(r"\{(\d)\}", t))) for t in (kind.en, kind.hi))
        assert en == hi and en == [str(i) for i in range(1, len(en) + 1)], kind.template
        assert not kind.en.startswith("{") and not kind.en.rstrip().endswith("}")  # Meta rejects these
