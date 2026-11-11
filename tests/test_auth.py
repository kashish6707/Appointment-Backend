from sqlalchemy import text

PHONE = "9876543210"
PASSWORD = "secret123"
BAD_LOGIN = {"message": "That mobile number, email or password is wrong. Check and try again."}


def register(client, **overrides):
    body = {"fullName": "Dr Anjali Singh", "method": "phone", "phone": PHONE, "password": PASSWORD}
    body.update(overrides)
    return client.post("/api/auth/doctor/register", json=body)


def login(client, phone=PHONE, password=PASSWORD, **headers):
    r = client.post(
        "/api/auth/doctor/login",
        json={"method": "phone", "phone": phone, "password": password},
        headers=headers,
    )
    client.cookies.clear()  # tests pass cookies explicitly
    return r


def session_token(response) -> str:
    cookie = response.headers["set-cookie"]
    return cookie.split(";")[0].split("=", 1)[1]


def me(client, token=None):
    headers = {"Cookie": f"clinically_session={token}"} if token is not None else {}
    return client.get("/api/auth/me", headers=headers)


# --- register -------------------------------------------------------------


def test_register_by_phone_then_duplicate(client):
    r = register(client)
    assert r.status_code == 201
    assert set(r.json()) == {"id"}
    assert "set-cookie" not in r.headers

    r = register(client)
    assert r.status_code == 409
    assert r.json() == {
        "message": "An account with this phone number or email already exists. Sign in instead."
    }


def test_register_email_is_lowercased(client, db):
    r = client.post(
        "/api/auth/doctor/register",
        json={"fullName": "Dr Ravi Kumar", "method": "email", "email": "  Ravi.Kumar@Example.COM ", "password": PASSWORD},
    )
    assert r.status_code == 201
    stored = db.scalar(text("SELECT email FROM doctors"))
    assert stored == "ravi.kumar@example.com"

    # Same address in different case is a duplicate.
    r = client.post(
        "/api/auth/doctor/register",
        json={"fullName": "Dr Ravi Kumar", "method": "email", "email": "RAVI.KUMAR@example.com", "password": PASSWORD},
    )
    assert r.status_code == 409


def test_password_is_hashed_with_argon2id(client, db):
    register(client)
    stored = db.scalar(text("SELECT password_hash FROM doctors"))
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


def test_register_validation(client):
    cases = [
        ({"fullName": "  Al "}, "full name"),
        ({"phone": "5876543210"}, "10-digit mobile number"),
        ({"phone": "98765"}, "10-digit mobile number"),
        ({"method": "email", "phone": None, "email": "not-an-email"}, None),
        ({"password": "short1"}, "at least 8 characters"),
        ({"password": "onlyletters"}, "at least 8 characters"),
        ({"password": "12345678"}, "at least 8 characters"),
        ({"method": "fax"}, "mobile number or email"),
        ({"extra": "x"}, "Unexpected field"),
    ]
    for overrides, expected in cases:
        body = {"fullName": "Dr Anjali Singh", "method": "phone", "phone": PHONE, "password": PASSWORD}
        body.update(overrides)
        body = {k: v for k, v in body.items() if v is not None}
        if body["method"] == "email":
            body.pop("phone", None)
        r = client.post("/api/auth/doctor/register", json=body)
        assert r.status_code == 400, (overrides, r.text)
        data = r.json()
        assert set(data) == {"message"}, data
        if expected:
            assert expected in data["message"], (overrides, data)

    r = client.post("/api/auth/doctor/register", json={"fullName": "Dr X Y", "method": "email", "email": "a@b", "password": PASSWORD})
    assert r.status_code == 400
    assert r.json() == {"message": "Enter a valid email address, like name@example.com."}


def test_bad_phone_message_is_plain_english(client):
    r = register(client, phone="12345")
    assert r.json() == {"message": "Enter a 10-digit mobile number starting with 6, 7, 8 or 9."}


def test_invalid_json_is_400(client):
    r = client.post(
        "/api/auth/doctor/register", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 400
    assert set(r.json()) == {"message"}


def test_non_json_post_is_415(client):
    r = client.post("/api/auth/doctor/login", content=b"method=phone", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert set(r.json()) == {"message"}

    r = client.post(
        "/api/auth/doctor/register",
        data={"fullName": "x"},  # form-encoded
    )
    assert r.status_code == 415


# --- login ----------------------------------------------------------------


def test_login_success_sets_session_cookie(client):
    doctor_id = register(client).json()["id"]
    r = login(client)
    assert r.status_code == 200
    assert r.json() == {"doctor": {"id": doctor_id, "fullName": "Dr Anjali Singh"}}

    cookie = r.headers["set-cookie"]
    parts = [p.strip() for p in cookie.split(";")]
    assert parts[0].startswith("clinically_session=") and len(parts[0]) > len("clinically_session=")
    assert "HttpOnly" in parts
    assert "SameSite=Lax" in parts
    assert "Path=/" in parts
    assert f"Max-Age={30 * 24 * 3600}" in parts
    assert "Secure" not in parts
    assert not any(p.lower().startswith("domain") for p in parts)


def test_login_by_email_is_case_insensitive(client):
    client.post(
        "/api/auth/doctor/register",
        json={"fullName": "Dr Ravi Kumar", "method": "email", "email": "ravi@example.com", "password": PASSWORD},
    )
    r = client.post(
        "/api/auth/doctor/login", json={"method": "email", "email": "Ravi@Example.com", "password": PASSWORD}
    )
    assert r.status_code == 200


def test_wrong_password_and_unknown_account_look_the_same(client):
    register(client)
    wrong = login(client, password="wrongpass1")
    unknown = login(client, phone="9123456789")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == BAD_LOGIN
    assert "set-cookie" not in wrong.headers and "set-cookie" not in unknown.headers


def test_session_token_is_stored_hashed(client, db):
    register(client)
    token = session_token(login(client))
    rows = db.execute(text("SELECT token_hash FROM sessions")).scalars().all()
    assert len(rows) == 1
    assert token not in rows[0]
    import hashlib

    assert rows[0] == hashlib.sha256(token.encode()).hexdigest()


def test_every_attempt_is_recorded(client, db):
    register(client)
    login(client, password="wrongpass1", **{"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
    login(client)
    rows = db.execute(text("SELECT identifier, ip, succeeded FROM login_attempts ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [(PHONE, "203.0.113.7", False), (PHONE, "testclient", True)]


def test_sixth_failed_login_is_rate_limited(client):
    register(client)
    for _ in range(5):
        assert login(client, password="wrongpass1").status_code == 401
    r = login(client, password="wrongpass1")
    assert r.status_code == 429
    assert r.json() == {"message": "Too many attempts. Wait a few minutes and try again."}
    # Even the right password is refused while locked.
    assert login(client).status_code == 429


def test_ip_rate_limit(client):
    for i in range(20):
        assert login(client, phone=f"90000000{i:02d}", **{"X-Forwarded-For": "198.51.100.1"}).status_code == 401
    assert login(client, phone="9111111111", **{"X-Forwarded-For": "198.51.100.1"}).status_code == 429
    # A different IP is unaffected.
    assert login(client, phone="9111111111", **{"X-Forwarded-For": "198.51.100.2"}).status_code == 401


def test_outdated_hash_is_upgraded_on_login(client, db):
    from argon2 import PasswordHasher

    register(client)
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    with db.begin():
        db.execute(text("UPDATE doctors SET password_hash = :h"), {"h": weak})
    assert login(client).status_code == 200
    assert db.scalar(text("SELECT password_hash FROM doctors")) != weak


# --- me / logout ----------------------------------------------------------


def test_me(client):
    doctor_id = register(client).json()["id"]
    token = session_token(login(client))

    r = me(client, token)
    assert r.status_code == 200
    assert r.json() == {"doctor": {"id": doctor_id, "fullName": "Dr Anjali Singh"}}

    for bad in (None, "garbage"):
        r = me(client, bad)
        assert r.status_code == 401
        assert r.json() == {"message": "Please sign in."}


def test_me_with_expired_session(client, db):
    register(client)
    token = session_token(login(client))
    with db.begin():
        db.execute(text("UPDATE sessions SET expires_at = now() - interval '1 minute'"))
    r = me(client, token)
    assert r.status_code == 401
    assert r.json() == {"message": "Please sign in."}
    assert db.scalar(text("SELECT count(*) FROM sessions")) == 0


def test_logout(client):
    register(client)
    token = session_token(login(client))
    assert me(client, token).status_code == 200

    r = client.post(
        "/api/auth/logout",
        headers={"Content-Type": "application/json", "Cookie": f"clinically_session={token}"},
    )
    assert r.status_code == 204
    assert r.content == b""
    assert r.headers["set-cookie"] == "clinically_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
    assert me(client, token).status_code == 401


def test_logout_without_cookie_still_204(client):
    r = client.post("/api/auth/logout", headers={"Content-Type": "application/json"})
    assert r.status_code == 204
    assert "Max-Age=0" in r.headers["set-cookie"]


def test_secure_flag_when_configured(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "cookie_secure", True)
    register(client)
    assert login(client).headers["set-cookie"].endswith("; Secure")


# --- misc -----------------------------------------------------------------


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_unknown_route_uses_message_format(client):
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert set(r.json()) == {"message"}
