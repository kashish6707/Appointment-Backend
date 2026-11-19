"""Free-trial rollout: auto-verified registration, trial end date, doctors adding clinics, and
patients' tracking pages joining live updates."""

from datetime import datetime, timedelta, timezone

import pytest

from app import realtime
from app.config import get_settings
from app.timeutil import today_ist
from tests.conftest import profile_body
from tests.test_doctor import sign_in

PHONE, PASSWORD = "9123456780", "secret123"
CLINIC = {
    "name": "Sanjeevani Clinic",
    "address": "Road No. 5, Rajendra Nagar",
    "areaKey": "kankarbagh",
    "phone": "9876500011",
    "fee": 400,
    "days": ["SAT", "MON", "TUE"],
    "start": "00:00",
    "end": "23:59",
    "limit": 30,
}


@pytest.fixture
def auto_verify():
    settings = get_settings()
    settings.auto_verify_doctors = True
    yield
    settings.auto_verify_doctors = False


def register(client):
    r = client.post(
        "/api/auth/doctor/register",
        json={"fullName": "Dr New Doctor", "method": "phone", "phone": PHONE, "password": PASSWORD, "profile": profile_body()},
    )
    assert r.status_code == 201, r.text
    return sign_in(client, PHONE, PASSWORD)


def test_registration_starts_a_90_day_trial(client):
    me = client.get("/api/doctor/me", headers=register(client)).json()["doctor"]
    ends = datetime.fromisoformat(me["trialEndsAt"])
    assert abs(ends - (datetime.now(timezone.utc) + timedelta(days=90))) < timedelta(minutes=1)
    assert me["verified"] is False  # default: an admin verifies


def test_auto_verify_lists_new_doctor_after_adding_clinic(client, auto_verify):
    cookie = register(client)
    assert client.get("/api/doctor/me", headers=cookie).json()["doctor"]["verified"] is True

    r = client.post("/api/doctor/clinic-details", headers=cookie, json=CLINIC)
    assert r.status_code == 201, r.text
    slot = r.json()
    assert slot["name"] == "Sanjeevani Clinic" and slot["area"] == "Kankarbagh" and slot["fee"] == 400
    assert slot["opdDays"] == ["MON", "TUE", "SAT"]  # in weekday order

    me = client.get("/api/doctor/me", headers=cookie).json()["doctor"]
    assert [c["doctorClinicId"] for c in me["clinics"]] == [slot["doctorClinicId"]]

    weekly = client.get(f"/api/doctor/clinics/{slot['doctorClinicId']}/schedule", headers=cookie).json()["weekly"]
    open_days = [d for d in weekly if d["open"]]
    assert [d["weekday"] for d in open_days] == ["MON", "TUE", "SAT"]
    assert all(d["limit"] == 30 for d in open_days)

    # Patients can find the clinic and see its booking days.
    home = client.get("/api/public/home").json()
    assert "Sanjeevani Clinic" in [c["name"] for c in home["clinics"]]
    days = client.get(f"/api/public/clinics/{slot['doctorClinicId']}/availability").json()["days"]
    assert any(d["bookable"] for d in days)


def test_add_clinic_validation(client):
    cookie = register(client)
    bad = [
        {**CLINIC, "areaKey": "nowhere"},
        {**CLINIC, "start": "14:00", "end": "10:00"},
        {**CLINIC, "days": []},
        {**CLINIC, "fee": -1},
        {**CLINIC, "name": " "},
    ]
    for body in bad:
        assert client.post("/api/doctor/clinic-details", headers=cookie, json=body).status_code in (400, 422), body
    assert client.get("/api/doctor/me", headers=cookie).json()["doctor"]["clinics"] == []
    assert client.post("/api/doctor/clinic-details", json=CLINIC).status_code == 401


def test_tracking_link_joins_only_its_booking_rooms(client, make_slot):
    slot = make_slot()
    r = client.post(
        "/api/public/bookings",
        json={
            "doctorClinicId": str(slot.id),
            "date": today_ist().isoformat(),
            "visitType": "NEW",
            "patient": {"name": "Ramesh Kumar", "phone": "9431012345", "age": 54, "gender": "MALE"},
            "recordsConsent": False,
        },
    )
    booking_id, code = r.json()["bookingId"], r.json()["trackingCode"]
    status = client.get(f"/api/public/bookings/{booking_id}", params={"code": code}).json()

    assert realtime._patient_rooms({"bookingId": booking_id, "code": code}) == [
        f"booking:{booking_id}",
        f"session:{status['sessionId']}",
    ]
    assert realtime._patient_rooms({"bookingId": booking_id, "code": "wrong"}) is None
    assert realtime._patient_rooms({"bookingId": "not-a-uuid", "code": code}) is None
    assert realtime._patient_rooms(None) is None


def test_photos_can_be_kept_in_the_database(client, db):
    settings = get_settings()
    settings.storage_backend = "db"
    try:
        cookie = register(client)
        r = client.get("/api/doctor/profile/photo", headers=cookie)
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/")
        from sqlalchemy import text

        assert db.scalar(text("SELECT count(*) FROM stored_files WHERE key LIKE 'doctors/%'")) == 1
        assert client.get("/api/media/doctors/x.jpg").status_code == 404  # private keys aren't public media
    finally:
        settings.storage_backend = "local"


def test_realtime_ticket_is_single_use_and_needs_sign_in(client):
    cookie = register(client)
    assert client.get("/api/auth/realtime-ticket").status_code == 401
    ticket = client.get("/api/auth/realtime-ticket", headers=cookie).json()["ticket"]
    token_hash = realtime._use_ticket(ticket)
    rooms, staff = realtime._rooms_for_session(token_hash)
    assert rooms[0].startswith("doctor:") and staff is None
    assert realtime._use_ticket(ticket) is None  # used up
    assert realtime._use_ticket("made-up") is None


def test_tracking_says_who_is_with_the_doctor(client, make_slot):
    slot = make_slot()
    doctor = sign_in(client, slot.doctor.phone, "secret123")

    def book(name, phone):
        r = client.post("/api/public/bookings", json={
            "doctorClinicId": str(slot.id), "date": today_ist().isoformat(), "visitType": "NEW",
            "patient": {"name": name, "phone": phone, "age": 40, "gender": "MALE"}, "recordsConsent": False})
        return r.json()

    first, second = book("Asha Devi", "9431000001"), book("Binod Kumar", "9431000002")
    track = lambda: client.get(f"/api/public/bookings/{second['bookingId']}", params={"code": second["trackingCode"]}).json()
    session_id = track()["sessionId"]
    assert track()["withDoctor"] is None and track()["patientsAhead"] == 1

    client.post(f"/api/doctor/sessions/{session_id}/start", headers=doctor, json={})
    client.post(f"/api/doctor/sessions/{session_id}/next", headers=doctor, json={})
    data = track()
    assert data["withDoctor"] == 1 and data["patientsAhead"] == 0

    # Token 1 marked seen without calling the next one: nobody is with the doctor.
    client.patch(f"/api/doctor/bookings/{first['bookingId']}/status", headers=doctor, json={"status": "DONE"})
    data = track()
    assert data["withDoctor"] is None and data["nowServing"] == 1
