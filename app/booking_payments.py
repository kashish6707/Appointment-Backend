"""Online token-amount payments: start, confirm (checkout or webhook), cancel and refund.

A booking row (and its token) is created only once a payment is confirmed. Confirmation can arrive
from the browser and from the Razorpay webhook at the same time; both lock the payment row, so
only one creates the booking and the other sees it already done.
"""

import hmac
import logging
import uuid
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app import bookings, security
from app.models import Booking, DoctorClinic, Payment
from app.payments import GatewayError, get_gateway
from app.timeutil import now_ist

log = logging.getLogger("clinically.payments")

PAYMENT_EXPIRES_AFTER = timedelta(minutes=15)


def start(
    db: DbSession, dc: DoctorClinic, day: date, visit_type: str, patient: dict, records_consent: bool
) -> tuple[Payment, str]:
    """Create the gateway order and a pending payment. Returns it and the patient's tracking code."""
    gateway = get_gateway()
    booking_id = uuid.uuid4()
    amount_paise = dc.token_amount * 100
    try:
        order_id = gateway.create_order(amount_paise, receipt=str(booking_id))
    except GatewayError as e:
        log.error("Could not create payment order: %s", e)
        raise HTTPException(502, "We couldn't start the payment. Please try again.") from None
    code = security.new_tracking_code()
    payment = Payment(
        booking_id=booking_id,
        doctor_clinic_id=dc.id,
        date=day,
        visit_type=visit_type,
        request={"patient": patient, "recordsConsent": records_consent},
        tracking_code_hash=security.hash_token(code),
        amount_paise=amount_paise,
        gateway=gateway.name,
        gateway_order_id=order_id,
    )
    db.add(payment)
    db.commit()
    return payment, code


def checkout_json(payment: Payment) -> dict:
    """What the frontend passes to Razorpay Checkout."""
    return {
        "orderId": payment.gateway_order_id,
        "amount": payment.amount_paise,  # paise, as Razorpay Checkout expects
        "currency": payment.currency,
        "keyId": get_gateway().key_id,
    }


def lock_by_order(db: DbSession, order_id: str) -> Payment | None:
    return db.scalar(select(Payment).where(Payment.gateway_order_id == order_id).with_for_update())


def lock_by_booking(db: DbSession, booking_id: uuid.UUID, code: str) -> Payment | None:
    payment = db.scalar(select(Payment).where(Payment.booking_id == booking_id).with_for_update())
    if payment is None or not hmac.compare_digest(payment.tracking_code_hash, security.hash_token(code)):
        return None
    return payment


def confirm(db: DbSession, payment: Payment, gateway_payment_id: str) -> Booking | None:
    """Mark a locked payment paid and create its booking. Commits.

    Returns the booking, or None if it couldn't be booked (the payment is then refunded).
    Safe to call again for the same payment.
    """
    if payment.status == "PAID":
        return db.get(Booking, payment.booking_id)
    if payment.status not in ("CREATED", "CANCELLED"):
        return None  # already being refunded

    payment.gateway_payment_id = gateway_payment_id
    payment.paid_at = now_ist()
    if payment.status == "CANCELLED":
        return _refund_later(db, payment, "Paid after the patient cancelled")

    details = payment.request["patient"]
    try:
        with db.begin_nested():
            session = bookings.lock_session(db, payment.doctor_clinic_id, payment.date)
            patient = bookings.find_or_create_patient(
                db,
                name=details["name"],
                phone=details["phone"],
                age=details["age"],
                gender=details["gender"],
                height_cm=details.get("heightCm"),
                weight_kg=details.get("weightKg"),
                records_consent=payment.request["recordsConsent"],
            )
            booking, _ = bookings.add_booking(
                db,
                session,
                patient,
                visit_type=payment.visit_type,
                source="ONLINE",
                booking_id=payment.booking_id,
                tracking_code_hash=payment.tracking_code_hash,
            )
    except HTTPException as e:  # closed meanwhile, or already holds a token
        return _refund_later(db, payment, e.detail)

    payment.status = "PAID"
    payment.request = None  # the details now live on the patient row
    db.commit()
    return booking


def _refund_later(db: DbSession, payment: Payment, reason: str) -> None:
    payment.status = "REFUND_PENDING"
    payment.note = reason
    db.commit()
    return None


def mark_for_refund(payment: Payment, reason: str) -> None:
    payment.status = "REFUND_PENDING"
    payment.note = reason


def refund(db: DbSession, payment: Payment) -> None:
    """Send a committed REFUND_PENDING payment's refund to the gateway, then record the outcome."""
    if payment.status != "REFUND_PENDING" or not payment.gateway_payment_id:
        return
    try:
        payment.refund_id = get_gateway().refund(payment.gateway_payment_id, payment.amount_paise)
        payment.status = "REFUNDED"
        payment.refunded_at = now_ist()
    except GatewayError as e:
        log.error("Refund failed for payment %s: %s", payment.id, e)
        payment.status = "REFUND_FAILED"
        payment.note = f"{payment.note or ''} | refund failed: {e}"[:500]
    db.commit()


def pending_status(payment: Payment) -> str:
    if payment.status == "CREATED":
        expired = now_ist() - payment.created_at > PAYMENT_EXPIRES_AFTER
        return "PAYMENT_EXPIRED" if expired else "PENDING_PAYMENT"
    if payment.status == "CANCELLED":
        return "CANCELLED"
    return "PAYMENT_REFUNDED"  # paid, but couldn't be booked


def summary(payment: Payment | None, fee: int) -> dict:
    """Shown with a booking: what was paid online and what's left for reception."""
    paid = payment.amount_paise // 100 if payment and payment.paid_at else 0
    return {
        "fee": fee,
        "paidOnline": paid,
        "payAtReception": max(fee - paid, 0),
        "refundStatus": payment.status if payment and payment.status.startswith("REFUND") else None,
    }
