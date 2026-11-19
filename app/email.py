"""Sending emails (password reset links).

EMAIL_CHANNEL:
- "log": writes the email to the server log (development, or until a provider is set up).
- "fake": keeps emails in memory (tests).
- "resend": sends with Resend (resend.com, free for 3,000 emails a month). Needs RESEND_API_KEY
  and EMAIL_FROM on a domain verified in Resend, e.g. "Clinically <no-reply@clinically.in>".
"""

import logging

import httpx

from app.config import get_settings

log = logging.getLogger("clinically.email")


class EmailError(Exception):
    pass


sent: list[tuple[str, str, str]] = []  # "fake": (to, subject, text)


def send(to: str, subject: str, text: str) -> None:
    settings = get_settings()
    if settings.email_channel == "fake":
        sent.append((to, subject, text))
    elif settings.email_channel == "resend":
        try:
            r = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={"from": settings.email_from, "to": [to], "subject": subject, "text": text},
                timeout=10,
            )
        except httpx.HTTPError as e:
            raise EmailError(str(e)) from e
        if r.status_code >= 300:
            raise EmailError(f"Resend answered {r.status_code}")
    else:
        log.info("Email to %s — %s\n%s", to, subject, text)
