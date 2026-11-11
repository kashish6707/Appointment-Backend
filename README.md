# Clinically backend

API for Clinically, a doctor appointment and OPD-queue app for Patna. Built so far: doctor
registration, sign-in sessions, the full data model, `GET /api/doctor/me`, and the patient-facing
doctor listing, booking with an online token-amount payment (Razorpay), booking status and
cancellation with refunds, and the doctor's queue controls (start/pause/resume/end, call next,
outcomes, walk-ins).

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
| `PAYMENT_GATEWAY`  | `razorpay`                                | `fake` for local dev and tests (no keys)  |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` |                     | From the Razorpay dashboard (API keys)    |
| `RAZORPAY_WEBHOOK_SECRET` |                                    | Secret you set on the webhook             |
| `FRONTEND_ORIGIN`  | `http://localhost:3000`                   | Live updates only accept this origin      |
| `RECORDS_DIR`      | `storage/records`                         | Folder holding prescription/report files  |
| `MEDIA_DIR`        | `storage/media`                           | Folder holding doctor photos              |
| `NOTIFY_CHANNEL`   | `log`                                     | How messages reach patients (see below)   |

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
Body: `{ "fullName", "method": "phone", "phone", "password", "profile" }` or
`{ "fullName", "method": "email", "email", "password", "profile" }` (`profile` is below)

- **201** `{ "id": "<uuid>" }`. This does not sign the doctor in and sets no cookie.
- **400** if validation fails
- **409** `An account with this phone number or email already exists. Sign in instead.`

#### The doctor's profile (required at registration)

`profile` = `{ specialisation?, experienceYears, degrees, fee, registrationNo, medicalCouncil?,
photo, about?, achievements?, expertise? }`:
- **Required:** `experienceYears` (0–70), `degrees` (1–15, each `{ level: UG | PG |
  SUPER_SPECIALITY | FELLOWSHIP | DIPLOMA | OTHER, name, institute?, year? }`), `fee` (whole
  rupees, 0–1,00,000; the doctor's standard fee, which their clinics start from),
  `registrationNo` (3–30 letters, numbers, `/ . -`; stored in capitals) and `photo`.
- **`photo`** is a data URL (`data:image/jpeg;base64,…`) of at most 2 MB. Its first bytes must be
  a real JPEG, PNG or WebP (what the browser claims is ignored). It's saved under `MEDIA_DIR` with
  a random name. The frontend crops and shrinks it before sending.
- **Optional:** `specialisation` (stored as `department`), `medicalCouncil`, `about` (≤ 1,500),
  `achievements` (≤ 20 × 200 characters) and `expertise` (≤ 20 × 60), with blanks and repeats
  dropped.
- `qualification` is filled in from the degrees, e.g. `MBBS, MS (Orthopaedics)`.

Afterwards (doctor only, staff get **403**):
- `GET /api/doctor/profile` → the profile with `registrationNo`, `medicalCouncil`, `photoUrl` and
  `complete` (false for accounts made before these fields were required)
- `PUT /api/doctor/profile` → same body, plus optional `fullName`. Leave out `photo` to keep it.
  The old photo file is deleted when replaced.
- `GET /api/doctor/profile/photo` → the doctor's own photo

**Patients** see the profile (photo, specialisation, experience, degrees, fee, about,
achievements, expertise) in `/api/public/doctors` and `/api/public/doctors/:id` once the doctor is
verified. The photo is at `GET /api/public/doctors/:id/photo` (verified doctors only). The
registration number, council and contact details are never public. `/api/doctor/me` adds
`photoUrl` and `profileComplete`.

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

### Staff accounts and access (like AWS IAM)

- **The doctor is root:** every permission, every clinic, and the only one who can manage staff.
- **Staff** (receptionist, compounder) have one account each. Each doctor who adds them grants a
  role preset, the exact permissions, and which of their clinic slots it covers
  (`staff_memberships`). A staff member working for several doctors picks one per browser
  (`POST /api/auth/staff/workplace`); with only one, it's picked automatically.
- **Permissions:** `queue.view` (always included), `queue.call`, `opd.manage`, `walkins.add`,
  `bookings.outcome`, `tests.manage`, `records.view`, `patients.correct`, `schedule.manage`. Presets:
  RECEPTIONIST = view, call, walk-ins, correct details; COMPOUNDER = view, call, walk-ins,
  outcomes. Missing permission → **403** naming it. Another clinic's data → **404**.
- **Sign-in:** `POST /api/auth/login` (same body as doctor login, which still works) signs in a
  doctor or a staff member. A phone number or email belongs to one account only.
- **Invites:** adding a new person returns `inviteUrl` (`/staff/invite/<token>`, valid 48 hours,
  works once) for the doctor to send them. `GET /api/auth/staff/invite/:token` shows who invited
  them. `POST` with `{ password }` sets their password and signs them in. Only the hash of the
  token is stored.
- **Live updates** for staff only cover their clinics (`slot:<doctorClinicId>` rooms). Changing or
  removing a grant disconnects their open dashboards so they reload with the new access.
- **Activity log** (`audit_log`): every queue action, outcome, walk-in, test, correction and staff
  change, with who did it. Never edited by the app.

Doctor-only routes (**403** `Only the doctor can do this.` for staff):
- `GET /api/doctor/staff` → `{ staff, permissions, presets }`
- `POST /api/doctor/staff` `{ fullName, phone | email, role, permissions, doctorClinicIds }` →
  **201** `{ member, inviteUrl }` (`inviteUrl` is null if they already have an account).
  **409** if the contact belongs to a doctor or they're already on your staff.
- `PATCH /api/doctor/staff/:id` `{ role?, permissions?, doctorClinicIds? }`
- `DELETE /api/doctor/staff/:id` → **204**
- `POST /api/doctor/staff/:id/invite` → a new link (the old one stops working)
- `GET /api/doctor/activity?limit=50&staffId=` → `{ entries }`, newest first

### OPD timings, days off and daily limits

Each clinic slot has **weekly hours**: per weekday, open or not, start and end, and a **daily
patient limit** (online + walk-in tokens, cancellations not counted). **Date exceptions** change
one date or a range: a day off (`CLOSED`), or different hours and/or limit (`HOURS`, which can also
open a normally-off day), with an optional note shown to patients. `app/schedule.py` works out
the plan for any date (exception, then weekly hours, then the slot's old `opd_days`/`opd_start`/
`opd_end` with no limit if weekly hours were never saved). Booking, the dashboard, the calendar
and the patient app all use it.

- **Limit:** online booking stops when the day is full (**409** `All tokens for that day are
  booked…`, checked before taking a payment and again when the token is given). A walk-in at the
  limit gets **409** `{ message, code: "LIMIT_REACHED", limit, booked }`; send it again with
  `"overLimit": true` once the desk confirms.
- **Taking away a booked day** (day off, turning off a weekday, removing an exception that opened
  a day) gets **409** `{ message, code: "HAS_BOOKINGS", count, dates }` and changes nothing. With
  `confirmCancel: true` the waiting bookings on those days are cancelled (`cancelledBy: CLINIC`,
  reason `Doctor unavailable`), online token payments refunded, and the dashboards updated.
  Shorter hours or a lower limit never cancel anyone; they only stop new bookings.
- **Expected time:** before OPD starts, a patient's status includes `expectedAt` (`HH:MM`):
  the day's start + patients ahead × (hours ÷ limit), or the usual consult time when there's no
  limit. Also `opdStart`, `opdEnd` and `dayNote` for that day.

Endpoints (permission `schedule.manage`; the doctor always has it):
- `GET /api/doctor/clinics/:doctorClinicId/schedule` → `{ clinic, weekly, exceptions, upcoming,
  bookingWindowDays }` (`upcoming`: each bookable day's plan and tokens booked)
- `PUT /api/doctor/clinics/:doctorClinicId/weekly` `{ days: [7 × { weekday, open, start, end,
  limit }], confirmCancel? }`
- `POST /api/doctor/clinics/:doctorClinicId/exceptions` `{ from, to?, kind: "CLOSED" | "HOURS",
  start?, end?, limit?, note?, confirmCancel? }` (today or later, up to 90 days at once)
- `DELETE /api/doctor/clinics/:doctorClinicId/exceptions/:date?confirmCancel=`

For the patient app: **`GET /api/public/clinics/:doctorClinicId/availability`** → today and the
next 7 days, each `{ date, open, start, end, limit, note, exception, booked, left, bookable,
reason }` with `reason` = `NOT_AVAILABLE`, `HOURS_OVER`, `FULL` or null.
`GET /api/doctor/dashboard` also returns `plan` for the day, and calendar days carry `limit`,
`exception` and `note`.

### Correcting patient details

`PATCH /api/doctor/bookings/:id/patient` `{ name?, age?, gender?, heightCm?, weightKg? }`
(needs `patients.correct`). Patients are shared by every doctor and matched by phone + name, so
**name, age and gender are saved as this doctor's correction** (`patient_corrections`) and only
change what this doctor and their staff see. Height and weight are saved on this visit. The phone
number and records consent can't be changed here. Every change is in the activity log. Returns the
booking details, with `correction` = `{ updatedBy, updatedAt, original }` or null.

### `GET /api/doctor/me`
Works for doctors and staff. Also returns `viewer` = `{ type: "DOCTOR" | "STAFF", id, fullName,
role, permissions }` and `workplaces` (staff: the doctors they work for). For staff, `clinics` only
lists the clinics their grant covers, and the doctor's phone, email and registration number are
null. A staff member who hasn't picked a doctor yet gets `doctor: null`.
- **200** `{ "viewer", "workplaces", "doctor": { "id", "fullName", "phone", "email", "department", "qualification",
  "experienceYears", "registrationNo", "verified", "clinics": [{ "doctorClinicId", "clinicId",
  "name", "area", "address", "phone", "fee", "opdDays", "opdStart", "opdEnd" }] } }`.
  `fee` is in whole rupees; `opdStart`/`opdEnd` are `HH:MM` Asia/Kolkata times.
- **401** `Please sign in.`

### Running the OPD queue (doctor, signed in)

Each of these locks the session row first, the same lock bookings take, so a double-clicked
"Next" or a booking arriving at the same moment can't scramble the queue. A session or booking that
belongs to another doctor returns 404, the same as one that doesn't exist. Responses with a session
use `{ id, doctorClinicId, date, status, startedAt, currentToken }`; bookings use
`{ id, sessionId, token, status, visitType, source, bookedAt, calledAt, completedAt, cancelledAt,
cancelledBy, cancelReason, patient: { id, name, phone, age, gender } }`.

#### `POST /api/doctor/sessions/:id/start | pause | resume | end`
**200** `{ session }`. `start`: NOT_STARTED → IN_OPD. `pause`: IN_OPD → ON_BREAK. `resume`:
ON_BREAK → IN_OPD. These three only work on today's session. `end` closes a session on any date,
which is also how a doctor closes a future day for bookings. The patient currently with the doctor
is marked DONE; waiting patients are left as they are. **409** for a change that doesn't fit the
current status (the message says why).

#### Telling patients OPD has started

`start` also queues one message per **waiting** patient that day ("Dr. … has started OPD at …
Your token is 7. 3 patients before you."), in the same transaction (`notifications` table, an
outbox). They're sent in the background after the reply, and a loop retries failures every
minute (backing off 1, 2, 4, 8 minutes; FAILED after 5 tries). A booking is told at most once,
walk-ins without a phone are SKIPPED, and cancelled bookings get nothing. The `start` reply adds
`notified` and `notifications` = `{ pending, sent, failed, skipped }`; the dashboard returns the
same summary, and the patient's booking status returns `messages` and `sessionStartedAt`.

**Sending:** `NOTIFY_CHANNEL=log` (default) only writes each message to the server output
(`INFO: clinically.messages: Message to +91 …`). To send real SMS or WhatsApp, add a sender
class in `app/messaging.py` (one `send(phone, text)` method, e.g. MSG91 or the WhatsApp Cloud API)
and its keys in settings. Indian SMS needs the message text registered as a DLT template first.

#### `POST /api/doctor/sessions/:id/reopen`
Undoes `end`, for example when it was pressed by mistake. Works on today's session or a coming day
(**409** for a past day, or if the session isn't closed). The session goes back to IN_OPD if it had
started, otherwise NOT_STARTED, and takes bookings again. The patient `end` marked DONE goes back to
WITH_DOCTOR, unless their status was changed after ending. **200** `{ session, restored }`.
Sessions carry `closedAt`, the time `end` was pressed (null when open).

#### `POST /api/doctor/sessions/:id/next`
Marks the patient with the doctor DONE and calls the lowest WAITING token (skipping cancelled and
no-show ones). **200** `{ session, finished: booking | null, current: booking | null }`; `current` is
`null` when nobody is waiting. **409** unless OPD is running today.

#### `PATCH /api/doctor/bookings/:id/status`
Body `{ status: DONE | SENT_FOR_TESTS | NO_SHOW }`. **200** booking. Also used to correct a mistake.
**409** for a cancelled booking.

#### `POST /api/doctor/sessions/:id/walk-ins`
Body `{ name, phone?, age, gender, visitType? (default NEW) }`. **201** `{ booking, trackingCode }`.
Today's open session only. Walk-ins pay in full at reception; their records consent is never
changed here.

### Real-time events

Every change is emitted through `app/events.py` after it's committed. `app/realtime.py` sends the
doctor events over **Socket.IO at `/api/socket.io`** (forwarded by Next.js like the rest of `/api`).
A connection needs a valid `clinically_session` cookie and an `Origin` equal to `FRONTEND_ORIGIN`;
it then joins `doctor:<doctorId>`. The Next.js proxy redirects a trailing slash away, so clients
must connect to `/api/socket.io` without one (socket.io-client: `addTrailingSlash: false`).
Rooms live in process memory, so run one worker (or add a Socket.IO message queue first).

- `doctor:<doctorId>`: `booking.created`, `booking.updated`, `booking.cancelled` (payload: booking)
  and `session.updated` (payload: session)
- `session:<sessionId>`: `session.updated` `{ id, status, nowServing }`
- `booking:<bookingId>`: `booking.updated` `{ id, token, status }`

### Doctor dashboard (signed in)

Everything is limited to the doctor's own clinics; another doctor's booking, test or record gets
the same 404 as a missing one. `doctorClinicId` (from `/api/doctor/me`) picks the clinic and
defaults to the doctor's earliest OPD slot.

- **`GET /api/doctor/dashboard?date=YYYY-MM-DD&doctorClinicId=`** (date defaults to today):
  `{ date, clinic, isOpdDay, session | null, stats, bookings, cancelled }`.
  `stats` = `{ booked, online, walkIn, waiting, withDoctor, seen, sentForTests, noShow, cancelled }`
  (`booked` leaves out cancellations). `bookings` are by token; `cancelled` newest first. For today or
  a coming OPD day the session is created if missing, so OPD can start before anyone books.
- **`GET /api/doctor/calendar?month=YYYY-MM&doctorClinicId=`**: `{ month, doctorClinicId, days }`,
  each day `{ date, bookings, isOpdDay, sessionStatus, closed }`. `closed` = no OPD that weekday, or a
  coming day the doctor closed.
- **`GET /api/doctor/search?q=&date=&doctorClinicId=`**: `{ results }` (max 20 bookings, each with
  `date` and `clinicName`). Up to 4 digits = token on `date` (default today); 5+ digits = phone;
  otherwise name, across all days, newest first.
- **`GET /api/doctor/bookings/:id`**: `{ booking, session, queuePosition, patient, recordsConsent,
  recordsConsentAt, tests, records }`. `patient` adds `heightCm`, `weightKg`. `records` is empty
  unless the patient consented. `tests` are the ones this doctor advised (always shown).
- **`POST /api/doctor/bookings/:id/tests`** `{ "names": ["CBC", "X-ray knee"] }` (1–10 names,
  2–100 characters): adds the tests (skipping names already on the booking) and marks the booking
  `SENT_FOR_TESTS`. **201** `{ booking, tests }`. **409** if cancelled.
- **`POST /api/doctor/bookings/:id/call`**: like `sessions/:id/next`, but calls this waiting patient
  (out of turn). Same response and errors as `next`; **409** if the patient isn't waiting.
- **`PATCH /api/doctor/tests/:id`** `{ "status": "DONE" | "PENDING" }` → the test.
- **`GET /api/doctor/records/:id/file`**: the file, when the patient consented and has booked with
  this doctor. PDFs and JPEG/PNG/WebP open inline; anything else downloads, because the file is
  served from the app's own site. Always `Cache-Control: private, no-store`.

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

#### Online token amount

Each doctor-clinic slot has a `tokenAmount` (whole rupees, between 0 and `fee`). The patient pays it
online when booking; reception collects `fee - tokenAmount`. With `tokenAmount: 0` a booking is
confirmed straight away with no payment. This is also what stops fake bookings: every token costs
money.

Flow for a paid slot:
1. `POST /api/public/bookings` checks everything first (doctor, date, closed session, duplicate
   token), then creates a Razorpay order and returns
   `{ bookingId, token: null, trackingCode, payment: { orderId, amount (paise), currency, keyId } }`.
   **No token, patient record or consent change is made yet**, so an unpaid attempt changes nothing.
2. The frontend opens Razorpay Checkout with `payment`, then posts the success result to
   `POST /api/public/bookings/:id/payment`.
3. Razorpay's webhook confirms it as well (in case the patient closes the page first). Either path
   creates the booking and assigns the token, exactly once.
4. If the booking can't be made after paying (the session closed meanwhile, or the patient already
   holds a token), the money is refunded automatically.

Refunds: a patient who cancels **before that day's OPD starts** gets the token amount back
automatically. After OPD starts, or for a no-show, there's no refund. If Razorpay refuses a refund,
the payment is marked `REFUND_FAILED` (with the reason in `payments.note`) for the clinic to retry
from the Razorpay dashboard.

Razorpay setup: enable **automatic capture** for payments, and add a webhook pointing at
`https://<your site>/api/payments/razorpay/webhook` for the `payment.captured` and `order.paid`
events, with the same secret as `RAZORPAY_WEBHOOK_SECRET`.

#### `POST /api/public/bookings`
Body: `{ doctorClinicId, date: "YYYY-MM-DD", visitType: NEW|FOLLOW_UP|REPORT_REVIEW,
patient: { name, phone, age, gender: MALE|FEMALE|OTHER, heightCm?, weightKg? }, recordsConsent }`

- **201** `{ bookingId, token, trackingCode, payment }`. `token` is `null` and `payment` holds the
  Checkout details when a token amount is due; otherwise `payment` is `null`.
- **502** if Razorpay couldn't create the order
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

#### `POST /api/public/bookings/:id/payment`
Body: `{ code, razorpayOrderId, razorpayPaymentId, razorpaySignature }` (Checkout's success result).
**200** `{ bookingId, token, status }`; **400** if the signature doesn't verify; **409** if it was
paid but couldn't be booked (refund issued, reason in `message`).

#### `POST /api/payments/razorpay/webhook`
Called by Razorpay. Verified with `X-Razorpay-Signature`.

#### `GET /api/public/bookings/:id?code=`
`{ bookingId, token, status, nowServing, patientsAhead, estimatedMinutes, sessionStatus, sessionId,
date, doctorName, clinicName, opdStart, payment: { fee, paidOnline, payAtReception, refundStatus } }`.
Before payment, `status` is `PENDING_PAYMENT` (with `payment.checkout` to reopen Checkout),
`PAYMENT_EXPIRED` after 15 minutes, `CANCELLED`, or `PAYMENT_REFUNDED`, and `token` is `null`. A
late payment on an expired booking is still accepted. `patientsAhead` counts WAITING tokens before this one.
`estimatedMinutes` = patients ahead × today's average consult time in this session (10 minutes
until someone has been seen), and is `null` once the patient isn't waiting.

#### `POST /api/public/bookings/:id/cancel`
Body `{ code, reason? }`. **200** `{ bookingId, status: "CANCELLED", refund: { amount, status } | null }`;
**409** unless the booking is WAITING (or still unpaid).

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
app/main.py          FastAPI app (`api`) wrapped by Socket.IO (`app`), error handlers, JSON-only POST guard, /api/health
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
app/booking_payments.py  token-amount payments, confirmation and refunds
app/payments.py      Razorpay client and the fake gateway
app/routers/payments.py  Razorpay webhook
app/routers/queue.py   doctor's OPD queue controls
app/routers/dashboard.py  doctor dashboard: day, calendar, search, booking details, tests, record files, corrections
app/routers/staff.py   staff access management, invites, activity log (doctor only)
app/access.py        who is signed in, permissions, clinic limits, activity log helper
app/profile.py       doctor profile: photo storage, degrees, public and own views
app/routers/profile.py  doctor's own profile and the public doctor photo
app/notifications.py  messages to patients: outbox, sending and retries
app/messaging.py     senders (log for development; add SMS/WhatsApp here)
app/schedule.py      a date's OPD plan: weekly hours, exceptions, limit, expected times
app/routers/schedule.py  OPD timings, days off and limits (with cancel-and-refund)
app/realtime.py      Socket.IO server for live dashboard updates
app/notify.py        which events each change sends
app/events.py        real-time event hub (app/realtime.py forwards to Socket.IO)
app/serializers.py   shared JSON shapes
scripts/seed.py      demo data
alembic/             migrations
tests/               pytest suite (uses a separate database)
```
