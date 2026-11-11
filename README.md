# Clinically backend

API for Clinically, a doctor appointment and OPD-queue app for Patna. Built so far: doctor
registration, sign-in sessions, the full data model, `GET /api/doctor/me`, and the patient-facing
doctor listing, booking, booking status and cancellation.

FastAPI · PostgreSQL 16 · SQLAlchemy 2 · Alembic · argon2id

## Setup

Requires Python 3.11 and Docker.

```sh
python3.11 -m venv .venv && source .venv/bin/activate   # or: conda create -p ./.venv python=3.11
pip install -r requirements-dev.txt
cp .env.example .env

docker compose up -d
alembic upgrade head
python -m scripts.seed          # optional demo data, see below
uvicorn app.main:app --port 4000
```

### Demo data

`python -m scripts.seed` adds Dr. Rajeev Anand (Orthopaedics) at Anand Ortho Care, Kankarbagh, with
today's OPD session (today in Asia/Kolkata) and 12 bookings in mixed states. It prints the sign-in
details (phone `9801234567`, password `demo1234`) and one patient tracking code. Running it again
does nothing; `python -m scripts.seed --reset` **empties every table** first, so only use it on a
dev database.

Run the tests with `pytest`. They use a separate database (`TEST_DATABASE_URL`, by default
`clinically_test` on the same server). It is created and migrated automatically, and the tables are
emptied before each test.

### Settings (env vars or `.env`)

| Name               | Default                                   | Notes                                     |
|--------------------|-------------------------------------------|-------------------------------------------|
| `DATABASE_URL`     | `postgresql+psycopg://clinically:…/clinically` | Use the `postgresql+psycopg://` scheme |
| `COOKIE_SECURE`    | `false`                                   | Set to `true` in production (HTTPS)       |
| `SESSION_TTL_DAYS` | `30`                                      |                                           |

## How requests arrive

The browser never calls this server directly. The Next.js server forwards `/api/*` to
`http://localhost:4000/api/*` unchanged, so the session cookie belongs to the frontend's site.
That's why there is no CORS middleware.

**Port 4000 must not be reachable from the internet.** The client IP used for rate limiting comes
from the first value of `X-Forwarded-For`, which only the proxy may set.

## API contract

Every error body is `{ "message": "<sentence shown to the doctor>" }`, including validation errors
(400, never 422) and unknown routes. Any `POST` whose `Content-Type` isn't `application/json`
gets **415**. Together with `SameSite=Lax`, this blocks cross-site form posts.

Validation rules:
- `fullName`: trimmed, at least 3 characters
- `phone`: `^[6-9][0-9]{9}$`
- `email`: `^[^\s@]+@[^\s@]+\.[^\s@]{2,}$`, trimmed and lowercased
- `password` on register: at least 8 characters, with at least one letter and one digit
- unknown fields are rejected

### `POST /api/auth/doctor/register`
Body: `{ "fullName", "method": "phone", "phone", "password" }` or
`{ "fullName", "method": "email", "email", "password" }`

- **201** `{ "id": "<uuid>" }`. This does not sign the doctor in and sets no cookie.
- **400** if validation fails
- **409** `An account with this phone number or email already exists. Sign in instead.`

### `POST /api/auth/doctor/login`
Body: `{ "method": "phone", "phone", "password" }` or `{ "method": "email", "email", "password" }`

- **200** `{ "doctor": { "id", "fullName" } }` with
  `Set-Cookie: clinically_session=<token>; HttpOnly; SameSite=Lax; Path=/; Max-Age=<ttl seconds>`
  (plus `; Secure` when `COOKIE_SECURE=true`, and never a `Domain`)
- **401** `That mobile number, email or password is wrong. Check and try again.`, for both an
  unknown account and a wrong password, with the same timing
- **429** `Too many attempts. Wait a few minutes and try again.` after 5 failed attempts for an
  identifier, or 20 from an IP, within 15 minutes

Every attempt is recorded in `login_attempts`, including ones refused with 429.

### `GET /api/auth/me`
- **200** `{ "doctor": { "id", "fullName" } }`
- **401** `Please sign in.` when there's no cookie, the session is unknown, or it has expired
  (expired sessions are deleted)

### `POST /api/auth/logout`
- Always **204**, with `Set-Cookie: clinically_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0`.
  Send `Content-Type: application/json`, since this is a POST like the others.

### `GET /api/doctor/me`
- **200** `{ "doctor": { "id", "fullName", "phone", "email", "department", "qualification",
  "experienceYears", "registrationNo", "verified", "clinics": [{ "doctorClinicId", "clinicId",
  "name", "area", "address", "phone", "fee", "opdDays", "opdStart", "opdEnd" }] } }`.
  `fee` is in whole rupees; `opdStart`/`opdEnd` are `HH:MM` Asia/Kolkata times.
- **401** `Please sign in.`

### Patient app (`/api/public`, no login)

Only verified doctors are listed or bookable. A booking is protected by its **tracking code**, which
is returned once at booking and stored only as a hash. An unknown booking and a wrong code both
return 404.

#### `GET /api/public/doctors?search=&department=`
One entry per doctor per clinic: `{ id, doctorId, fullName, department, qualification,
experienceYears, fee, waitingToday, clinic: {doctorClinicId, clinicId, name, area, address, phone,
fee, opdDays, opdStart, opdEnd} }`. `search` matches doctor name, department, clinic name or area.
`department` is an exact, case-insensitive match.

#### `GET /api/public/doctors/:id`
`{ id, fullName, department, qualification, experienceYears, clinics: [{ …clinic, waitingToday,
todaySessionStatus, nowServing }] }`

#### `POST /api/public/bookings`
Body: `{ doctorClinicId, date: "YYYY-MM-DD", visitType: NEW|FOLLOW_UP|REPORT_REVIEW,
patient: { name, phone, age, gender: MALE|FEMALE|OTHER, heightCm?, weightKg? }, recordsConsent }`

- **201** `{ bookingId, token, trackingCode }`
- **400** for invalid input, a date outside today to 7 days ahead, a day the doctor has no OPD there,
  or today after OPD hours have ended
- **404** if the doctor or clinic is unknown or unverified
- **409** if the session is closed, or the patient already has an active token in it

Rules:
- **Tokens:** the day's session is created on the first booking. Tokens are assigned while holding a
  row lock on the session, so parallel bookings never get the same number. Cancelled tokens are
  never reused.
- **Patients:** the same phone and name (ignoring case and extra spaces) is the same patient, so
  earlier tests and records carry over. Height and weight update to the latest values.
  `recordsConsent: true` grants consent (keeping the first consent time); `false` withdraws it.

#### `GET /api/public/bookings/:id?code=`
`{ bookingId, token, status, nowServing, patientsAhead, estimatedMinutes, sessionStatus, sessionId,
date, doctorName, clinicName, opdStart }`. `patientsAhead` counts WAITING tokens before this one.
`estimatedMinutes` = patients ahead × today's average consult time in this session (10 minutes
until someone has been seen), and is `null` once the patient isn't waiting.

#### `POST /api/public/bookings/:id/cancel`
Body `{ code, reason? }`. **200** `{ bookingId, status: "CANCELLED" }`; **409** unless the booking is
WAITING.

### `GET /api/health`
- **200** `{ "status": "ok" }` after `SELECT 1`, or **503** if the database is unreachable

## Curl walkthrough

With the server running on port 4000:

```sh
J='Content-Type: application/json'

curl -i -X POST localhost:4000/api/auth/doctor/register -H "$J" \
  -d '{"fullName":"Dr Anjali Singh","method":"phone","phone":"9876543210","password":"secret123"}'
# 201 {"id":"…"}

curl -i -c cookies.txt -X POST localhost:4000/api/auth/doctor/login -H "$J" \
  -d '{"method":"phone","phone":"9876543210","password":"secret123"}'
# 200 + Set-Cookie: clinically_session=…

curl -i -b cookies.txt localhost:4000/api/auth/me
# 200 {"doctor":{…}}

curl -i -b cookies.txt -c cookies.txt -X POST localhost:4000/api/auth/logout -H "$J"
# 204, cookie cleared

curl -i -b cookies.txt localhost:4000/api/auth/me
# 401 {"message":"Please sign in."}
```

## Layout

```
app/main.py          app, error handlers, JSON-only POST guard, /api/health
app/config.py        settings
app/db.py            engine and session dependency
app/models.py        all tables
app/schemas.py       request bodies and validation messages
app/security.py      argon2, session tokens, cookie headers
app/deps.py          signed-in doctor dependency
app/timeutil.py      Asia/Kolkata dates
app/routers/auth.py  register, login, me, logout
app/routers/doctor.py  doctor-side endpoints
app/routers/public.py  patient-facing endpoints
app/bookings.py      booking rules (token locking, patient matching, queue estimates)
app/events.py        real-time event hub (Socket.IO plugs in here)
app/serializers.py   shared JSON shapes
scripts/seed.py      demo data
alembic/             migrations
tests/               pytest suite (uses a separate database)
```
