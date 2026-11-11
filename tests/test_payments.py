import json
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from tests.test_public import book


def start_paid_booking(client, slot, **kwargs):
    r = book(client, slot, **kwargs)
    assert r.status_code == 201, r.text
    return r.json()


def pay(client, gateway, started, payment_id="pay_test123", signature=None):
    order_id = started["payment"]["orderId"]
    return client.post(
        f"/api/public/bookings/{started['bookingId']}/payment",
        json={
            "code": started["trackingCode"],
            "razorpayOrderId": order_id,
            "razorpayPaymentId": payment_id,
            "razorpaySignature": signature or gateway.sign_checkout(order_id, payment_id),
        },
    )


def webhook(client, gateway, order_id, payment_id="pay_hook123", event="payment.captured", sign=True):
    body = json.dumps(
        {"event": event, "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id}}}}
    ).encode()
    signature = gateway.sign_webhook(body) if sign else "bad"
    return client.post(
        "/api/payments/razorpay/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )


def status(client, started):
    return client.get(f"/api/public/bookings/{started['bookingId']}", params={"code": started["trackingCode"]})


def test_paid_slot_needs_payment_before_a_token(client, make_slot, gateway, db, emitted):
    slot = make_slot(token_amount=50)
    started = start_paid_booking(client, slot)
    assert started["token"] is None
    assert started["payment"]["amount"] == 5000  # paise
    assert started["payment"]["currency"] == "INR"
    assert started["payment"]["keyId"] == "rzp_test_fake"

    # Nothing is booked, and no patient exists, until payment is confirmed.
    assert db.scalar(text("SELECT count(*) FROM bookings")) == 0
    assert db.scalar(text("SELECT count(*) FROM patients")) == 0
    assert emitted == []
    s = status(client, started).json()
    assert s["status"] == "PENDING_PAYMENT" and s["token"] is None
    assert s["payment"]["checkout"]["orderId"] == started["payment"]["orderId"]

    r = pay(client, gateway, started)
    assert r.status_code == 200
    assert r.json()["token"] == 1
    assert (f"doctor:{slot.doctor_id}", "booking.created") in [(room, e) for room, e, _ in emitted]

    s = status(client, started).json()
    assert s["status"] == "WAITING" and s["token"] == 1
    assert s["payment"] == {"fee": 400, "paidOnline": 50, "payAtReception": 350, "refundStatus": None}
    # The pending patient details are cleared once they're on the patient row.
    assert db.scalar(text("SELECT request IS NULL FROM payments"))


def test_bad_signature_is_rejected(client, make_slot, gateway, db):
    started = start_paid_booking(client, make_slot(token_amount=50))
    r = pay(client, gateway, started, signature="0" * 64)
    assert r.status_code == 400
    assert "couldn't verify" in r.json()["message"]
    assert db.scalar(text("SELECT count(*) FROM bookings")) == 0


def test_webhook_confirms_payment(client, make_slot, gateway, db):
    started = start_paid_booking(client, make_slot(token_amount=50))
    assert webhook(client, gateway, started["payment"]["orderId"], sign=False).status_code == 400
    assert db.scalar(text("SELECT count(*) FROM bookings")) == 0

    assert webhook(client, gateway, started["payment"]["orderId"]).status_code == 200
    assert status(client, started).json()["token"] == 1
    # Repeated webhooks and the browser confirmation afterwards don't book twice.
    assert webhook(client, gateway, started["payment"]["orderId"]).status_code == 200
    assert pay(client, gateway, started).json()["token"] == 1
    assert db.scalar(text("SELECT count(*) FROM bookings")) == 1


def test_browser_and_webhook_racing_book_once(make_slot, gateway, db):
    with TestClient(app) as c:
        started = start_paid_booking(c, make_slot(token_amount=50))

    def via_browser(_):
        with TestClient(app) as c:
            return pay(c, gateway, started).status_code

    def via_webhook(_):
        with TestClient(app) as c:
            return webhook(c, gateway, started["payment"]["orderId"]).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(lambda i: (via_browser if i % 2 else via_webhook)(i), range(6)))
    assert codes == [200] * 6
    assert db.scalar(text("SELECT count(*) FROM bookings")) == 1


def test_tokens_follow_payment_order_not_booking_order(client, make_slot, gateway):
    slot = make_slot(token_amount=50)
    first = start_paid_booking(client, slot, phone="9431000001")
    second = start_paid_booking(client, slot, phone="9431000002")
    assert pay(client, gateway, second).json()["token"] == 1
    assert pay(client, gateway, first).json()["token"] == 2


def test_session_closed_while_paying_is_refunded(client, make_slot, gateway, db):
    slot = make_slot(token_amount=50)
    pay(client, gateway, start_paid_booking(client, slot, phone="9431000001"))  # creates the session
    started = start_paid_booking(client, slot, phone="9431000002")
    with db.begin():
        db.execute(text("UPDATE opd_sessions SET status = 'CLOSED'"))

    r = pay(client, gateway, started, payment_id="pay_late")
    assert r.status_code == 409
    assert "will be refunded" in r.json()["message"]
    assert gateway.refunds == [("pay_late", 5000)]
    assert db.scalar(text("SELECT status FROM payments WHERE gateway_payment_id = 'pay_late'")) == "REFUNDED"
    assert status(client, started).json()["status"] == "PAYMENT_REFUNDED"


def test_closed_session_is_refused_before_payment(client, make_slot, gateway, db):
    slot = make_slot(token_amount=50)
    pay(client, gateway, start_paid_booking(client, slot, phone="9431000001"))
    with db.begin():
        db.execute(text("UPDATE opd_sessions SET status = 'CLOSED'"))
    r = book(client, slot, phone="9431000002")
    assert r.status_code == 409
    assert db.scalar(text("SELECT count(*) FROM payments")) == 1  # only the first, paid one


def test_existing_token_is_refused_before_payment(client, make_slot, gateway, db):
    slot = make_slot(token_amount=50)
    pay(client, gateway, start_paid_booking(client, slot))
    r = book(client, slot)
    assert r.status_code == 409
    assert "already has token 1" in r.json()["message"]
    assert db.scalar(text("SELECT count(*) FROM payments")) == 1


def test_cancel_before_opd_starts_refunds(client, make_slot, gateway, db):
    started = start_paid_booking(client, make_slot(token_amount=50))
    pay(client, gateway, started, payment_id="pay_abc")
    r = client.post(f"/api/public/bookings/{started['bookingId']}/cancel", json={"code": started["trackingCode"]})
    assert r.status_code == 200
    assert r.json()["refund"] == {"amount": 50, "status": "REFUNDED"}
    assert gateway.refunds == [("pay_abc", 5000)]
    assert status(client, started).json()["payment"]["refundStatus"] == "REFUNDED"


def test_cancel_after_opd_started_is_not_refunded(client, make_slot, gateway, db):
    started = start_paid_booking(client, make_slot(token_amount=50))
    pay(client, gateway, started)
    with db.begin():
        db.execute(text("UPDATE opd_sessions SET status = 'IN_OPD'"))
    r = client.post(f"/api/public/bookings/{started['bookingId']}/cancel", json={"code": started["trackingCode"]})
    assert r.status_code == 200
    assert r.json()["refund"] is None
    assert gateway.refunds == []


def test_failed_refund_is_recorded(client, make_slot, gateway, db):
    started = start_paid_booking(client, make_slot(token_amount=50))
    pay(client, gateway, started)
    gateway.fail_refunds = True
    r = client.post(f"/api/public/bookings/{started['bookingId']}/cancel", json={"code": started["trackingCode"]})
    assert r.status_code == 200  # the cancellation itself still stands
    assert r.json()["refund"]["status"] == "REFUND_FAILED"
    assert db.scalar(text("SELECT status FROM bookings")) == "CANCELLED"


def test_cancel_before_paying_then_late_payment_is_refunded(client, make_slot, gateway, db):
    started = start_paid_booking(client, make_slot(token_amount=50))
    r = client.post(f"/api/public/bookings/{started['bookingId']}/cancel", json={"code": started["trackingCode"]})
    assert r.json() == {"bookingId": started["bookingId"], "status": "CANCELLED", "refund": None}
    assert status(client, started).json()["status"] == "CANCELLED"

    webhook(client, gateway, started["payment"]["orderId"], payment_id="pay_late")
    assert db.scalar(text("SELECT count(*) FROM bookings")) == 0
    assert gateway.refunds == [("pay_late", 5000)]


def test_unpaid_attempts_cannot_change_consent(client, make_slot, gateway, db):
    free = make_slot()
    book(client, free, consent=True)  # patient has consented
    paid = make_slot(token_amount=50)
    start_paid_booking(client, paid, consent=False)  # someone starts a booking but never pays
    assert db.scalar(text("SELECT records_consent_at IS NOT NULL FROM patients"))


def test_pending_status_needs_the_right_code(client, make_slot):
    started = start_paid_booking(client, make_slot(token_amount=50))
    r = client.get(f"/api/public/bookings/{started['bookingId']}", params={"code": "wrong"})
    assert r.status_code == 404


def test_listing_shows_token_amount(client, make_slot):
    make_slot(token_amount=50)
    assert client.get("/api/public/doctors").json()[0]["clinic"]["tokenAmount"] == 50
