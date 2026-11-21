"""Public live queue status: on the clinic cards, and pushed when the queue moves."""

from app import directory, realtime
from tests.test_queue import act, opd  # noqa: F401  (fixture)


def live(emitted):
    return [p for room, event, p in emitted if event == "live.updated"]


def test_cards_show_the_token_with_the_doctor(client, opd):
    act(client, opd, "start")
    act(client, opd, "next")
    directory.clear_cache()
    home = client.get("/api/public/home").json()
    card = home["clinics"][0]["doctors"][0]
    assert card["sessionStatus"] == "IN_OPD" and card["withDoctor"] == 1 and card["waitingToday"] == 2


def test_queue_changes_push_live_updates(client, opd, emitted):
    act(client, opd, "start")
    assert live(emitted)[-1] == {**live(emitted)[-1], "sessionStatus": "IN_OPD", "withDoctor": None, "waiting": 3}
    act(client, opd, "next")
    act(client, opd, "next")
    last = live(emitted)[-1]
    assert last["withDoctor"] == 2 and last["waiting"] == 1
    assert all("patient" not in p and "name" not in p for p in live(emitted))  # nothing about patients
    rooms = {room for room, event, _ in emitted if event == "live.updated"}
    assert len(rooms) == 1 and next(iter(rooms)).startswith("live:")


def test_watch_rooms_are_checked():
    assert realtime._watch_rooms({"watch": ["0f6d6c0e-6b9e-4b8e-9a1c-2f6c2b9b1a11"]}) == ["live:0f6d6c0e-6b9e-4b8e-9a1c-2f6c2b9b1a11"]
    assert realtime._watch_rooms({"watch": ["not-an-id"]}) is None
    assert realtime._watch_rooms({"watch": ["0f6d6c0e-6b9e-4b8e-9a1c-2f6c2b9b1a11"] * 61}) is None
    assert realtime._watch_rooms({"watch": "x"}) is None
