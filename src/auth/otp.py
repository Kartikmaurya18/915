"""One-time codes: issue, send-limits, and constant-time checking.

Security properties, each pinned by a test:
  * a code is random (secrets), numeric, and stored only as an HMAC keyed from the server secret, bound to its
    challenge, purpose and destination (a code for one phone cannot be replayed against another challenge)
  * at most `OTP_MAX_ATTEMPTS` guesses per code; the counter is advanced atomically BEFORE the guess is compared,
    so any number of parallel guesses together still stop at the limit
  * a code works once, for `OTP_TTL_MINUTES`; sending a new one retires the old one
  * resend cooldown and an hourly cap per (account, channel, purpose), counted in the database so they hold
    across every gateway replica
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.auth import settings
from src.auth.security import AuthError
from src.db.models import OtpChallenge

PURPOSE_VERIFY = "verify_contact"
PURPOSE_RESET = "reset_password"


@dataclass(frozen=True)
class Issued:
    channel: str
    destination: str
    code: str
    expires_at: datetime
    purpose: str


def _key() -> bytes:
    return hashlib.sha256(b"market-data|otp|v1|" + settings.jwt_secret().encode("utf-8")).digest()


def _digest(challenge_id: str, purpose: str, destination: str, code: str) -> str:
    return hmac.new(_key(), f"{challenge_id}|{purpose}|{destination}|{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def new_code() -> str:
    n = settings.otp_length()
    return f"{secrets.randbelow(10**n):0{n}d}"


def message_for(purpose: str, code: str) -> tuple[str, str]:
    """(subject, body). Plain text, no links: there is nothing to click, so nothing to phish."""
    minutes = max(1, settings.otp_ttl_seconds() // 60)
    what = "password reset" if purpose == PURPOSE_RESET else "verification"
    subject = f"Your Market Data {what} code"
    body = (
        f"Your Market Data {what} code is {code}. It expires in {minutes} minutes. "
        "Never share this code: we will never ask for it."
    )
    return subject, body


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def issue(session: Session, user_id: str, purpose: str, channel: str, destination: str, now: datetime | None = None) -> Issued:
    """Create a fresh code for (user, purpose, channel), retiring any earlier one. Enforces the send limits."""
    now = now or datetime.now(timezone.utc)
    recent = session.scalars(
        select(OtpChallenge.created_at)
        .where(
            OtpChallenge.user_id == user_id,
            OtpChallenge.purpose == purpose,
            OtpChallenge.channel == channel,
            OtpChallenge.created_at > now - timedelta(hours=1),
        )
        .order_by(OtpChallenge.created_at.desc())
    ).all()
    if recent:
        wait = settings.otp_resend_seconds() - (now - _utc(recent[0])).total_seconds()
        if wait > 0:
            raise AuthError(
                "otp_cooldown",
                f"Please wait {int(wait) + 1} seconds before asking for another code.",
                status=429,
                retry_after=int(wait) + 1,
                extra={"retry_after": int(wait) + 1},
            )
        if len(recent) >= settings.otp_max_sends_per_hour():
            raise AuthError(
                "otp_limit",
                "Too many codes were requested. Try again in a while.",
                status=429,
                retry_after=3600,
                extra={"retry_after": 3600},
            )
    session.execute(
        update(OtpChallenge)
        .where(
            OtpChallenge.user_id == user_id,
            OtpChallenge.purpose == purpose,
            OtpChallenge.channel == channel,
            OtpChallenge.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    code = new_code()
    challenge = OtpChallenge(
        user_id=user_id,
        purpose=purpose,
        channel=channel,
        destination=destination,
        code_hash="",
        created_at=now,
        expires_at=now + timedelta(seconds=settings.otp_ttl_seconds()),
        attempts=0,
    )
    session.add(challenge)
    session.flush()  # assigns the id the digest is bound to
    challenge.code_hash = _digest(challenge.id, purpose, destination, code)
    return Issued(channel, destination, code, challenge.expires_at, purpose)


def has_live_challenge(session: Session, user_id: str, purpose: str, channel: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return (
        session.scalar(
            select(func.count())
            .select_from(OtpChallenge)
            .where(
                OtpChallenge.user_id == user_id,
                OtpChallenge.purpose == purpose,
                OtpChallenge.channel == channel,
                OtpChallenge.consumed_at.is_(None),
                OtpChallenge.expires_at > now,
                OtpChallenge.attempts < settings.otp_max_attempts(),
            )
        )
        or 0
    ) > 0


def check(session: Session, user_id: str, purpose: str, channel: str, code: str, now: datetime | None = None) -> str:
    """Consume the live code for (user, purpose, channel) if `code` matches; returns the destination it was sent to.

    Raises AuthError otherwise. A wrong guess commits its attempt before raising, so the failure persists."""
    now = now or datetime.now(timezone.utc)
    code = (code or "").strip().replace(" ", "")
    challenge = session.scalar(
        select(OtpChallenge)
        .where(
            OtpChallenge.user_id == user_id,
            OtpChallenge.purpose == purpose,
            OtpChallenge.channel == channel,
            OtpChallenge.consumed_at.is_(None),
        )
        .order_by(OtpChallenge.created_at.desc())
        .limit(1)
    )
    if challenge is None:
        raise AuthError("no_active_code", "There is no active code. Ask for a new one.", status=400)
    if _utc(challenge.expires_at) <= now:
        raise AuthError("code_expired", "That code has expired. Ask for a new one.", status=400)

    # Claim an attempt atomically before looking at the guess: parallel requests cannot exceed the limit.
    claimed = session.execute(
        update(OtpChallenge)
        .where(
            OtpChallenge.id == challenge.id,
            OtpChallenge.consumed_at.is_(None),
            OtpChallenge.attempts < settings.otp_max_attempts(),
        )
        .values(attempts=OtpChallenge.attempts + 1)
    ).rowcount
    if claimed != 1:
        session.commit()
        raise AuthError("too_many_attempts", "Too many wrong codes. Ask for a new code.", status=429, extra={"retry_after": 0})
    session.refresh(challenge)
    expected = _digest(challenge.id, purpose, challenge.destination, code)
    if not (code.isdigit() and hmac.compare_digest(expected, challenge.code_hash)):
        left = settings.otp_max_attempts() - challenge.attempts
        if left <= 0:
            challenge.consumed_at = now  # burned: a new code is needed
        session.commit()  # the wrong guess must be remembered even though the request fails
        message = "That code is not right." + (f" {left} attempt{'s' if left != 1 else ''} left." if left > 0 else " Ask for a new code.")
        raise AuthError("invalid_code", message, status=400, extra={"attempts_left": max(0, left)})

    used = session.execute(
        update(OtpChallenge).where(OtpChallenge.id == challenge.id, OtpChallenge.consumed_at.is_(None)).values(consumed_at=now)
    ).rowcount
    if used != 1:  # a concurrent request spent it first
        raise AuthError("no_active_code", "There is no active code. Ask for a new one.", status=400)
    return challenge.destination
