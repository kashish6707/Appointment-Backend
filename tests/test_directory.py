import base64
import io
from datetime import datetime, time, timedelta

import pytest
from PIL import Image
from sqlalchemy import text

from app.db import SessionLocal
from app.models import WEEKDAYS, Clinic, Doctor, DoctorClinic
from app.timeutil import now_ist, today_ist
from tests.conftest import profile_body
from tests.test_doctor import sign_in
from tests.test_public import book
from tests.test_queue import J

ORIGIN = (25.6000, 85.1000)  # where the "patient" is in these tests


def set_clinic(db, slot, **values):
    sets = ", ".join(f"{k} = :{k}" for k in values)
    with db.begin():
        db.execute(text(f"UPDATE clinics SET {sets} WHERE id = :id"), {**values, "id": slot.clinic_id})


def set_doctor(db, slot, **values):
    sets = ", ".join(f"{k} = :{k}" for k in values)
    with db.begin():
        db.execute(text(f"UPDATE doctors SET {sets} WHERE id = :id"), {**values, "id": slot.doctor_id})


def add_doctor(clinic_id, name, department_key, *, n, verified=True):
    """Another doctor at an existing clinic."""
    with SessionLocal() as s:
        d = Doctor(full_name=name, phone=f"91110000{n:02d}", password_hash="x", is_verified=verified,
                   department=department_key.title(), department_key=department_key)
        s.add(d)
        s.flush()
        s.add(DoctorClinic(doctor_id=d.id, clinic_id=clinic_id, fee=300, opd_days=list(WEEKDAYS),
                           opd_start=time(0, 0), opd_end=time(23, 59)))
        s.commit()


def home(client, **params):
    params.setdefault("lat", ORIGIN[0])
    params.setdefault("lng", ORIGIN[1])
    return client.get("/api/public/home", params=params)


def png(width=1600, height=1000) -> str:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (31, 122, 85)).save(out, "PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


# --- home feed ---------------------------------------------------------------------------------


def test_home_sorts_clinics_by_distance(client, make_slot, db):
    far, near, middle = make_slot(), make_slot(), make_slot()
    set_clinic(db, far, lat=25.70, lng=85.30)
    set_clinic(db, near, lat=25.601, lng=85.101, type="HOSPITAL")
    set_clinic(db, middle, lat=25.62, lng=85.14)
    unknown = make_slot()  # no location at all: listed last

    data = home(client).json()
    assert [c["id"] for c in data["clinics"]] == [str(s.clinic_id) for s in (near, middle, far, unknown)]
    assert data["clinics"][0]["distanceKm"] == 0.1 and data["clinics"][-1]["distanceKm"] is None
    assert {d["key"] for d in data["departments"]} >= {"orthopaedics", "paediatrics"}
    assert data["nextCursor"] is None

    hospitals = home(client, type="hospital").json()["clinics"]
    assert [c["id"] for c in hospitals] == [str(near.clinic_id)] and hospitals[0]["type"] == "HOSPITAL"


def test_home_uses_area_when_no_location(client, make_slot, db):
    a, b = make_slot(), make_slot()
    set_clinic(db, a, lat=25.5990, lng=85.1600)  # in Kankarbagh
    set_clinic(db, b, lat=25.6266, lng=85.1006)  # in Patliputra
    area = db.scalar(text("SELECT id FROM areas WHERE key = 'patliputra'"))
    db.commit()
    data = client.get("/api/public/home", params={"areaId": str(area)}).json()
    assert data["clinics"][0]["id"] == str(b.clinic_id)
    assert client.get("/api/public/home", params={"lat": 25.6}).status_code == 400


def test_home_open_now_filter_and_status(client, make_slot, db):
    open_slot = make_slot()  # all day, every day
    today = WEEKDAYS[today_ist().weekday()]
    closed_slot = make_slot(days=[d for d in WEEKDAYS if d != today])
    data = {c["id"]: c for c in home(client).json()["clinics"]}
    assert data[str(open_slot.clinic_id)]["opdStatus"] == "OPEN_NOW"
    assert data[str(closed_slot.clinic_id)]["opdStatus"] == "CLOSED_TODAY"
    only_open = home(client, openNow="true").json()["clinics"]
    assert [c["id"] for c in only_open] == [str(open_slot.clinic_id)]


def test_home_opens_later(client, make_slot):
    now = now_ist()
    if now.hour >= 22:
        pytest.skip("needs an OPD later today")
    start = (datetime.combine(now.date(), time(now.hour, 0)) + timedelta(hours=1)).time()
    slot = make_slot(start=start, end=time(23, 59))
    card = home(client).json()["clinics"][0]
    assert card["id"] == str(slot.clinic_id)
    assert card["opdStatus"] == "OPENS_LATER" and card["opensAt"] == start.strftime("%H:%M")


def test_home_card_summary_waiting_and_doctors(client, make_slot, db):
    slot = make_slot(full_name="Dr Rajeev Anand")
    set_doctor(db, slot, department_key="orthopaedics", designation="Director & HOD, Orthopaedics")
    add_doctor(slot.clinic_id, "Dr Meera Sinha", "paediatrics", n=1)
    add_doctor(slot.clinic_id, "Dr Not Verified", "cardiology", n=2, verified=False)
    book(client, slot, phone="9431000001")
    card = home(client).json()["clinics"][0]
    assert card["doctorCount"] == 2 and card["departmentCount"] == 2
    assert card["summary"] == "2 doctors, Orthopaedics, Paediatrics"
    assert card["waitingNow"] == 1
    names = [d["name"] for d in card["doctors"]]
    assert names == ["Dr Meera Sinha", "Dr Rajeev Anand"]  # verified only
    rajeev = card["doctors"][1]
    assert rajeev["designation"] == "Director & HOD, Orthopaedics" and rajeev["department"] == "Orthopaedics"
    assert rajeev["fee"] == 400 and rajeev["initials"] == "RA"


def test_photo_and_registration_need_consent(client, make_slot, db):
    slot = make_slot(registration_no="BMC-777")
    set_doctor(db, slot, photo_key="doctors/x.jpg", photo_content_type="image/jpeg")
    doctor = home(client).json()["clinics"][0]["doctors"][0]
    assert doctor["photoUrl"] is None and doctor["registrationNo"] is None and doctor["initials"]

    set_doctor(db, slot, photo_consent_at=now_ist())
    from app import directory
    directory.clear_cache()
    doctor = home(client).json()["clinics"][0]["doctors"][0]
    assert doctor["photoUrl"].startswith(f"/api/public/doctors/{slot.doctor_id}/photo") and doctor["registrationNo"] == "BMC-777"


def test_home_pagination(client, make_slot, db):
    slots = [make_slot() for _ in range(12)]
    for i, s in enumerate(slots):
        set_clinic(db, s, lat=25.60 + i * 0.01, lng=85.10)
    first = home(client).json()
    assert len(first["clinics"]) == 10 and first["nextCursor"]
    second = home(client, cursor=first["nextCursor"]).json()
    assert len(second["clinics"]) == 2 and second["nextCursor"] is None
    ids = [c["id"] for c in first["clinics"] + second["clinics"]]
    assert ids == [str(s.clinic_id) for s in slots]
    r = home(client, cursor="not-a-cursor")
    assert r.status_code == 400 and r.json() == {"message": "That page link isn't valid. Start from the first page."}


# --- clinic page ----------------------------------------------------------------------------


def test_clinic_page_and_doctors_by_department(client, make_slot, db):
    slot = make_slot()
    set_doctor(db, slot, department_key="orthopaedics")
    set_clinic(db, slot, phone="9801234500", opd_hours_text_en="Mon–Sat 10 am–2 pm", opd_hours_text_hi="सोम–शनि सुबह 10–दोपहर 2")
    for i in range(11):
        add_doctor(slot.clinic_id, f"Dr Child {i:02d}", "paediatrics", n=10 + i)

    page = client.get(f"/api/public/clinics/{slot.clinic_id}").json()
    assert page["opdHoursTextHi"].startswith("सोम") and page["phone"] == "9801234500"
    assert [d["key"] for d in page["departments"]] == ["orthopaedics", "paediatrics"]
    assert page["opdStatus"] == "OPEN_NOW" and page["doctorCount"] == 12

    base = f"/api/public/clinics/{slot.clinic_id}/doctors"
    ortho = client.get(base, params={"department": "orthopaedics"}).json()
    assert [d["doctorClinicId"] for d in ortho["doctors"]] == [str(slot.id)] and ortho["nextCursor"] is None
    assert ortho["doctors"][0]["waitingToday"] == 0 and ortho["doctors"][0]["opdStatus"] == "OPEN_NOW"

    kids = client.get(base, params={"department": "paediatrics"}).json()
    assert len(kids["doctors"]) == 10 and kids["nextCursor"]
    more = client.get(base, params={"department": "paediatrics", "cursor": kids["nextCursor"]}).json()
    assert len(more["doctors"]) == 1 and more["nextCursor"] is None
    assert client.get(base, params={"department": "astrology"}).status_code == 400


def test_clinic_without_verified_doctors_is_hidden(client, make_slot):
    slot = make_slot(verified=False)
    assert client.get(f"/api/public/clinics/{slot.clinic_id}").status_code == 404
    assert home(client).json()["clinics"] == []


# --- search ---------------------------------------------------------------------------------


def test_search_mixes_clinics_hospitals_and_doctors(client, make_slot, db):
    a = make_slot(full_name="Dr Anand Kumar")
    set_clinic(db, a, name="Anand Ortho Care")
    b = make_slot(full_name="Dr Ritu Verma")
    set_clinic(db, b, name="Paras HMRI Hospital", type="HOSPITAL")
    set_doctor(db, b, department_key="cardiology", department="Cardiology")

    results = client.get("/api/public/search", params={"q": "anand"}).json()["results"]
    assert {(r["type"], r["name"]) for r in results} == {("clinic", "Anand Ortho Care"), ("doctor", "Dr Anand Kumar")}
    hospital = client.get("/api/public/search", params={"q": "paras"}).json()["results"]
    assert [(r["type"], r["name"]) for r in hospital] == [("hospital", "Paras HMRI Hospital")]
    by_department = client.get("/api/public/search", params={"q": "heart"}).json()["results"]
    assert [(r["type"], r["name"]) for r in by_department] == [("doctor", "Dr Ritu Verma")]
    assert client.get("/api/public/search", params={"q": "a"}).json()["results"] == []


# --- clinic details and photos (doctor) --------------------------------------------------------


def test_doctor_edits_clinic_and_photos(client, make_slot, db, media_dir):
    slot = make_slot()
    auth = {**J, **sign_in(client, slot.doctor.phone, "secret123")}
    base = f"/api/doctor/clinic-details/{slot.clinic_id}"

    r = client.patch(base, headers=auth, json={"type": "HOSPITAL", "nameHi": "आनंद अस्पताल", "areaKey": "kankarbagh",
                                                 "lat": 25.5991, "lng": 85.1601, "opdHoursTextEn": "Mon–Sat 10–2"})
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "HOSPITAL" and r.json()["areaName"] == "Kankarbagh" and r.json()["lat"] == 25.5991

    first = client.post(f"{base}/photos", headers=auth, json={"image": png()})
    assert first.status_code == 201, first.text
    second = client.post(f"{base}/photos", headers=auth, json={"image": png(800, 800), "cover": True}).json()
    photos = second["photos"]
    assert len(photos) == 2 and second["coverPhotoId"] == photos[1]["id"]
    assert photos[0]["width"] == 1200 and photos[0]["height"] == 750  # resized, same shape

    # Both sizes are stored and served; the small one is 400 px wide.
    large = client.get(photos[0]["large"])
    small = client.get(photos[0]["small"])
    assert large.status_code == 200 and large.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(small.content)).size == (400, 250)
    assert "immutable" in large.headers["cache-control"]

    public = client.get(f"/api/public/clinics/{slot.clinic_id}").json()
    assert public["coverPhoto"]["small"] == photos[1]["small"] and len(public["photos"]) == 2
    assert home(client).json()["clinics"][0]["coverPhotoUrl"] == photos[1]["small"]

    r = client.put(f"{base}/photos/order", headers=auth, json={"photoIds": [photos[1]["id"], photos[0]["id"]]})
    assert [p["id"] for p in r.json()["photos"]] == [photos[1]["id"], photos[0]["id"]]
    r = client.delete(f"{base}/photos/{photos[1]['id']}", headers=auth)
    assert len(r.json()["photos"]) == 1 and r.json()["coverPhotoId"] == photos[0]["id"]  # cover moves on
    assert client.get(photos[1]["large"]).status_code == 404  # files deleted

    actions = [e["action"] for e in client.get("/api/doctor/activity", headers=auth).json()["entries"]]
    assert {"clinic.update", "clinic.photo_add", "clinic.photo_remove"} <= set(actions)


@pytest.mark.parametrize("image, message", [
    ("data:image/png;base64," + base64.b64encode(b"<svg/>" * 100).decode(), "Upload a JPEG, PNG or WebP image of up to 5 MB."),
    ("not a data url", "Upload a JPEG, PNG or WebP image of up to 5 MB."),
    (None, None),
])
def test_bad_photos_are_refused(client, make_slot, image, message):
    slot = make_slot()
    auth = {**J, **sign_in(client, slot.doctor.phone, "secret123")}
    body = {"image": image if image is not None else png(100, 100)}
    r = client.post(f"/api/doctor/clinic-details/{slot.clinic_id}/photos", headers=auth, json=body)
    assert r.status_code == 400
    assert r.json()["message"] == (message or "That image is too small. Use one at least 200 pixels on each side.")


def test_only_doctors_at_the_clinic_can_edit_it(client, make_slot):
    mine, other = make_slot(), make_slot()
    auth = {**J, **sign_in(client, mine.doctor.phone, "secret123")}
    assert client.patch(f"/api/doctor/clinic-details/{other.clinic_id}", headers=auth, json={"type": "HOSPITAL"}).status_code == 404
    assert client.get("/api/media/public/../../etc/passwd").status_code == 404


def test_profile_sets_designation_hindi_name_and_consent(client, db):
    client.post("/api/auth/doctor/register", json={
        "fullName": "Dr Meera Sinha", "method": "phone", "phone": "9123400001", "password": "secret123",
        "profile": profile_body(designation="Senior Consultant", nameHi="डॉ. मीरा सिन्हा", photoConsent=True, specialisation="Child specialist")})
    row = db.execute(text("SELECT designation, name_hi, photo_consent_at IS NOT NULL, department_key FROM doctors")).one()
    assert row == ("Senior Consultant", "डॉ. मीरा सिन्हा", True, "paediatrics")
    db.commit()
    auth = {**J, **sign_in(client, "9123400001", "secret123")}
    body = profile_body(photoConsent=False)
    body.pop("photo")
    assert client.put("/api/doctor/profile", headers=auth, json=body).json()["photoConsent"] is False


def test_running_opd_counts_as_open_even_after_hours(client, make_slot, db):
    now = now_ist()
    if now.hour < 1:
        pytest.skip("needs hours that ended earlier today")
    slot = make_slot(start=time(0, 0), end=time(now.hour - 1 if now.hour > 1 else 0, 30))
    assert home(client).json()["clinics"][0]["opdStatus"] == "CLOSED_TODAY"
    with db.begin():
        db.execute(text("INSERT INTO opd_sessions (id, doctor_clinic_id, date, status, started_at) "
                        "VALUES (gen_random_uuid(), :dc, :d, 'IN_OPD', now())"), {"dc": slot.id, "d": today_ist()})
    from app import directory
    directory.clear_cache()
    assert home(client).json()["clinics"][0]["opdStatus"] == "OPEN_NOW"


def test_home_filters_by_department_and_words(client, make_slot, db):
    bones = make_slot(full_name="Dr Rajeev Anand")
    set_doctor(db, bones, department_key="orthopaedics")
    set_clinic(db, bones, name="Anand Ortho Care")
    kids = make_slot(full_name="Dr Meera Sinha")
    set_doctor(db, kids, department_key="paediatrics")
    set_clinic(db, kids, name="Little Steps Clinic")

    only_bones = home(client, department="orthopaedics").json()["clinics"]
    assert [c["id"] for c in only_bones] == [str(bones.clinic_id)] and only_bones[0]["departmentKeys"] == ["orthopaedics"]
    assert [c["name"] for c in home(client, q="little").json()["clinics"]] == ["Little Steps Clinic"]  # clinic name
    assert [c["name"] for c in home(client, q="meera").json()["clinics"]] == ["Little Steps Clinic"]  # doctor name
    assert [c["name"] for c in home(client, q="शिशु").json()["clinics"]] == ["Little Steps Clinic"]  # department in Hindi
    assert home(client, q="nothing like this").json()["clinics"] == []
    assert home(client, department="astrology").status_code == 400
