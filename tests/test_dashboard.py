import asyncio
from datetime import date, timedelta

import pytest
import socketio
from sqlalchemy import text

from app import realtime
from app.config import get_settings
from app.db import SessionLocal
from app.models import WEEKDAYS, MedicalRecord
from app.timeutil import today_ist
from tests.test_doctor import sign_in
from tests.test_public import book
from tests.test_queue import J, act, opd  # noqa: F401  (opd is a fixture)


def get(client, opd, path):
    return client.get(f"/api/doctor{path}", headers=opd["auth"])


def booking_ids(db):
    return [r[0] for r in db.execute(text("SELECT id::text FROM bookings ORDER BY token")).all()]


# --- dashboard -------------------------------------------------------------------------


def test_dashboard_lists_the_day(client, opd, db):
    first = booking_ids(db)[0]
    act(client, opd, "start")
    act(client, opd, "next")  # token 1 with the doctor
    client.post(f"/api/doctor/sessions/{opd['session']}/walk-ins", headers=opd["auth"],
                json={"name": "Walk In", "age": 30, "gender": "FEMALE"})
    b = opd["bookings"][2]
    client.post(f"/api/public/bookings/{b['bookingId']}/cancel", json={"code": b["trackingCode"], "reason": "Feeling better"})

    r = get(client, opd, "/dashboard")
    assert r.status_code == 200
    data = r.json()
    assert data["date"] == today_ist().isoformat()
    assert data["isOpdDay"] is True
    assert data["clinic"]["doctorClinicId"] == str(opd["slot"].id)
    assert data["session"]["status"] == "IN_OPD" and data["session"]["currentToken"] == 1
    assert [x["token"] for x in data["bookings"]] == [1, 2, 4]
    assert data["bookings"][0]["id"] == first
    assert [x["token"] for x in data["cancelled"]] == [3]
    assert data["cancelled"][0]["cancelReason"] == "Feeling better"
    assert data["cancelled"][0]["cancelledBy"] == "PATIENT"
    assert data["stats"] == {
        "booked": 3, "online": 2, "walkIn": 1, "waiting": 2, "withDoctor": 1,
        "seen": 0, "sentForTests": 0, "noShow": 0, "cancelled": 1,
    }


def test_dashboard_creates_todays_session_before_any_booking(client, make_slot, db):
    slot = make_slot()
    auth = sign_in(client, slot.doctor.phone, "secret123")
    r = client.get("/api/doctor/dashboard", headers=auth)
    assert r.status_code == 200
    assert r.json()["session"]["status"] == "NOT_STARTED"
    assert r.json()["bookings"] == []
    assert db.scalar(text("SELECT count(*) FROM opd_sessions")) == 1
    # Asking again doesn't make a second one.
    client.get("/api/doctor/dashboard", headers=auth)
    assert db.scalar(text("SELECT count(*) FROM opd_sessions")) == 1


def test_dashboard_on_a_day_without_opd(client, make_slot, db):
    today = today_ist()
    slot = make_slot(days=[d for d in WEEKDAYS if d != WEEKDAYS[today.weekday()]])
    auth = sign_in(client, slot.doctor.phone, "secret123")
    data = client.get("/api/doctor/dashboard", headers=auth).json()
    assert data["isOpdDay"] is False and data["session"] is None
    assert db.scalar(text("SELECT count(*) FROM opd_sessions")) == 0


def test_dashboard_past_day_without_session(client, opd, db):
    yesterday = (today_ist() - timedelta(days=1)).isoformat()
    data = get(client, opd, f"/dashboard?date={yesterday}").json()
    assert data["session"] is None and data["bookings"] == []


def test_dashboard_chooses_clinic_and_hides_other_doctors(client, opd, make_slot):
    other = make_slot()
    r = get(client, opd, f"/dashboard?doctorClinicId={other.id}")
    assert r.status_code == 404 and r.json() == {"message": "We couldn't find that clinic."}
    assert get(client, opd, f"/dashboard?doctorClinicId={opd['slot'].id}").status_code == 200


def test_dashboard_needs_sign_in_and_valid_params(client, opd):
    assert client.get("/api/doctor/dashboard").status_code == 401
    r = get(client, opd, "/dashboard?date=11-11-2026")
    assert r.status_code == 400 and r.json() == {"message": "Choose a date in the format YYYY-MM-DD."}


# --- calendar ----------------------------------------------------------------------------


def test_calendar_counts_bookings_per_day(client, opd):
    b = opd["bookings"][0]
    client.post(f"/api/public/bookings/{b['bookingId']}/cancel", json={"code": b["trackingCode"]})
    today = today_ist()
    r = get(client, opd, f"/calendar?month={today:%Y-%m}")
    assert r.status_code == 200
    data = r.json()
    assert data["month"] == f"{today:%Y-%m}"
    assert len(data["days"]) == (date(today.year + today.month // 12, today.month % 12 + 1, 1) - today.replace(day=1)).days
    day = next(d for d in data["days"] if d["date"] == today.isoformat())
    assert day == {"date": today.isoformat(), "bookings": 2, "isOpdDay": True, "sessionStatus": "NOT_STARTED", "closed": False,
                   "limit": None, "exception": None, "note": None}


def test_calendar_marks_closed_days(client, make_slot):
    today = today_ist()
    off = WEEKDAYS[(today.weekday() + 1) % 7]
    slot = make_slot(days=[d for d in WEEKDAYS if d != off])
    auth = sign_in(client, slot.doctor.phone, "secret123")
    days = client.get("/api/doctor/calendar", headers=auth).json()["days"]
    for d in days:
        assert d["closed"] == (WEEKDAYS[date.fromisoformat(d["date"]).weekday()] == off)


def test_calendar_rejects_bad_month(client, opd):
    r = get(client, opd, "/calendar?month=2026-13")
    assert r.status_code == 400 and r.json() == {"message": "Choose a month in the format YYYY-MM."}


# --- search ------------------------------------------------------------------------------


def test_search_by_token_name_and_phone(client, opd):
    by_token = get(client, opd, "/search?q=2").json()["results"]
    assert [r["patient"]["name"] for r in by_token] == ["Patient 2"]
    assert by_token[0]["clinicName"].startswith("Test Clinic")
    assert by_token[0]["date"] == today_ist().isoformat()

    assert len(get(client, opd, "/search?q=patient").json()["results"]) == 3
    assert [r["token"] for r in get(client, opd, "/search?q=94310000").json()["results"]] == [1, 2, 3]
    assert get(client, opd, "/search?q=%25").json()["results"] == []  # % is not a wildcard
    assert get(client, opd, "/search?q=").json()["results"] == []


def test_search_never_shows_other_doctors_patients(client, opd, make_slot):
    other = make_slot()
    book(client, other, phone="9431099999", name="Someone Else")
    assert get(client, opd, "/search?q=someone").json()["results"] == []


# --- booking details ---------------------------------------------------------------------


def test_booking_detail_with_consent(client, opd, db, tmp_path, monkeypatch):
    bid = booking_ids(db)[1]
    patient_id = db.scalar(text("SELECT patient_id FROM bookings WHERE id = :id"), {"id": bid})
    db.commit()
    with SessionLocal() as s:
        s.add(MedicalRecord(patient_id=patient_id, kind="REPORT", title="Blood test", date=today_ist(),
                            added_by="PATIENT", file_key="a/report.pdf", content_type="application/pdf"))
        s.commit()

    data = get(client, opd, f"/bookings/{bid}").json()
    assert data["booking"]["token"] == 2
    assert data["queuePosition"] == 2
    assert data["patient"]["name"] == "Patient 2" and data["patient"]["age"] == 54
    assert data["recordsConsent"] is True
    [record] = data["records"]
    assert record["title"] == "Blood test" and record["fileUrl"] == f"/api/doctor/records/{record['id']}/file"
    assert data["tests"] == []


def test_booking_detail_without_consent_hides_records(client, make_slot, db):
    slot = make_slot()
    book(client, slot, consent=False)
    bid = booking_ids(db)[0]
    patient_id = db.scalar(text("SELECT patient_id FROM bookings"))
    db.commit()
    with SessionLocal() as s:
        s.add(MedicalRecord(patient_id=patient_id, kind="PRESCRIPTION", title="Old prescription", date=today_ist(),
                            added_by="CLINIC", file_key="b/old.pdf", content_type="application/pdf"))
        s.commit()
    auth = sign_in(client, slot.doctor.phone, "secret123")
    data = client.get(f"/api/doctor/bookings/{bid}", headers=auth).json()
    assert data["recordsConsent"] is False and data["records"] == []
    assert data["patient"]["name"] == "Ramesh Kumar"


def test_other_doctors_booking_is_not_found(client, opd, make_slot, db):
    other = make_slot()
    book(client, other, phone="9431099999")
    bid = db.scalar(text("SELECT b.id::text FROM bookings b JOIN opd_sessions s ON s.id = b.session_id "
                         "WHERE s.doctor_clinic_id = :dc"), {"dc": other.id})
    assert get(client, opd, f"/bookings/{bid}").status_code == 404
    assert get(client, opd, "/bookings/not-a-uuid").status_code == 404


# --- tests advised -----------------------------------------------------------------------


def test_send_for_tests_then_mark_done(client, opd, db, emitted):
    bid = booking_ids(db)[0]
    act(client, opd, "start")
    act(client, opd, "next")
    r = client.post(f"/api/doctor/bookings/{bid}/tests", headers=opd["auth"],
                    json={"names": ["  X-ray  knee ", "CBC", "cbc"]})
    assert r.status_code == 201
    assert r.json()["booking"]["status"] == "SENT_FOR_TESTS"
    assert r.json()["booking"]["completedAt"] is not None
    assert [t["name"] for t in r.json()["tests"]] == ["CBC", "X-ray knee"]
    assert ("booking.updated" in [e for _, e, _ in emitted])

    # Adding the same names again doesn't duplicate them.
    r = client.post(f"/api/doctor/bookings/{bid}/tests", headers=opd["auth"], json={"names": ["CBC", "ECG"]})
    assert [t["name"] for t in r.json()["tests"]] == ["CBC", "ECG", "X-ray knee"]

    test_id = r.json()["tests"][0]["id"]
    r = client.patch(f"/api/doctor/tests/{test_id}", headers=opd["auth"], json={"status": "DONE"})
    assert r.status_code == 200 and r.json()["status"] == "DONE"
    detail = get(client, opd, f"/bookings/{bid}").json()
    assert {t["name"]: t["status"] for t in detail["tests"]} == {"CBC": "DONE", "ECG": "PENDING", "X-ray knee": "PENDING"}


@pytest.mark.parametrize("names", [[], ["x"], ["a" * 101], [f"Test {i}" for i in range(11)], "CBC", [1]])
def test_send_for_tests_validates_names(client, opd, db, names):
    r = client.post(f"/api/doctor/bookings/{booking_ids(db)[0]}/tests", headers=opd["auth"], json={"names": names})
    assert r.status_code == 400 and r.json() == {"message": "Add 1 to 10 test names, each 2 to 100 characters."}


def test_test_status_validation_and_ownership(client, opd, db, make_slot):
    bid = booking_ids(db)[0]
    test_id = client.post(f"/api/doctor/bookings/{bid}/tests", headers=opd["auth"], json={"names": ["CBC"]}).json()["tests"][0]["id"]
    r = client.patch(f"/api/doctor/tests/{test_id}", headers=opd["auth"], json={"status": "MAYBE"})
    assert r.status_code == 400 and r.json() == {"message": "Choose a test status: done or not done."}

    other = make_slot()
    other_auth = sign_in(client, other.doctor.phone, "secret123")
    r = client.patch(f"/api/doctor/tests/{test_id}", headers={**J, **other_auth}, json={"status": "DONE"})
    assert r.status_code == 404


def test_cancelled_booking_cannot_be_sent_for_tests(client, opd, db):
    b = opd["bookings"][0]
    client.post(f"/api/public/bookings/{b['bookingId']}/cancel", json={"code": b["trackingCode"]})
    r = client.post(f"/api/doctor/bookings/{b['bookingId']}/tests", headers=opd["auth"], json={"names": ["CBC"]})
    assert r.status_code == 409


# --- record files ------------------------------------------------------------------------


def _record(db, *, file_key, content_type="application/pdf", consent=True):
    patient_id = db.scalar(text("SELECT patient_id FROM bookings ORDER BY token LIMIT 1"))
    if not consent:
        db.execute(text("UPDATE patients SET records_consent_at = NULL WHERE id = :id"), {"id": patient_id})
    db.commit()
    with SessionLocal() as s:
        record = MedicalRecord(patient_id=patient_id, kind="REPORT", title="Knee X-ray", date=today_ist(),
                               added_by="PATIENT", file_key=file_key, content_type=content_type)
        s.add(record)
        s.commit()
        return str(record.id)


@pytest.fixture
def records_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "records_dir", str(tmp_path))
    (tmp_path / "p1").mkdir()
    (tmp_path / "p1" / "xray.pdf").write_bytes(b"%PDF-1.4 test")
    (tmp_path / "p1" / "page.html").write_text("<script>alert(1)</script>")
    return tmp_path


def test_record_file_is_served_inline(client, opd, db, records_dir):
    rid = _record(db, file_key="p1/xray.pdf")
    r = get(client, opd, f"/records/{rid}/file")
    assert r.status_code == 200 and r.content == b"%PDF-1.4 test"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_record_file_that_could_run_scripts_is_downloaded(client, opd, db, records_dir):
    rid = _record(db, file_key="p1/page.html", content_type="text/html")
    r = get(client, opd, f"/records/{rid}/file")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")


def test_record_file_needs_consent_and_a_booking_with_this_doctor(client, opd, db, records_dir, make_slot):
    rid = _record(db, file_key="p1/xray.pdf", consent=False)
    assert get(client, opd, f"/records/{rid}/file").status_code == 404

    db.execute(text("UPDATE patients SET records_consent_at = now()"))
    db.commit()
    other = make_slot()
    other_auth = sign_in(client, other.doctor.phone, "secret123")
    assert client.get(f"/api/doctor/records/{rid}/file", headers=other_auth).status_code == 404
    assert get(client, opd, f"/records/{rid}/file").status_code == 200


def test_record_file_outside_the_folder_is_refused(client, opd, db, records_dir):
    rid = _record(db, file_key="../outside.pdf")
    (records_dir.parent / "outside.pdf").write_bytes(b"secret")
    assert get(client, opd, f"/records/{rid}/file").status_code == 404


# --- live updates ------------------------------------------------------------------------


def test_socket_connection_needs_a_valid_session(client, opd):
    cookie = opd["auth"]["Cookie"]
    assert realtime._doctor_id(cookie) == str(opd["slot"].doctor_id)
    assert realtime._doctor_id("") is None
    assert realtime._doctor_id("clinically_session=wrong") is None
    with pytest.raises(socketio.exceptions.ConnectionRefusedError):
        asyncio.run(realtime.connect("sid", {"HTTP_COOKIE": "clinically_session=wrong"}))
