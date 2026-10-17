"""Phone numbers and how to show contact details without exposing them."""

from __future__ import annotations

import re

from src.auth.security import AuthError

_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
_SEPARATORS = re.compile(r"[\s\-().]")


def normalize_phone(raw: str) -> str:
    """A mobile number in E.164 ("+919876543210").

    Spaces, dashes and brackets are tolerated; the leading "+" and country code are required. Guessing a country
    here would send a stranger a code, so the web form is what offers a default country, visibly."""
    value = _SEPARATORS.sub("", (raw or "").strip())
    if not _E164.match(value):
        raise AuthError("invalid_phone", "Enter your mobile number with the country code, for example +91 98765 43210.", status=422)
    return value


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"  # a fixed mask: one star per hidden letter would reveal the length


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}{'*' * max(0, len(phone) - 6)}{phone[-3:]}" if len(phone) > 6 else "***"


def mask(channel: str, destination: str) -> str:
    return mask_email(destination) if channel == "email" else mask_phone(destination)
