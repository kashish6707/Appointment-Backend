"""Razorpay webhook. Confirms payments even if the patient closed the page before we heard back."""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app import booking_payments, notify
from app.deps import Db
from app.payments import get_gateway

router = APIRouter(prefix="/api/payments")
log = logging.getLogger("clinically.payments")

HANDLED_EVENTS = ("payment.captured", "order.paid")


@router.post("/razorpay/webhook")
async def razorpay_webhook(request: Request, db: Db):
    body = await request.body()  # the signature covers the raw bytes
    if not get_gateway().verify_webhook(body, request.headers.get("x-razorpay-signature", "")):
        raise HTTPException(400, "Invalid signature.")
    try:
        event = json.loads(body)
        kind = event.get("event")
        entity = event["payload"]["payment"]["entity"] if kind in HANDLED_EVENTS else None
        order_id, payment_id = (entity["order_id"], entity["id"]) if entity else (None, None)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Unexpected webhook body.") from None
    if order_id is None:
        return {"status": "ignored"}
    # The database work is blocking, so keep it off the event loop.
    return await run_in_threadpool(_confirm, db, order_id, payment_id)


def _confirm(db: Db, order_id: str, payment_id: str) -> dict:
    payment = booking_payments.lock_by_order(db, order_id)
    if payment is None:
        log.warning("Webhook for unknown order %s", order_id)
        return {"status": "ignored"}  # 2xx, so Razorpay doesn't keep retrying

    already_paid = payment.status == "PAID"
    booking = booking_payments.confirm(db, payment, payment_id)
    if booking is None:
        booking_payments.refund(db, payment)
    elif not already_paid:
        notify.booking_and_queue(booking, payment.doctor_clinic.doctor_id, "booking.created")
    return {"status": "ok"}
