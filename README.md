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
| `MEDIA_PUBLIC_BASE_URL` | (empty)                              | CDN for public images; empty = `/api/media` |

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

#### Breaks with a set length

`pause` takes `{ "minutes": 5–180 }` (15 if left out) and sets `breakStartedAt` and `breakUntil`
on the session. `POST /api/doctor/sessions/:id/break/extend` `{ minutes }` adds time (from the
planned end, or from now if the break has run over; at most 4 hours in all). `resume` and `end`
clear both.

**Expected times move with the break.** For each waiting patient: minutes until their turn = time
left on the break (0 once it has run over) + patients ahead × the average consult time so far.
Before OPD starts it's the day's start (or now, if the doctor is late) + patients ahead × (hours ÷
limit). The patient's booking status returns `estimatedMinutes`, `expectedAt` (`HH:MM`, rounded
down to 5 minutes) and `breakUntil`; the dashboard returns `expected` = `{ bookingId: "HH:MM" }`
for waiting patients, and booking details return `expectedAt`. Patients' live `session.updated`
events include `breakUntil`.

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

### Directory: home feed, clinic pages, search (patient app)

Only clinics with at least one **verified** doctor are listed, and only verified doctors appear.
A doctor's **photo and registration number are public only if they agreed** (`photoConsent` in
their profile, stored as `photo_consent_at`); otherwise `photoUrl` and `registrationNo` are null
and patients see `initials`.

- **`GET /api/public/home?areaId=&lat=&lng=&type=all|clinic|hospital&openNow=true&department=&q=&cursor=`**
  → `{ departments, clinics, nextCursor }`. `departments` is the fixed list (`key, nameEn, nameHi,
  iconKey`, `app/departments.py`). `clinics` are nearest first, 10 per page: distance from
  `lat,lng`, else the area's centre (`GET /api/public/areas`), else Patna's centre. Clinics
  without a position come last. `department` (a key) keeps clinics with a doctor in it; `q` keeps
  clinics whose name or area, or a doctor's name or department (EN/HI), contains the words. Each
  clinic: `departmentKeys`, `id, name, nameHi, type, coverPhotoUrl, address,
  areaName, distanceKm, doctorCount, departmentCount, summary` ("2 doctors, Orthopaedics"),
  `opdStatus` (`OPEN_NOW` | `OPENS_LATER` with `opensAt` | `CLOSED_TODAY`), `waitingNow`, and
  `doctors` (first 5: `id, doctorClinicId, name, nameHi, department, departmentKey,
  designation, degree, experienceYears, fee, registrationNo, photoUrl, initials`).
- **`GET /api/public/clinics/:clinicId`** → type, name, address, `lat/lng`
  (`locationIsApproximate` when it's the area's centre), phone, `opdHoursTextEn/Hi`,
  `coverPhoto` and `photos` (`large`/`small` URLs), `opdStatus`, `opensAt`, `waitingNow`, and the
  `departments` present.
- **`GET /api/public/clinics/:clinicId/doctors?department=<key>&cursor=`** → `{ doctors,
  nextCursor }`, 10 per page, one entry per OPD slot (`doctorClinicId` for booking), with
  `opdStatus`, `waitingToday` (when open) or `opensAt`.
- **`GET /api/public/search?q=`** → `{ results }`, up to 20, mixed: each has `type` = `doctor`,
  `clinic` or `hospital`, `id, name, nameHi, subtitle, photoUrl`, and `clinicId` /
  `doctorClinicId`. Matches clinic names (English or Hindi) and areas, doctor names, and
  departments ("bone", "हड्डी रोग" or "heart" find those doctors). Names starting with the words
  come first.

**Opening status** comes from the day's plan (`app/schedule.py`) and today's session: a running
OPD (in OPD or on a break) is always `OPEN_NOW`, even past its hours; an ended one is
`CLOSED_TODAY`. A clinic is open if any of its doctors is. Statuses and waiting counts are cached
for 30 seconds per slot (`app/directory.py`); clinic edits clear the cache. Paging cursors are
opaque strings; a bad one gets **400**.

### Clinic details and photos (doctor)

Any doctor with an OPD slot at a clinic can edit it (others get **404**). Every change goes into
the activity log.
- `GET /api/doctor/clinic-details/:clinicId` → the clinic, its photos and the list of areas
- `PATCH /api/doctor/clinic-details/:clinicId` `{ type?, name?, nameHi?, address?, areaKey?,
  lat?, lng?, phone?, opdHoursTextEn?, opdHoursTextHi? }`. Only the fields sent change; send
  `lat` and `lng` together.
- `POST /api/doctor/clinic-details/:clinicId/photos` `{ image: "data:image/…;base64,…", cover? }`
  → **201**, up to 12 photos. The first photo becomes the cover.
- `DELETE …/photos/:photoId`, `PUT …/photos/order` `{ photoIds }`, `PUT …/cover` `{ photoId }`

**Images** (`app/images.py`): JPEG, PNG or WebP of at most 5 MB and at least 200 px a side,
decoded with Pillow (the bytes decide the type), turned upright from EXIF, and saved again as
JPEG at **1200 px and 400 px** wide. Re-encoding removes all metadata, including GPS. Files go
through `app/storage.py`: `LocalStorage` under `MEDIA_DIR`, served at `/api/media/<key>` with
long cache headers. For production, add an S3 storage class there and set
`MEDIA_PUBLIC_BASE_URL` to the CDN. These are public marketing images; doctors' photos and
medical records never go through this path.

**Doctor profile** adds `designation` (e.g. "Director & HOD, Medical Oncology"), `nameHi` and
`photoConsent` (true/false; left out when editing = unchanged). The free-text specialisation is
matched to a department (`departmentKey`).

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

## WhatsApp updates

Patients who tick "Send token updates on WhatsApp" (ticked by default; walk-ins with a number too) get template messages in the language they booked in (`en`/`hi`), from `app/notifications.py`:

| Message | When |
|---|---|
| `clinically_token_booked` | Booking or walk-in: token, doctor, clinic, day, OPD start, live link |
| `clinically_opd_started` | Start OPD: patients before you, live link |
| `clinically_doctor_break` | Each break: minutes and when the doctor is back |
| `clinically_get_ready` | 2 or fewer patients before you (not if "OPD started" already said so) |
| `clinically_you_are_next` | You're next |
| `clinically_your_turn` | Called in |
| `clinically_opd_closed` | OPD ended while you were still waiting |
| `clinically_token_cancelled` | The clinic closed that day (timings), token cancelled |

Each booking gets each message once (breaks: once per break). Every message with a link gets its own tracking code (`tracking_links`). Without opt-in or a number, the message is recorded as SKIPPED; the patient's queue page still lists what was sent.

**Going live:** the templates in `docs/whatsapp-templates.md` (generated by `python -m scripts.whatsapp_templates` from the code, so the wording matches) must be approved in WhatsApp Manager in English and Hindi. Then set `NOTIFY_CHANNEL=whatsapp`, `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID`. Errors that retrying can't fix (number not on WhatsApp, template missing) fail at once; others retry with back-off.

## Patient accounts

Routes under `/api/patient` (app/routers/patient.py). Cookie `clinically_patient`, separate from doctors' `clinically_session`, so one browser can hold both.

- `POST /auth/register` `{ fullName, method: phone|email, phone|email, password }` → **201** `{ account, members }`, signed in. Creates the patient's own profile (`relation: SELF`).
- `POST /auth/login`, `POST /auth/logout`, `GET /me`. Sign-in failures are rate-limited like doctors' (counted separately).
- `POST /auth/forgot` `{ email }` always answers the same; if the account exists, a one-hour, one-use link to `SITE_URL/reset-password?token=…` is emailed (`EMAIL_CHANNEL`: log, fake, resend). `POST /auth/reset` `{ token, password }` sets it and signs out every other device. Phone-only accounts can't reset by themselves yet (no SMS).
- Family profiles: `POST /members`, `PUT /members/{id}` (whole profile), `DELETE /members/{id}` (not SELF; deletes files they uploaded, clinics keep their visit history). Up to 8. Fields: name, relation, phone, dateOfBirth or age, gender, height, weight, bloodGroup, allergies, conditions, surgeries, medicines, emergency contact, address, recordsConsent.
- Records: `GET|POST /members/{id}/records` (`{ kind: PRESCRIPTION|REPORT, title, date, file: data URL }`, JPEG/PNG/WebP/PDF checked by their bytes, 5 MB), `GET /records/{id}/file`, `DELETE /records/{id}` (patient-added only). Stored with `get_record_storage()`: `RECORDS_DIR` locally, Postgres with `STORAGE_BACKEND=db`.
- Bookings: `GET /bookings` (every profile, newest 50). `POST /bookings/{id}/link` gives this device its own tracking code (`tracking_links`); earlier links keep working. `POST /api/public/bookings` with `memberId` (signed in) books for that profile and updates its name, age, gender and consent.
- Doctors see a profile's health details (`health` in the booking detail) and records only with that profile's consent.

**Safety rules:** phone numbers aren't verified, so a new account never picks up earlier bookings or records with the same number, and guest bookings never match a profile in an account (`find_or_create_patient` skips them). Everything is scoped to the signed-in account; other ids answer 404.

## Free trial rollout

- **Registration** sets `trial_ends_at` to now + `TRIAL_DAYS` (90). `GET /api/doctor/me` returns it as `doctor.trialEndsAt` to the doctor only (null for staff). Nothing is locked when it passes; the dashboard shows a banner.
- **`AUTO_VERIFY_DOCTORS=true`** makes new doctors visible to patients straight away (default false: an admin sets `is_verified`). Tests always run with it off.
- **`POST /api/doctor/clinic-details`** (doctor only) adds a clinic and the doctor's OPD there. Body `{ type?: CLINIC|HOSPITAL, name, address, areaKey, phone?, fee, days: ["MON",…], start: "HH:MM", end: "HH:MM", limit? }`. Writes the clinic, the doctor-clinic slot and weekly hours rows for the chosen days. **201** with the slot (same shape as `doctor.clinics[]`). Up to 10 clinics.
- **`GET /api/public/clinics/{doctorClinicId}/availability`** also returns `doctor`, `clinic`, `fee` and `tokenAmount`, so the booking page needs one call.

### Live updates for patients

A tracking page connects to Socket.IO with **no cookie**, sending `auth: { bookingId, code }` (its tracking link). It joins `booking:<id>` and `session:<sessionId>` only, and gets `booking.updated` and `session.updated` (token numbers and queue status, no patient details). A wrong code is refused.

When the website's host can't forward WebSockets (Vercel), browsers connect to this server directly. The dashboard first calls **`GET /api/auth/realtime-ticket`** through the website (cookie checked) and connects with `auth: { ticket }`. A ticket works once, for 60 seconds. `FRONTEND_ORIGIN` must list the website's address (several, comma-separated, are allowed).

## Hosting (free): Render + Neon

1. **Neon** (neon.tech): create a project in the Singapore region and copy its connection string.
2. **Render** (render.com): New → Blueprint → this repo. `render.yaml` sets everything except `DATABASE_URL` (the Neon string; `postgresql://` is accepted as is) and `FRONTEND_ORIGIN` (the Vercel address). Migrations run on every start.
3. Images go in the database (`STORAGE_BACKEND=db`), because the free plan's disk is wiped on every restart. Uploaded medical record files still use `RECORDS_DIR` on disk: don't rely on them on the free plan.
4. The free plan sleeps after 15 idle minutes. `.github/workflows/keep-awake.yml` calls `GET /api/ping` (which doesn't touch the database, so Neon can still sleep) every 10 minutes from 6:30 am to 11:30 pm IST. Set the repository variable `API_URL` to turn it on.

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
app/departments.py   fixed department list (EN/HI, icons) and Patna areas
app/directory.py     public directory: clinic cards, opening status (cached), distance, paging
app/images.py        image checks and resizing (1200/400 px)
app/storage.py       where public images are stored (local; S3 plugs in here)
app/routers/directory.py  home feed, clinic page, clinic doctors, search, /api/media
app/routers/clinics.py    doctors editing their clinics and photos
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
