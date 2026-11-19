"""Patient accounts: sign up and in, forgot password, family profiles, records, bookings, and
what doctors may see."""

import base64
import re

from sqlalchemy import text

from app import email
from app.timeutil import today_ist
from tests.test_doctor import sign_in as doctor_sign_in

PHONE, PASSWORD = "9876543210", "health123"
PDF = "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4\n" + b"x" * 200).decode()
HTML = "data:text/html;base64," + base64.b64encode(b"<script>alert(1)</script>" * 10).decode()


def cookie_of(r) -> dict:
    token = r.cookies["clinically_patient"]
    return {"Cookie": f"clinically_patient={token}"}


def register(client, **body):
    payload = {"fullName": "Sunita Devi", "method": "phone", "phone": PHONE, "password": PASSWORD, **body}
    r = client.post("/api/patient/auth/register", json={k: v for k, v in payload.items() if v is not None})
    assert r.status_code == 201, r.text
    headers = cookie_of(r)
    client.cookies.clear()
    return r.json(), headers


def member_body(**over):
    return {"name": "Ram Prasad", "relation": "FATHER", "age": 68, "gender": "MALE", **over}


def book(client, slot, headers=None, **over):
    body = {
        "doctorClinicId": str(slot.id), "date": today_ist().isoformat(), "visitType": "NEW",
        "patient": {"name": "Sunita Devi", "phone": PHONE, "age": 45, "gender": "FEMALE"}, "recordsConsent": True, **over,
    }
    r = client.post("/api/public/bookings", json=body, headers=headers or {})
    client.cookies.clear()
    return r


# --- accounts --------------------------------------------------------------------------


def test_register_creates_own_profile_and_signs_in(client):
    data, headers = register(client)
    assert data["account"]["fullName"] == "Sunita Devi" and data["account"]["phone"] == PHONE
    [me] = data["members"]
    assert me["relation"] == "SELF" and me["name"] == "Sunita Devi" and me["age"] is None
    assert client.get("/api/patient/me", headers=headers).status_code == 200
    # One account per number.
    r = client.post("/api/patient/auth/register", json={"fullName": "Someone", "method": "phone", "phone": PHONE, "password": PASSWORD})
    assert r.status_code == 409


def test_login_logout_and_wrong_password(client):
    register(client)
    bad = client.post("/api/patient/auth/login", json={"method": "phone", "phone": PHONE, "password": "wrong-pass1"})
    assert bad.status_code == 401
    ok = client.post("/api/patient/auth/login", json={"method": "phone", "phone": PHONE, "password": PASSWORD})
    assert ok.status_code == 200
    headers = cookie_of(ok)
    client.cookies.clear()
    assert client.post("/api/patient/auth/logout", headers=headers, json={}).status_code == 204
    assert client.get("/api/patient/me", headers=headers).status_code == 401


def test_patient_and_doctor_sessions_are_separate(client, make_slot):
    _, patient = register(client)
    assert client.get("/api/doctor/me", headers=patient).status_code == 401
    slot = make_slot()
    doctor = doctor_sign_in(client, slot.doctor.phone, "secret123")
    assert client.get("/api/patient/me", headers=doctor).status_code == 401


def test_forgot_password_by_email(client):
    email.sent.clear()
    _, old = register(client, method="email", email="sunita@example.com", phone=None)
    unknown = client.post("/api/patient/auth/forgot", json={"email": "nobody@example.com"})
    known = client.post("/api/patient/auth/forgot", json={"email": "sunita@example.com"})
    assert unknown.json() == known.json()  # doesn't reveal which emails have accounts
    [(to, _, body)] = email.sent
    assert to == "sunita@example.com"
    token = re.search(r"token=(\S+)", body).group(1)

    r = client.post("/api/patient/auth/reset", json={"token": token, "password": "newpass123"})
    assert r.status_code == 200
    client.cookies.clear()
    assert client.get("/api/patient/me", headers=old).status_code == 401  # other sessions signed out
    assert client.post("/api/patient/auth/reset", json={"token": token, "password": "another123"}).status_code == 400
    login = client.post("/api/patient/auth/login", json={"method": "email", "email": "sunita@example.com", "password": "newpass123"})
    assert login.status_code == 200


# --- family profiles -------------------------------------------------------------------


def test_family_profiles_with_health_details(client):
    data, headers = register(client)
    r = client.post("/api/patient/members", headers=headers, json=member_body(
        bloodGroup="B+", allergies="Penicillin", conditions="Diabetes, BP", medicines="Metformin 500",
        emergencyName="Sunita Devi", emergencyRelation="Daughter", emergencyPhone=PHONE, addressArea="Kankarbagh", addressCity="Patna",
    ))
    assert r.status_code == 201, r.text
    father = r.json()
    assert father["bloodGroup"] == "B+" and father["conditions"] == "Diabetes, BP" and father["phone"] == PHONE

    me_id = data["members"][0]["id"]
    r = client.put(f"/api/patient/members/{me_id}", headers=headers, json=member_body(name="Sunita Devi", relation="SELF", gender="FEMALE", dateOfBirth="1981-03-10", age=None))
    assert r.status_code == 200 and r.json()["age"] >= 44 and r.json()["relation"] == "SELF"
    assert client.post("/api/patient/members", headers=headers, json=member_body(relation="SELF")).status_code == 400
    assert client.put(f"/api/patient/members/{father['id']}", headers=headers, json=member_body(bloodGroup="Z+")).status_code == 400

    names = [m["name"] for m in client.get("/api/patient/me", headers=headers).json()["members"]]
    assert names == ["Sunita Devi", "Ram Prasad"]  # self first


def test_other_accounts_cannot_see_or_change_profiles(client):
    _, headers = register(client)
    father = client.post("/api/patient/members", headers=headers, json=member_body()).json()
    _, other = register(client, phone="9876500000")
    assert client.put(f"/api/patient/members/{father['id']}", headers=other, json=member_body()).status_code == 404
    assert client.get(f"/api/patient/members/{father['id']}/records", headers=other).status_code == 404
    assert client.delete(f"/api/patient/members/{father['id']}", headers=other).status_code == 404


# --- records ---------------------------------------------------------------------------


def test_upload_view_and_delete_records(client):
    data, headers = register(client)
    me_id = data["members"][0]["id"]
    r = client.post(f"/api/patient/members/{me_id}/records", headers=headers,
                    json={"kind": "REPORT", "title": "Blood test", "date": today_ist().isoformat(), "file": PDF})
    assert r.status_code == 201, r.text
    record = r.json()
    assert record["contentType"] == "application/pdf" and record["addedBy"] == "PATIENT"

    f = client.get(record["fileUrl"], headers=headers)
    assert f.status_code == 200 and f.content.startswith(b"%PDF") and f.headers["cache-control"] == "private, no-store"

    _, other = register(client, phone="9876500000")
    assert client.get(record["fileUrl"], headers=other).status_code == 404
    assert client.delete(f"/api/patient/records/{record['id']}", headers=other).status_code == 404

    bad = client.post(f"/api/patient/members/{me_id}/records", headers=headers,
                      json={"kind": "REPORT", "title": "Page", "date": today_ist().isoformat(), "file": HTML})
    assert bad.status_code == 400  # only photos and PDFs, checked by their bytes

    assert client.delete(f"/api/patient/records/{record['id']}", headers=headers).status_code == 204
    assert client.get(f"/api/patient/members/{me_id}/records", headers=headers).json()["records"] == []


# --- bookings --------------------------------------------------------------------------


def test_booking_for_a_family_member_and_tracking_on_a_new_device(client, make_slot, db):
    slot = make_slot()
    _, headers = register(client)
    father = client.post("/api/patient/members", headers=headers, json=member_body(allergies="Penicillin")).json()

    r = book(client, slot, headers, memberId=father["id"],
             patient={"name": "Ram Prasad", "phone": PHONE, "age": 68, "gender": "MALE"})
    assert r.status_code == 201, r.text
    booking_id = r.json()["bookingId"]
    assert db.scalar(text("SELECT patient_id::text FROM bookings WHERE id = :id"), {"id": booking_id}) == father["id"]

    [listed] = client.get("/api/patient/bookings", headers=headers).json()["bookings"]
    assert listed["patientName"] == "Ram Prasad" and listed["token"] == 1

    # Another phone: a new tracking code, while the first one keeps working.
    code = client.post(f"/api/patient/bookings/{booking_id}/link", headers=headers, json={}).json()["code"]
    assert client.get(f"/api/public/bookings/{booking_id}", params={"code": code}).status_code == 200
    assert client.get(f"/api/public/bookings/{booking_id}", params={"code": r.json()["trackingCode"]}).status_code == 200
    _, other = register(client, phone="9876500000")
    assert client.post(f"/api/patient/bookings/{booking_id}/link", headers=other, json={}).status_code == 404

    # Booking for someone else's profile isn't possible.
    assert book(client, slot, other, memberId=father["id"]).status_code == 404
    assert book(client, slot, None, memberId=father["id"]).status_code == 401


def test_guest_booking_never_attaches_to_an_account_profile(client, make_slot, db):
    slot = make_slot()
    data, headers = register(client)
    me_id = data["members"][0]["id"]
    client.put(f"/api/patient/members/{me_id}", headers=headers, json=member_body(name="Sunita Devi", relation="SELF", age=45, gender="FEMALE"))
    r = book(client, slot)  # same name and number, not signed in
    assert r.status_code == 201
    patient_id = db.scalar(text("SELECT patient_id::text FROM bookings WHERE id = :id"), {"id": r.json()["bookingId"]})
    assert patient_id != me_id


def test_doctor_sees_health_details_only_with_consent(client, make_slot):
    slot = make_slot()
    data, headers = register(client)
    me_id = data["members"][0]["id"]
    client.put(f"/api/patient/members/{me_id}", headers=headers,
               json=member_body(name="Sunita Devi", relation="SELF", age=45, gender="FEMALE", allergies="Dust"))
    father = client.post("/api/patient/members", headers=headers, json=member_body(allergies="Penicillin", bloodGroup="O+")).json()
    doctor = doctor_sign_in(client, slot.doctor.phone, "secret123")

    shared = book(client, slot, headers, memberId=father["id"], patient={"name": "Ram Prasad", "phone": PHONE, "age": 68, "gender": "MALE"})
    detail = client.get(f"/api/doctor/bookings/{shared.json()['bookingId']}", headers=doctor).json()
    assert detail["health"]["allergies"] == "Penicillin" and detail["health"]["bloodGroup"] == "O+"

    hidden = book(client, slot, headers, memberId=me_id, recordsConsent=False)
    detail = client.get(f"/api/doctor/bookings/{hidden.json()['bookingId']}", headers=doctor).json()
    assert detail["health"] is None and detail["records"] == []
