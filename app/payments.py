"""Payment gateways. Razorpay in production; a fake one (same signature scheme) for dev and tests."""

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.config import get_settings

log = logging.getLogger("clinically.payments")


class GatewayError(Exception):
    """The gateway refused the request or couldn't be reached."""


class Gateway(Protocol):
    name: str
    key_id: str

    def create_order(self, amount_paise: int, receipt: str) -> str: ...
    def verify_checkout(self, order_id: str, payment_id: str, signature: str) -> bool: ...
    def verify_webhook(self, body: bytes, signature: str) -> bool: ...
    def refund(self, payment_id: str, amount_paise: int) -> str: ...


def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _matches(secret: str, message: bytes, signature: str) -> bool:
    return bool(secret) and hmac.compare_digest(_hmac_hex(secret, message), signature or "")


class Razorpay:
    name = "razorpay"
    API = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str):
        self.key_id = key_id
        self._secret = key_secret
        self._webhook_secret = webhook_secret

    def _post(self, path: str, payload: dict) -> dict:
        if not (self.key_id and self._secret):
            raise GatewayError("Razorpay keys are not configured")
        try:
            r = httpx.post(f"{self.API}{path}", json=payload, auth=(self.key_id, self._secret), timeout=15)
        except httpx.HTTPError as e:
            raise GatewayError(f"Razorpay unreachable: {type(e).__name__}") from None
        if r.status_code >= 400:
            # Razorpay error bodies describe the request, never card or UPI details.
            raise GatewayError(f"Razorpay {path} returned {r.status_code}: {r.text[:300]}")
        return r.json()

    def create_order(self, amount_paise: int, receipt: str) -> str:
        return self._post("/orders", {"amount": amount_paise, "currency": "INR", "receipt": receipt[:40]})["id"]

    def verify_checkout(self, order_id: str, payment_id: str, signature: str) -> bool:
        # https://razorpay.com/docs/payments/server-integration/python/payment-gateway/build-integration/#verify-payment-signature
        return _matches(self._secret, f"{order_id}|{payment_id}".encode(), signature)

    def verify_webhook(self, body: bytes, signature: str) -> bool:
        return _matches(self._webhook_secret, body, signature)

    def refund(self, payment_id: str, amount_paise: int) -> str:
        return self._post(f"/payments/{payment_id}/refund", {"amount": amount_paise})["id"]


@dataclass
class FakeGateway:
    """Behaves like Razorpay without the network. Tests sign with FAKE_SECRET."""

    name: str = "fake"
    key_id: str = "rzp_test_fake"
    secret: str = "fake_secret"
    fail_refunds: bool = False
    refunds: list[tuple[str, int]] = field(default_factory=list)

    def create_order(self, amount_paise: int, receipt: str) -> str:
        return f"order_fake{secrets.token_hex(7)}"

    def sign_checkout(self, order_id: str, payment_id: str) -> str:
        return _hmac_hex(self.secret, f"{order_id}|{payment_id}".encode())

    def sign_webhook(self, body: bytes) -> str:
        return _hmac_hex(self.secret, body)

    def verify_checkout(self, order_id: str, payment_id: str, signature: str) -> bool:
        return _matches(self.secret, f"{order_id}|{payment_id}".encode(), signature)

    def verify_webhook(self, body: bytes, signature: str) -> bool:
        return _matches(self.secret, body, signature)

    def refund(self, payment_id: str, amount_paise: int) -> str:
        if self.fail_refunds:
            raise GatewayError("fake refund failure")
        self.refunds.append((payment_id, amount_paise))
        return f"rfnd_fake{secrets.token_hex(7)}"


_fake = FakeGateway()


def get_gateway() -> Gateway:
    s = get_settings()
    if s.payment_gateway == "fake":
        return _fake
    return Razorpay(s.razorpay_key_id, s.razorpay_key_secret, s.razorpay_webhook_secret)
