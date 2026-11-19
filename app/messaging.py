"""Senders: how a message reaches a patient's phone.

NOTIFY_CHANNEL picks one:
- "log": writes the message to the server log (development, or before WhatsApp is set up).
- "fake": keeps messages in memory (tests).
- "whatsapp": the WhatsApp Cloud API (Meta). Needs WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID,
  and the templates in docs/whatsapp-templates.md approved in WhatsApp Manager.
"""

import logging
from functools import lru_cache
from typing import Protocol

import httpx

from app.config import get_settings

log = logging.getLogger("clinically.messages")


class SendError(Exception):
    """The provider refused or couldn't be reached. The message is retried later."""


class PermanentSendError(SendError):
    """Retrying won't help (e.g. the number isn't on WhatsApp, or the template isn't approved)."""


class Message(Protocol):
    phone: str
    body: str
    template: str | None
    params: list
    lang: str


class Sender(Protocol):
    name: str

    def send(self, m: Message) -> str | None:
        """Send to a 10-digit Indian mobile number. Returns the provider's message id."""


class LogSender:
    """Development: writes the message to the server log instead of sending it."""

    name = "log"

    def send(self, m: Message) -> str | None:
        log.info("WhatsApp to +91 %s (%s, %s): %s", m.phone, m.template, m.lang, m.body)
        return None


class FakeSender:
    """Tests: keeps messages in memory, and can be told to fail."""

    name = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.messages: list[Message] = []
        self.fail = False

    def send(self, m: Message) -> str | None:
        if self.fail:
            raise SendError("Provider unavailable (test)")
        self.sent.append((m.phone, m.body))
        self.messages.append(m)
        return f"fake-{len(self.sent)}"


# Meta error codes that won't go away by retrying.
# https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
PERMANENT_CODES = {
    131026,  # recipient can't receive (not on WhatsApp, or old app)
    131051,  # unsupported message type
    132000, 132001, 132005, 132007, 132012, 132015, 132016,  # template missing, paused or parameters wrong
    131009,  # parameter value not valid
    100,  # invalid parameter
}


class WhatsAppSender:
    """Template messages through the WhatsApp Cloud API."""

    name = "whatsapp"
    API = "https://graph.facebook.com/v21.0"

    def __init__(self, token: str, phone_number_id: str) -> None:
        self.token = token
        self.phone_number_id = phone_number_id

    def send(self, m: Message) -> str | None:
        if not m.template:
            raise PermanentSendError("No WhatsApp template for this message")
        payload = {
            "messaging_product": "whatsapp",
            "to": f"91{m.phone}",
            "type": "template",
            "template": {
                "name": m.template,
                "language": {"code": "hi" if m.lang == "hi" else "en"},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in m.params]}],
            },
        }
        try:
            r = httpx.post(
                f"{self.API}/{self.phone_number_id}/messages",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
                timeout=15,
            )
        except httpx.HTTPError as e:
            raise SendError(f"WhatsApp unreachable: {e}") from e
        if r.status_code < 300:
            try:
                return r.json()["messages"][0]["id"]
            except (KeyError, IndexError, ValueError):
                return None
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        code = err.get("code")
        detail = f"WhatsApp {r.status_code} (code {code}): {err.get('message', r.text[:200])}"
        if code in PERMANENT_CODES or r.status_code in (400, 404) and code not in (4, 80007, 130429, 131048, 131056):
            raise PermanentSendError(detail)
        raise SendError(detail)  # rate limits, Meta outages: retried with back-off


_fake = FakeSender()


@lru_cache
def _log_sender() -> LogSender:
    return LogSender()


@lru_cache
def _whatsapp(token: str, phone_number_id: str) -> WhatsAppSender:
    return WhatsAppSender(token, phone_number_id)


def get_sender() -> Sender:
    s = get_settings()
    if s.notify_channel == "fake":
        return _fake
    if s.notify_channel == "whatsapp":
        if not s.whatsapp_token or not s.whatsapp_phone_number_id:
            log.error("NOTIFY_CHANNEL=whatsapp but WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID are missing; logging instead")
            return _log_sender()
        return _whatsapp(s.whatsapp_token, s.whatsapp_phone_number_id)
    return _log_sender()
