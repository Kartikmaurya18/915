"""Registration, login, refresh-token rotation and logout.

All functions take an open SQLAlchemy Session and do NOT commit, except where
noted: a detected refresh-token replay must persist its family revocation even
though the request then fails, so that path commits before raising.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.auth import settings
from src.auth.security import (
    AuthError,
    create_access_token,
    hash_password,
    hash_refresh_token,
    needs_rehash,
    new_refresh_token,
    verify_password,
)
from src.db.models import AuditLog, RefreshToken, User, _uuid

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128


@dataclass(frozen=True)
class AuthSession:
    """Result of a successful login/refresh."""

    user: User
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise AuthError("invalid_email", "Enter a valid email address.", status=422)
    return email


def validate_password(password: str) -> None:
    if not isinstance(password, str) or not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
        raise AuthError(
            "weak_password",
            f"Password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters long.",
            status=422,
        )


def _email_fingerprint(email: str) -> str:
    """Stable, non-reversible tag for correlating events without storing the address."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]


def audit(session: Session, event: str, user_id: str | None = None, ip: str | None = None, detail: dict | None = None) -> None:
    session.add(AuditLog(user_id=user_id, event=event, ip=ip, detail=detail))


def register(session: Session, email: str, password: str, ip: str | None = None) -> User:
    if not settings.registration_open():
        raise AuthError("registration_closed", "Registration is currently closed.", status=403)
    email = normalize_email(email)
    validate_password(password)
    user = User(email=email, password_hash=hash_password(password), role="user")
    session.add(user)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise AuthError("email_taken", "An account with this email already exists.", status=409) from exc
    audit(session, "register", user.id, ip)
    return user


def _issue(session: Session, user: User, family_id: str, now: datetime) -> AuthSession:
    raw = new_refresh_token()
    expires = now + timedelta(seconds=settings.refresh_token_ttl_seconds())
    session.add(
        RefreshToken(
            user_id=user.id,
            family_id=family_id,
            token_hash=hash_refresh_token(raw),
            created_at=now,
            expires_at=expires,
        )
    )
    access, access_exp = create_access_token(user.id, user.role, now)
    return AuthSession(user, access, access_exp, raw, expires)


def start_session(session: Session, user: User, ip: str | None = None, event: str = "login") -> AuthSession:
    """Open a fresh refresh-token family for an already-verified user."""
    now = datetime.now(timezone.utc)
    user.last_login_at = now
    result = _issue(session, user, _uuid(), now)
    audit(session, event, user.id, ip)
    return result


def login(session: Session, email: str, password: str, ip: str | None = None) -> AuthSession:
    try:
        email = normalize_email(email)
    except AuthError:
        # A malformed address cannot match an account; answer like any failed login.
        raise AuthError("invalid_credentials", "Incorrect email or password.") from None
    user = session.scalar(select(User).where(User.email == email))
    ok = verify_password(user.password_hash if user else None, password or "")
    if not ok or user is None or not user.is_active:
        audit(session, "login_failed", user.id if user else None, ip, {"email": _email_fingerprint(email)})
        session.commit()  # keep the failure record even though the request fails
        raise AuthError("invalid_credentials", "Incorrect email or password.")
    now = datetime.now(timezone.utc)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = now
    result = _issue(session, user, _uuid(), now)
    audit(session, "login", user.id, ip)
    return result


def refresh(session: Session, raw_token: str | None, ip: str | None = None) -> AuthSession:
    if not raw_token:
        raise AuthError("invalid_refresh_token", "Missing refresh token.")
    now = datetime.now(timezone.utc)
    token = session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_token)))
    if token is None:
        raise AuthError("invalid_refresh_token", "Invalid refresh token.")

    # Atomically claim the token: exactly one concurrent caller can flip
    # used_at from NULL. Anyone else is replaying a spent token.
    claimed = session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.id == token.id,
            RefreshToken.used_at.is_(None),
            RefreshToken.revoked_at.is_(None),
        )
        .values(used_at=now)
    ).rowcount
    if claimed != 1:
        _revoke_family(session, token.family_id, now)
        audit(session, "refresh_reuse_detected", token.user_id, ip, {"family": token.family_id})
        session.commit()  # the revocation must survive the failed request
        raise AuthError("refresh_token_reused", "Session is no longer valid. Please sign in again.")

    if token.expires_at <= now:
        raise AuthError("refresh_token_expired", "Session expired. Please sign in again.")
    user = session.get(User, token.user_id)
    if user is None or not user.is_active:
        raise AuthError("invalid_refresh_token", "Invalid refresh token.")
    result = _issue(session, user, token.family_id, now)
    audit(session, "token_refresh", user.id, ip)
    return result


def logout(session: Session, raw_token: str | None, ip: str | None = None) -> None:
    """Revoke the whole session family. Always succeeds (idempotent, no oracle)."""
    if not raw_token:
        return
    token = session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_token)))
    if token is None:
        return
    _revoke_family(session, token.family_id, datetime.now(timezone.utc))
    audit(session, "logout", token.user_id, ip)


def _revoke_family(session: Session, family_id: str, now: datetime) -> None:
    # Lock the family's live tokens in a fixed order first. Concurrent revocations (every loser
    # of a refresh race runs one) otherwise lock the same rows in different orders and deadlock.
    ids = session.scalars(
        select(RefreshToken.id)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .order_by(RefreshToken.id)
        .with_for_update()
    ).all()
    if ids:
        session.execute(update(RefreshToken).where(RefreshToken.id.in_(ids)).values(revoked_at=now))


def set_role(session: Session, email: str, role: str) -> User:
    if role not in ("user", "admin"):
        raise ValueError("role must be 'user' or 'admin'")
    user = session.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None:
        raise LookupError(f"No user with email {email!r}")
    user.role = role
    audit(session, "role_changed", user.id, None, {"role": role})
    return user
