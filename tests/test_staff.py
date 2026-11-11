from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import text

from app import realtime
from tests.conftest import profile_body
from app.db import SessionLocal
from app.models import Clinic, DoctorClinic, WEEKDAYS
from tests.test_doctor import sign_in
from tests.test_public import book
from tests.test_queue import J, act, opd  # noqa: F401  (opd is a fixture)

COMPOUNDER = ["queue.view", "queue.call", "walkins.add", "bookings.outcome"]


def add_staff(client, auth, slot_ids, *, phone="9876500001", role="COMPOUNDER", permissions=COMPOUNDER, name="Sunil Kumar"):
    return client.post("/api/doctor/staff", headers={**J, **auth}, json={
        "fullName": name, "phone": phone, "role": role, "permissions": permissions,
        "doctorClinicIds": [str(i) for i in slot_ids],
    })


def accept(client, invite_url, password="staff123"):
    token = invite_url.rsplit("/", 1)[1]
    r = client.post(f"/api/auth/staff/invite/{token}", json={"password": password})
    assert r.status_code == 200, r.text
    cookie = r.cookies["clinically_session"]
    client.cookies.clear()
    return {**J, "Cookie": f"clinically_session={cookie}"}


def staff_login(client, phone="9876500001", password="staff123"):
    r = client.post("/api/auth/login", json={"method": "phone", "phone": phone, "password": password})
    assert r.status_code == 200, r.text
    cookie = r.cookies["clinically_session"]
    client.cookies.clear()
    return {**J, "Cookie": f"clinically_session={cookie}"}


@pytest.fixture
def team(client, opd):
    """opd's doctor with a compounder who has accepted their invite."""
    r = add_staff(client, opd["auth"], [opd["slot"].id])
    assert r.status_code == 201, r.text
    staff_auth = accept(client, r.json()["inviteUrl"])
    return {**opd, "member": r.json()["member"], "staff": staff_auth}


# --- adding staff and invites -------------------------------------------------------------


def test_add_staff_invite_and_sign_in(client, opd):
    r = add_staff(client, opd["auth"], [opd["slot"].id], permissions=["queue.call"])
    assert r.status_code == 201
    member = r.json()["member"]
    assert member["status"] == "INVITED" and member["role"] == "COMPOUNDER"
    assert member["permissions"] == ["queue.view", "queue.call"]  # queue.view is always included
    url = r.json()["inviteUrl"]
    assert url.startswith("/staff/invite/")

    token = url.rsplit("/", 1)[1]
    details = client.get(f"/api/auth/staff/invite/{token}").json()
    assert details["fullName"] == "Sunil Kumar" and details["doctors"] == [opd["slot"].doctor.full_name]

    # A staff member can't sign in before setting a password.
    r = client.post("/api/auth/login", json={"method": "phone", "phone": "9876500001", "password": "anything1"})
    assert r.status_code == 401

    accept(client, url)
    assert client.get(f"/api/auth/staff/invite/{token}").status_code == 404  # used once only
    auth = staff_login(client)
    me = client.get("/api/doctor/me", headers=auth).json()
    assert me["viewer"]["type"] == "STAFF" and me["viewer"]["role"] == "COMPOUNDER"
    assert me["viewer"]["permissions"] == ["queue.call", "queue.view"]
    assert me["doctor"]["id"] == str(opd["slot"].doctor_id)
    assert me["doctor"]["phone"] is None  # the doctor's own contact details stay private
    assert client.get("/api/doctor/staff", headers=opd["auth"]).json()["staff"][0]["status"] == "ACTIVE"


def test_expired_invite_and_new_link(client, opd, db):
    url = add_staff(client, opd["auth"], [opd["slot"].id]).json()["inviteUrl"]
    with db.begin():
        db.execute(text("UPDATE staff SET invite_expires_at = now() - interval '1 minute'"))
    token = url.rsplit("/", 1)[1]
    assert client.get(f"/api/auth/staff/invite/{token}").status_code == 404
    member = client.get("/api/doctor/staff", headers=opd["auth"]).json()["staff"][0]
    assert member["status"] == "INVITE_EXPIRED"

    new = client.post(f"/api/doctor/staff/{member['id']}/invite", headers=opd["auth"]).json()["inviteUrl"]
    assert new != url
    accept(client, new)
    r = client.post(f"/api/doctor/staff/{member['id']}/invite", headers=opd["auth"])
    assert r.status_code == 409


def test_contact_rules(client, opd, make_slot):
    other = make_slot()
    r = add_staff(client, opd["auth"], [opd["slot"].id], phone=other.doctor.phone)
    assert r.status_code == 409 and "doctor's account" in r.json()["message"]
    add_staff(client, opd["auth"], [opd["slot"].id])
    assert add_staff(client, opd["auth"], [opd["slot"].id]).status_code == 409  # already on staff
    # A staff member's number can't register as a doctor.
    r = client.post("/api/auth/doctor/register", json={"fullName": "Dr Someone", "method": "phone", "phone": "9876500001", "password": "secret123", "profile": profile_body()})
    assert r.status_code == 409
    r = client.post("/api/doctor/staff", headers={**J, **opd["auth"]}, json={
        "fullName": "Both", "phone": "9876500002", "email": "a@b.co", "role": "COMPOUNDER",
        "permissions": [], "doctorClinicIds": [str(opd["slot"].id)]})
    assert r.status_code == 400


def test_bad_clinics_and_permissions(client, opd, make_slot):
    other = make_slot()
    assert add_staff(client, opd["auth"], [other.id]).status_code == 400
    r = add_staff(client, opd["auth"], [opd["slot"].id], permissions=["staff.manage"])
    assert r.status_code == 400 and r.json() == {"message": "Unknown permission: staff.manage."}


# --- what staff can and can't do -----------------------------------------------------------


def test_compounder_permissions(client, team, db):
    staff = team["staff"]
    session = team["session"]
    # Running OPD needs opd.manage.
    r = client.post(f"/api/doctor/sessions/{session}/start", headers=staff)
    assert r.status_code == 403 and "start, pause, end and reopen opd" in r.json()["message"]
    act(client, team, "start")
    # Calling patients and walk-ins are allowed.
    assert client.post(f"/api/doctor/sessions/{session}/next", headers=staff).status_code == 200
    r = client.post(f"/api/doctor/sessions/{session}/walk-ins", headers=staff, json={"name": "Desk Patient", "age": 40, "gender": "MALE"})
    assert r.status_code == 201
    # Tests need tests.manage; staff management is root only.
    bid = db.scalar(text("SELECT id::text FROM bookings ORDER BY token LIMIT 1"))
    db.commit()
    assert client.post(f"/api/doctor/bookings/{bid}/tests", headers=staff, json={"names": ["CBC"]}).status_code == 403
    assert client.get("/api/doctor/staff", headers=staff).json() == {"message": "Only the doctor can do this."}
    assert client.get("/api/doctor/activity", headers=staff).status_code == 403
    # Records stay hidden without records.view, even when the patient consented.
    detail = client.get(f"/api/doctor/bookings/{bid}", headers=staff).json()
    assert detail["recordsConsent"] is True and detail["canViewRecords"] is False and detail["records"] == []


def test_activity_log_records_who_did_what(client, team):
    act(client, team, "start")
    client.post(f"/api/doctor/sessions/{team['session']}/next", headers=team["staff"])
    entries = client.get("/api/doctor/activity", headers=team["auth"]).json()["entries"]
    assert [(e["actorKind"], e["actorName"], e["action"]) for e in entries[:3]] == [
        ("STAFF", "Sunil Kumar", "queue.next"),
        ("DOCTOR", team["slot"].doctor.full_name, "opd.start"),
        ("DOCTOR", team["slot"].doctor.full_name, "staff.add"),
    ]


def test_permission_changes_apply_at_once(client, team):
    staff = team["staff"]
    member = team["member"]["id"]
    assert client.post(f"/api/doctor/sessions/{team['session']}/start", headers=staff).status_code == 403
    r = client.patch(f"/api/doctor/staff/{member}", headers=team["auth"], json={"permissions": COMPOUNDER + ["opd.manage"]})
    assert "opd.manage" in r.json()["permissions"]
    assert client.post(f"/api/doctor/sessions/{team['session']}/start", headers=staff).status_code == 200


def test_removed_staff_lose_access(client, team, db):
    staff = team["staff"]
    assert client.delete(f"/api/doctor/staff/{team['member']['id']}", headers=team["auth"]).status_code == 204
    r = client.get("/api/doctor/dashboard", headers=staff)
    assert r.status_code == 409 and "no longer have access" in r.json()["message"]
    me = client.get("/api/doctor/me", headers=staff).json()
    assert me["doctor"] is None and me["workplaces"] == []
    assert db.scalar(text("SELECT acting_doctor_id FROM sessions WHERE staff_id IS NOT NULL")) is None


# --- clinic limits ---------------------------------------------------------------------------


def second_slot(slot):
    with SessionLocal() as s:
        clinic = Clinic(name="Second Clinic", area="Kankarbagh", address="2 Other Road, Patna")
        dc = DoctorClinic(doctor_id=slot.doctor_id, clinic=clinic, fee=300, opd_days=list(WEEKDAYS),
                          opd_start=time(0, 0), opd_end=time(23, 59))
        s.add_all([clinic, dc])
        s.commit()
        return dc


def test_staff_only_see_their_clinics(client, team):
    other = second_slot(team["slot"])
    book(client, other, phone="9431088888", name="Other Clinic Patient")
    staff = team["staff"]
    me = client.get("/api/doctor/me", headers=staff).json()
    assert [c["doctorClinicId"] for c in me["doctor"]["clinics"]] == [str(team["slot"].id)]
    assert client.get(f"/api/doctor/dashboard?doctorClinicId={other.id}", headers=staff).status_code == 404
    assert client.get("/api/doctor/search?q=other", headers=staff).json()["results"] == []
    # The doctor sees both.
    assert len(client.get("/api/doctor/search?q=other", headers=team["auth"]).json()["results"]) == 1


def test_staff_live_updates_are_limited_to_their_clinics(client, team):
    rooms, who = realtime._rooms(team["staff"]["Cookie"])
    assert rooms == [f"slot:{team['slot'].id}"]
    assert who == (team["member"]["staffId"], str(team["slot"].doctor_id))
    assert realtime._rooms(team["auth"]["Cookie"])[0] == [f"doctor:{team['slot'].doctor_id}"]


# --- one person, several doctors -----------------------------------------------------------


def test_staff_working_for_two_doctors(client, team, make_slot):
    other = make_slot()
    other_auth = {**J, **sign_in(client, other.doctor.phone, "secret123")}
    r = add_staff(client, other_auth, [other.id], role="RECEPTIONIST", permissions=["queue.view", "walkins.add"])
    assert r.status_code == 201
    assert r.json()["inviteUrl"] is None  # they already have an account
    assert r.json()["member"]["status"] == "ACTIVE"

    staff = staff_login(client)  # a fresh sign-in hasn't picked a doctor yet
    me = client.get("/api/doctor/me", headers=staff).json()
    assert me["doctor"] is None and len(me["workplaces"]) == 2
    assert client.get("/api/doctor/dashboard", headers=staff).json() == {"message": "Choose which doctor you're working for."}

    r = client.post("/api/auth/staff/workplace", headers=staff, json={"doctorId": str(other.doctor_id)})
    assert r.status_code == 204
    me = client.get("/api/doctor/me", headers=staff).json()
    assert me["doctor"]["id"] == str(other.doctor_id) and me["viewer"]["role"] == "RECEPTIONIST"
    # Each doctor's grant is separate: this one doesn't allow calling patients.
    session = client.get("/api/doctor/dashboard", headers=staff).json()["session"]["id"]
    assert client.post(f"/api/doctor/sessions/{session}/next", headers=staff).status_code == 403

    r = client.post("/api/auth/staff/workplace", headers=staff, json={"doctorId": "00000000-0000-0000-0000-000000000000"})
    assert r.status_code == 404


# --- correcting patient details --------------------------------------------------------------


def test_correct_patient_details(client, team, make_slot, db):
    bid = db.scalar(text("SELECT id::text FROM bookings ORDER BY token LIMIT 1"))
    db.commit()
    staff = team["staff"]
    # A compounder doesn't have patients.correct.
    r = client.patch(f"/api/doctor/bookings/{bid}/patient", headers=staff, json={"age": 45})
    assert r.status_code == 403

    client.patch(f"/api/doctor/staff/{team['member']['id']}", headers=team["auth"], json={"permissions": COMPOUNDER + ["patients.correct"]})
    r = client.patch(f"/api/doctor/bookings/{bid}/patient", headers=staff, json={"name": "Patient One Kumar", "age": 45, "heightCm": 170})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["patient"]["name"] == "Patient One Kumar" and data["patient"]["age"] == 45
    assert data["patient"]["heightCm"] == 170 and data["patient"]["corrected"] is True
    assert data["correction"]["updatedBy"] == "Sunil Kumar"
    assert data["correction"]["original"] == {"name": "Patient 1", "age": 54, "gender": "MALE"}

    # The shared patient row is unchanged, and other doctors still see the original.
    assert db.execute(text("SELECT name, age FROM patients WHERE phone = '9431000001'")).one() == ("Patient 1", 54)
    db.commit()
    other = make_slot()
    book(client, other, phone="9431000001", name="Patient 1")
    other_auth = {**J, **sign_in(client, other.doctor.phone, "secret123")}
    other_day = client.get("/api/doctor/dashboard", headers=other_auth).json()
    assert other_day["bookings"][0]["patient"]["name"] == "Patient 1"
    assert other_day["bookings"][0]["patient"]["corrected"] is False
    # This doctor's dashboard shows the correction.
    day = client.get("/api/doctor/dashboard", headers=team["auth"]).json()
    assert day["bookings"][0]["patient"]["name"] == "Patient One Kumar"

    # Setting a field back to the patient's own value drops that correction; all back drops it all.
    r = client.patch(f"/api/doctor/bookings/{bid}/patient", headers=staff, json={"name": "Patient 1", "age": 54})
    assert r.json()["correction"] is None and r.json()["patient"]["corrected"] is False

    entries = client.get("/api/doctor/activity", headers=team["auth"]).json()["entries"]
    first_fix = next(e for e in reversed(entries) if e["action"] == "patient.correct")
    assert first_fix["actorName"] == "Sunil Kumar"
    assert first_fix["detail"]["changes"]["age"] == {"old": 54, "new": 45}


def test_phone_and_consent_cannot_be_corrected(client, team, db):
    bid = db.scalar(text("SELECT id::text FROM bookings ORDER BY token LIMIT 1"))
    db.commit()
    for body, message in [
        ({"phone": "9000000000"}, "Unexpected field: phone."),
        ({"recordsConsent": False}, "Unexpected field: recordsConsent."),
        ({}, "Change at least one detail."),
        ({"age": 200}, "Enter the patient's age in years, from 0 to 120."),
    ]:
        r = client.patch(f"/api/doctor/bookings/{bid}/patient", headers=team["auth"], json=body)
        assert r.status_code == 400 and r.json() == {"message": message}, body
