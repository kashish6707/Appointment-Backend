"""Senders: how a text message reaches a patient's phone.

Only development senders exist so far. To go live, add a class here with the same `send` method
(e.g. MSG91 SMS or the WhatsApp Cloud API), give it a name in Settings.notify_channel, and add
its keys to the settings. Nothing else in the app changes.
"""

import logging
from functools import lru_cache
from typing import Protocol

from app.config import get_settings

log = logging.getLogger("clinically.messages")


class SendError(Exception):
    """The provider refused or couldn't be reached. The message is retried later."""


class Sender(Protocol):
    name: str

    def send(self, phone: str, text: str) -> str | None:
        """Send `text` to a 10-digit Indian mobile number. Returns the provider's message id."""


class LogSender:
    """Development: writes the message to the server log instead of sending it."""

    name = "log"

    def send(self, phone: str, text: str) -> str | None:
        log.info("Message to +91 %s: %s", phone, text)
        return None


class FakeSender:
    """Tests: keeps messages in memory, and can be told to fail."""

    name = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    def send(self, phone: str, text: str) -> str | None:
        if self.fail:
            raise SendError("Provider unavailable (test)")
        self.sent.append((phone, text))
        return f"fake-{len(self.sent)}"


_fake = FakeSender()


@lru_cache
def _log_sender() -> LogSender:
    return LogSender()


def get_sender() -> Sender:
    channel = get_settings().notify_channel
    return _fake if channel == "fake" else _log_sender()
