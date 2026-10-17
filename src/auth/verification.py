"""Account verification and password reset by one-time code (email and SMS).

These functions take an open Session, do not send anything and do not commit (except where noted: a wrong code
must persist its attempt). They return the codes that were issued; the route sends them AFTER the transaction
commits, so a delivery problem can never roll back or delay account state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.auth import otp, service, settings
from src.auth.contact import mask, normalize_phone
from src.auth.otp import PURPOSE_RESET, PURPOSE_VERIFY, Issued
from src.auth.security import AuthError, hash_password
from src.db.models import RefreshToken, User, _uuid


@dataclass(frozen=True)
class Pending:
    """What the person still has to do, safe to show them."""

    user: User
    channels: list[str]
    issued: list[Issued]

    def destinations(self) -> dict[str, str]:
        return {c: mask(c, self.user.email if c == "email" else self.user.phone or "") for c in self.channels}


def _destination(user: User, channel: str) -> str:
    return user.email if channel == "email" else (user.phone or "")


def _issue_pending(session: Session, user: User, now: datetime | None = None, *, only_if_missing: bool = False) -> list[Issued]:
    issued: list[Issued] = []
    for channel in service.pending_channels(user):
        if only_if_missing and otp.has_live_challenge(session, user.id, PURPOSE_VERIFY, channel, now):
            continue
        issued.append(otp.issue(session, user.id, PURPOSE_VERIFY, channel, _destination(user, channel), now))
    return issued


# -- registration ---------------------------------------------------------------------------------------------


def register(session: Session, email: str, phone: str | None, password: str, ip: str | None = None) -> Pending:
    """Create (or take over an abandoned, never-verified) account and issue codes for each channel it must verify."""
    if not settings.registration_open():
        raise AuthError("registration_closed", "Registration is currently closed.", status=403)
    email = service.normalize_email(email)
    service.validate_password(password)
    channels = settings.required_channels()
    number = normalize_phone(phone) if (phone or "phone" in channels) else None

    user = session.scalar(select(User).where(User.email == email))
    if user is not None:
        if not service.pending_channels(user):
            raise AuthError("email_taken", "An account with this email already exists.", status=409)
        # An earlier sign-up that was never completed. Nothing about it is reachable without verification, so a
        # new attempt replaces its password and number (this is how someone who mistyped their number recovers).
        user.password_hash = hash_password(password)
    else:
        user = User(id=_uuid(), email=email, password_hash=hash_password(password), role="user")
        session.add(user)

    if number is not None:
        holder = session.scalar(select(User).where(User.phone == number, User.id != user.id))
        if holder is not None:
            if holder.phone_verified_at is not None:
                raise AuthError("phone_taken", "An account with this mobile number already exists.", status=409)
            holder.phone = None  # an abandoned claim on the number
            session.flush()
        if user.phone != number:
            user.phone = number
            user.phone_verified_at = None
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise AuthError("email_taken", "An account with this email or mobile number already exists.", status=409) from exc

    pending = service.pending_channels(user)
    service.audit(session, "register", user.id, ip, {"verify": pending})
    return Pending(user, pending, _issue_pending(session, user))


# -- verifying ------------------------------------------------------------------------------------------------


def _user(session: Session, user_id: str) -> User:
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthError("invalid_verification_token", "Invalid verification session. Sign in again.")
    return user


def verify(session: Session, user_id: str, channel: str, code: str, ip: str | None = None) -> tuple[User, list[str]]:
    """Check a code for one channel. Returns the user and the channels still pending afterwards."""
    user = _user(session, user_id)
    if channel not in service.pending_channels(user):
        raise AuthError("nothing_to_verify", f"Your {channel} is already verified or not required.", status=400)
    try:
        otp.check(session, user.id, PURPOSE_VERIFY, channel, code)
    except AuthError as exc:
        service.audit(session, "otp_failed", user.id, ip, {"channel": channel, "code": exc.code})
        session.commit()
        raise
    now = datetime.now(timezone.utc)
    if channel == "email":
        user.email_verified_at = now
    else:
        user.phone_verified_at = now
    service.audit(session, "contact_verified", user.id, ip, {"channel": channel})
    return user, service.pending_channels(user)


def resend(session: Session, user_id: str, channel: str, ip: str | None = None) -> Issued:
    user = _user(session, user_id)
    if channel not in service.pending_channels(user):
        raise AuthError("nothing_to_verify", f"Your {channel} is already verified or not required.", status=400)
    issued = otp.issue(session, user.id, PURPOSE_VERIFY, channel, _destination(user, channel))
    service.audit(session, "otp_resent", user.id, ip, {"channel": channel})
    return issued


def prepare_login(session: Session, user_id: str) -> Pending:
    """Someone with the right password but unverified contacts signed in: make sure codes exist for what is missing
    (an unexpired one is kept; a cooldown is not an error here, the code they already have still works)."""
    user = _user(session, user_id)
    issued: list[Issued] = []
    for channel in service.pending_channels(user):
        if otp.has_live_challenge(session, user.id, PURPOSE_VERIFY, channel):
            continue
        try:
            issued.append(otp.issue(session, user.id, PURPOSE_VERIFY, channel, _destination(user, channel)))
        except AuthError:
            continue
    return Pending(user, service.pending_channels(user), issued)


# -- forgotten password ---------------------------------------------------------------------------------------


def request_reset(session: Session, email: str, channel: str) -> Issued | None:
    """Send a reset code to a channel the account has already verified. Returns None, silently, when there is
    nothing to send: the route answers identically either way, so it cannot be used to discover accounts."""
    try:
        email = service.normalize_email(email)
    except AuthError:
        return None
    user = session.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        return None
    if channel == "email":
        if user.email_verified_at is None:
            return None
    elif channel == "phone":
        if user.phone is None or user.phone_verified_at is None:
            return None
    else:
        return None
    try:
        return otp.issue(session, user.id, PURPOSE_RESET, channel, _destination(user, channel))
    except AuthError:
        return None  # rate limited: say nothing different


def reset_password(session: Session, email: str, channel: str, code: str, new_password: str, ip: str | None = None) -> None:
    """Set a new password after a correct code, and sign the account out everywhere."""
    service.validate_password(new_password)
    generic = AuthError("invalid_code", "That code is not right or has expired. Ask for a new one.", status=400)
    try:
        email = service.normalize_email(email)
    except AuthError:
        raise generic from None
    user = session.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or channel not in ("email", "phone"):
        raise generic
    try:
        otp.check(session, user.id, PURPOSE_RESET, channel, code)
    except AuthError as exc:
        service.audit(session, "otp_failed", user.id, ip, {"channel": channel, "code": exc.code, "purpose": "reset"})
        session.commit()
        raise
    user.password_hash = hash_password(new_password)
    now = datetime.now(timezone.utc)
    session.execute(update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)).values(revoked_at=now))
    service.audit(session, "password_reset", user.id, ip, {"channel": channel})
