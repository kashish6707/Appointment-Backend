from sqlalchemy import text

from scripts import seed


def sign_in(client, phone=seed.DEMO_PHONE, password=seed.DEMO_PASSWORD) -> dict:
    r = client.post("/api/auth/doctor/login", json={"method": "phone", "phone": phone, "password": password})
    assert r.status_code == 200, r.text
    token = r.cookies["clinically_session"]
    client.cookies.clear()
    return {"Cookie": f"clinically_session={token}"}


def test_seed_creates_demo_data(db):
    seed.main()
    assert db.scalar(text("SELECT count(*) FROM bookings")) == 12
    assert db.scalar(text("SELECT count(*) FROM opd_sessions WHERE date = (now() AT TIME ZONE 'Asia/Kolkata')::date")) == 1
    seed.main()  # second run is a no-op
    assert db.scalar(text("SELECT count(*) FROM doctors")) == 1


def test_doctor_me_includes_profile_and_clinics(client):
    seed.main()
    r = client.get("/api/doctor/me", headers=sign_in(client))
    assert r.status_code == 200
    doctor = r.json()["doctor"]
    assert doctor["fullName"] == "Dr. Rajeev Anand"
    assert doctor["department"] == "Orthopaedics"
    assert doctor["verified"] is True
    assert "passwordHash" not in doctor and "password_hash" not in doctor
    [clinic] = doctor["clinics"]
    assert clinic["name"] == "Anand Ortho Care"
    assert clinic["area"] == "Kankarbagh"
    assert clinic["fee"] == 500
    assert clinic["opdDays"] == ["MON", "TUE", "WED", "THU", "FRI", "SAT"]
    assert (clinic["opdStart"], clinic["opdEnd"]) == ("10:00", "14:00")


def test_doctor_me_with_no_clinics_yet(client):
    client.post(
        "/api/auth/doctor/register",
        json={"fullName": "Dr New Doctor", "method": "phone", "phone": "9123456780", "password": "secret123"},
    )
    r = client.get("/api/doctor/me", headers=sign_in(client, "9123456780", "secret123"))
    assert r.status_code == 200
    assert r.json()["doctor"]["clinics"] == []
    assert r.json()["doctor"]["verified"] is False


def test_doctor_me_requires_sign_in(client):
    r = client.get("/api/doctor/me")
    assert r.status_code == 401
    assert r.json() == {"message": "Please sign in."}
