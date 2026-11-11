import base64

import pytest
from sqlalchemy import text

from tests.conftest import JPEG, profile_body
from tests.test_doctor import sign_in
from tests.test_queue import J

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 2000


def data_url(raw: bytes, claimed="image/jpeg") -> str:
    return f"data:{claimed};base64," + base64.b64encode(raw).decode()


def register(client, phone="9123400001", **profile):
    return client.post("/api/auth/doctor/register", json={
        "fullName": "Dr Meera Sinha", "method": "phone", "phone": phone, "password": "secret123",
        "profile": profile_body(**profile),
    })


def test_register_saves_the_full_profile(client, db, media_dir):
    r = register(client, achievements=["Gold medal, PMCH", " gold medal, pmch ", ""], expertise=["Knee replacement", "Sports injuries"],
                 about="Treats bone and joint problems.", medicalCouncil="Bihar Council of Medicine", registrationNo=" bmc-12345 ")
    assert r.status_code == 201, r.text
    row = db.execute(text("SELECT qualification, experience_years, consultation_fee, registration_no, medical_council, "
                          "achievements, expertise, photo_key, photo_content_type, department FROM doctors")).one()
    assert row.qualification == "MBBS, MS (Orthopaedics)"
    assert row.experience_years == 12 and row.consultation_fee == 500
    assert row.registration_no == "BMC-12345" and row.medical_council == "Bihar Council of Medicine"
    assert row.achievements == ["Gold medal, PMCH"]  # duplicates and blanks dropped
    assert row.expertise == ["Knee replacement", "Sports injuries"]
    assert row.photo_content_type == "image/jpeg" and row.department == "Orthopaedics"
    assert (media_dir / row.photo_key).read_bytes() == JPEG
    assert row.photo_key.startswith("doctors/") and row.photo_key.endswith(".jpg")
    degrees = db.execute(text("SELECT level, name, institute, year FROM doctor_degrees ORDER BY position")).all()
    assert degrees == [("UG", "MBBS", "PMCH Patna", 2008), ("PG", "MS (Orthopaedics)", None, None)]

    me = client.get("/api/doctor/me", headers=sign_in(client, "9123400001", "secret123")).json()
    assert me["doctor"]["profileComplete"] is True
    assert me["doctor"]["photoUrl"].startswith("/api/doctor/profile/photo?v=")


@pytest.mark.parametrize("change, message", [
    ({"photo": None}, "Add a clear photo of yourself (JPEG, PNG or WebP, up to 2 MB)."),
    ({"photo": data_url(b"<svg onload=alert(1)>" + b" " * 2000, "image/svg+xml")}, "Add a clear photo of yourself (JPEG, PNG or WebP, up to 2 MB)."),
    ({"photo": data_url(b"not really a jpeg" * 100)}, "Add a clear photo of yourself (JPEG, PNG or WebP, up to 2 MB)."),
    ({"photo": data_url(b"\xff\xd8\xff" + b"0" * (2 * 1024 * 1024 + 10))}, "Add a clear photo of yourself (JPEG, PNG or WebP, up to 2 MB)."),
    ({"photo": "data:image/jpeg;base64,!!!notbase64"}, "Add a clear photo of yourself (JPEG, PNG or WebP, up to 2 MB)."),
    ({"degrees": []}, "Add at least one degree (up to 15)."),
    ({"degrees": [{"level": "PHD", "name": "PhD"}]}, "Choose each degree's level: UG, PG, super speciality, fellowship, diploma or other."),
    ({"degrees": [{"level": "UG"}]}, "Enter each degree's name, for example MBBS or MS (Orthopaedics)."),
    ({"degrees": [{"level": "UG", "name": "MBBS", "year": 1900}]}, None),
    ({"experienceYears": None}, "Enter your years of experience, from 0 to 70."),
    ({"experienceYears": 99}, "Enter your years of experience, from 0 to 70."),
    ({"fee": -1}, "Enter your consultation fee in rupees, from 0 to 1,00,000."),
    ({"fee": "500"}, "Enter your consultation fee in rupees, from 0 to 1,00,000."),
    ({"registrationNo": "x"}, "Enter your medical registration number (3 to 30 letters, numbers, / . or -)."),
    ({"registrationNo": None}, "Enter your medical registration number (3 to 30 letters, numbers, / . or -)."),
    ({"about": "a" * 1501}, "Keep “About” under 1,500 characters."),
    ({"expertise": ["x"]}, "Add up to 20 areas of expertise, each 2 to 60 characters."),
])
def test_register_requires_a_valid_profile(client, db, change, message):
    body = profile_body(**change)
    body = {k: v for k, v in body.items() if v is not None}
    r = client.post("/api/auth/doctor/register", json={
        "fullName": "Dr Meera Sinha", "method": "phone", "phone": "9123400001", "password": "secret123", "profile": body})
    assert r.status_code == 400, r.text
    if message:
        assert r.json() == {"message": message}
    assert db.scalar(text("SELECT count(*) FROM doctors")) == 0


def test_register_without_profile(client):
    r = client.post("/api/auth/doctor/register", json={"fullName": "Dr X Kumar", "method": "phone", "phone": "9123400001", "password": "secret123"})
    assert r.status_code == 400
    assert r.json()["message"].startswith("Add your professional details")


def test_png_is_accepted_and_typed_from_its_bytes(client, db):
    assert register(client, photo=data_url(PNG, claimed="image/jpeg")).status_code == 201
    assert db.scalar(text("SELECT photo_content_type FROM doctors")) == "image/png"


def test_duplicate_registration_leaves_no_photo_behind(client, media_dir):
    register(client)
    assert register(client).status_code == 409
    assert len(list((media_dir / "doctors").iterdir())) == 1


# --- editing the profile -------------------------------------------------------------------


def test_edit_profile_keeps_or_replaces_the_photo(client, db, media_dir):
    register(client)
    auth = {**J, **sign_in(client, "9123400001", "secret123")}
    before = client.get("/api/doctor/profile", headers=auth).json()
    assert before["complete"] is True and before["registrationNo"] == "BMC-12345"

    body = profile_body(fee=700, degrees=[{"level": "UG", "name": "MBBS"}])
    body.pop("photo")
    r = client.put("/api/doctor/profile", headers=auth, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["fee"] == 700 and r.json()["qualification"] == "MBBS"
    assert r.json()["photoUrl"] == before["photoUrl"]  # photo kept

    r = client.put("/api/doctor/profile", headers=auth, json={**profile_body(photo=data_url(PNG)), "fullName": "Dr Meera K Sinha"})
    assert r.json()["photoUrl"] != before["photoUrl"] and r.json()["fullName"] == "Dr Meera K Sinha"
    files = list((media_dir / "doctors").iterdir())
    assert len(files) == 1 and files[0].suffix == ".png"  # the old photo was deleted

    photo = client.get("/api/doctor/profile/photo", headers=auth)
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/png"
    assert photo.headers["x-content-type-options"] == "nosniff"
    entries = client.get("/api/doctor/activity", headers=auth).json()["entries"]
    assert entries[0]["action"] == "profile.update"


def test_older_accounts_are_marked_incomplete(client, make_slot):
    slot = make_slot()
    auth = sign_in(client, slot.doctor.phone, "secret123")
    assert client.get("/api/doctor/me", headers=auth).json()["doctor"]["profileComplete"] is False
    assert client.get("/api/doctor/profile/photo", headers=auth).status_code == 404


def test_staff_cannot_edit_the_doctors_profile(client, make_slot):
    slot = make_slot()
    auth = {**J, **sign_in(client, slot.doctor.phone, "secret123")}
    r = client.post("/api/doctor/staff", headers=auth, json={
        "fullName": "Desk Person", "phone": "9876500031", "role": "RECEPTIONIST", "permissions": [], "doctorClinicIds": [str(slot.id)]})
    token = r.json()["inviteUrl"].rsplit("/", 1)[1]
    cookie = client.post(f"/api/auth/staff/invite/{token}", json={"password": "staff123"}).cookies["clinically_session"]
    client.cookies.clear()
    staff = {**J, "Cookie": f"clinically_session={cookie}"}
    assert client.put("/api/doctor/profile", headers=staff, json=profile_body()).status_code == 403


# --- what patients see -----------------------------------------------------------------------


def test_public_profile_only_for_verified_doctors(client, db):
    doctor_id = register(client).json()["id"]
    assert client.get(f"/api/public/doctors/{doctor_id}/photo").status_code == 404
    with db.begin():
        db.execute(text("UPDATE doctors SET is_verified = true"))

    data = client.get(f"/api/public/doctors/{doctor_id}").json()
    assert data["photoUrl"].startswith(f"/api/public/doctors/{doctor_id}/photo?v=")
    assert data["experienceYears"] == 12 and data["fee"] == 500
    assert [d["name"] for d in data["degrees"]] == ["MBBS", "MS (Orthopaedics)"]
    assert "registrationNo" not in data and "phone" not in data
    photo = client.get(data["photoUrl"])
    assert photo.status_code == 200 and photo.content == JPEG
    assert photo.headers["cache-control"] == "public, max-age=604800"
