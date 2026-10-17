"""Sending one-time codes: email over SMTP, SMS over Twilio or a generic webhook, and a development console.

A code is only ever written to a log by the *console* sender, which production refuses to start with (see
settings._validate_delivery). Every other sender logs the channel and a masked destination, never the code or the
provider's response body (which can echo the number back).

Delivery runs after the database transaction commits and its failures never change what the API answers: the code
exists either way and the person can ask for another. Failures are counted in `otp_delivery_total` and logged.
"""

from __future__ import annotations

import base64
import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Callable, Protocol

from prometheus_client import Counter

from src.auth import settings
from src.auth.contact import mask

log = logging.getLogger("market_data.auth.delivery")

TIMEOUT_SECONDS = 15.0

delivery_total = Counter("otp_delivery_total", "One-time-code deliveries", ["channel", "provider", "result"])


class DeliveryError(Exception):
    """A send failed. The message never contains the code, the destination or a provider response body."""


class Sender(Protocol):
    name: str

    def send(self, to: str, subject: str | None, body: str) -> None: ...


class ConsoleSender:
    """Development only: prints the message instead of sending it, so registration can be tried without an
    email or SMS account."""

    def __init__(self, channel: str):
        self.name = "console"
        self.channel = channel

    def send(self, to: str, subject: str | None, body: str) -> None:
        log.warning("DEV ONLY, not delivered (%s -> %s): %s", self.channel, to, body)


class SmtpSender:
    name = "smtp"

    def __init__(self) -> None:
        import os

        self.host = os.environ["SMTP_HOST"]
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.security = os.getenv("SMTP_SECURITY", "starttls").strip().lower()  # starttls | ssl | none
        self.user = os.getenv("SMTP_USER", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.sender = os.environ["EMAIL_FROM"]

    def send(self, to: str, subject: str | None, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject or "Your verification code"
        message.set_content(body)
        context = ssl.create_default_context()
        try:
            if self.security == "ssl":
                server: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=TIMEOUT_SECONDS, context=context)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=TIMEOUT_SECONDS)
            with server as connection:
                if self.security == "starttls":
                    connection.starttls(context=context)
                if self.user:
                    connection.login(self.user, self.password)
                connection.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(f"SMTP send failed ({exc.__class__.__name__})") from None


def _post(url: str, data: bytes, headers: dict[str, str]) -> int:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - operator-configured https URL
            return response.status
    except urllib.error.HTTPError as exc:
        raise DeliveryError(f"provider answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise DeliveryError(f"provider unreachable ({exc.__class__.__name__})") from None


class TwilioSender:
    """Twilio's Messages API: POST /2010-04-01/Accounts/{sid}/Messages.json, form encoded, HTTP basic auth."""

    name = "twilio"
    BASE = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

    def __init__(self) -> None:
        import os

        self.sid = os.environ["TWILIO_ACCOUNT_SID"]
        self.token = os.environ["TWILIO_AUTH_TOKEN"]
        self.sender = os.getenv("TWILIO_FROM", "")
        self.service = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "")
        self.url = os.getenv("TWILIO_API_URL") or self.BASE.format(sid=self.sid)  # overridable for tests

    def send(self, to: str, subject: str | None, body: str) -> None:
        fields = {"To": to, "Body": body}
        if self.service:
            fields["MessagingServiceSid"] = self.service
        else:
            fields["From"] = self.sender
        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        status = _post(
            self.url,
            urllib.parse.urlencode(fields).encode(),
            {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        if not 200 <= status < 300:
            raise DeliveryError(f"provider answered HTTP {status}")


class WebhookSender:
    """Any SMS gateway: POSTs {"to", "message"} as JSON to SMS_WEBHOOK_URL (optionally with a bearer token)."""

    name = "webhook"

    def __init__(self) -> None:
        import os

        self.url = os.environ["SMS_WEBHOOK_URL"]
        self.token = os.getenv("SMS_WEBHOOK_TOKEN", "")

    def send(self, to: str, subject: str | None, body: str) -> None:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        status = _post(self.url, json.dumps({"to": to, "message": body}).encode(), headers)
        if not 200 <= status < 300:
            raise DeliveryError(f"provider answered HTTP {status}")


class Outbox:
    """Test double: remembers what would have been sent."""

    name = "outbox"

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail_next: Exception | None = None

    def send(self, to: str, subject: str | None, body: str) -> None:
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        self.sent.append({"to": to, "subject": subject, "body": body})

    def last_code(self, to: str | None = None) -> str:
        """The digits of the most recent code sent (to `to`, if given)."""
        import re

        for item in reversed(self.sent):
            if to is None or item["to"] == to:
                match = re.search(r"\b(\d{4,8})\b", item["body"])
                if match:
                    return match.group(1)
        raise LookupError("no code sent")


_overrides: dict[str, Sender] = {}


def set_sender(channel: str, sender: Sender | None) -> None:
    """Replace (or, with None, restore) the sender for a channel. Used by tests."""
    if sender is None:
        _overrides.pop(channel, None)
    else:
        _overrides[channel] = sender


def get_sender(channel: str) -> Sender:
    if channel in _overrides:
        return _overrides[channel]
    if channel == "email":
        return SmtpSender() if settings.email_provider() == "smtp" else ConsoleSender("email")
    provider = settings.sms_provider()
    if provider == "twilio":
        return TwilioSender()
    if provider == "webhook":
        return WebhookSender()
    return ConsoleSender("sms")


def deliver(channel: str, destination: str, subject: str | None, body: str, sender: Sender | None = None) -> bool:
    """Send one message. Never raises: returns whether it was accepted by the provider."""
    provider = "unknown"
    try:
        sender = sender or get_sender(channel)
        provider = sender.name
        sender.send(destination, subject, body)
    except Exception as exc:  # delivery must never take down the request that asked for it
        delivery_total.labels(channel, provider, "error").inc()
        log.error("code delivery failed (%s via %s to %s): %s", channel, provider, mask(channel, destination), exc if isinstance(exc, DeliveryError) else exc.__class__.__name__)
        return False
    delivery_total.labels(channel, provider, "ok").inc()
    return True


Deliver = Callable[[str, str, str | None, str], bool]
