"""Turning a verified provider identity into one of our accounts and a session.

The rules, in order:

1. **Known identity** (this provider + this `sub`): sign that account in. The key is the provider's stable subject,
   never the email, because an email address can change hands and a subject cannot.
2. **Unknown identity, email already has an account**: link it, BUT only because the provider vouched that the email is
   verified. If that account's email was never verified here, the person who created it never proved it was theirs
   (this is how "pre-hijacking" works: an attacker registers a victim's address with their own password, waiting for the
   victim to sign in with Google). So its password is destroyed and its sessions revoked before linking.
3. **Unknown identity, new email**: create an account with the verified email, no password (a random unusable one;
   "forgot password" can set one) and no phone number: social sign-in is not asked for a number.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.auth import service, settings
from src.auth.oauth import OAuthError, ProviderIdentity
from src.auth.security import hash_password
from src.db.models import OAuthIdentity, RefreshToken, User, _uuid


def _unusable_password() -> str:
    return hash_password(secrets.token_urlsafe(48))  # nobody knows it, so password sign-in cannot succeed


def sign_in(session: Session, who: ProviderIdentity, ip: str | None = None) -> service.AuthSession:
    now = datetime.now(timezone.utc)
    identity = session.scalar(select(OAuthIdentity).where(OAuthIdentity.provider == who.provider, OAuthIdentity.subject == who.subject))

    if identity is not None:
        user = session.get(User, identity.user_id)
        if user is None or not user.is_active:
            raise OAuthError("account_disabled", "linked account is inactive")
        identity.last_login_at = now
        return service.start_session(session, user, ip, event="oauth_login")

    user = session.scalar(select(User).where(User.email == who.email))
    if user is not None:
        if not user.is_active:
            raise OAuthError("account_disabled", "account with this email is inactive")
        if user.email_verified_at is None:
            # Nobody has proved this address is theirs here. Whoever set its password may not be the owner.
            user.password_hash = _unusable_password()
            session.execute(update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)).values(revoked_at=now))
            service.audit(session, "oauth_unverified_account_reclaimed", user.id, ip, {"provider": who.provider})
            user.email_verified_at = now
        event = "oauth_link"
    else:
        if not settings.registration_open():
            raise OAuthError("registration_closed", "registration is closed")
        user = User(id=_uuid(), email=who.email, password_hash=_unusable_password(), role="user", email_verified_at=now)
        session.add(user)
        session.flush()  # the user row must exist before the identity that points at it (no ORM relationship orders them)
        event = "oauth_register"

    if user.phone is not None and user.phone_verified_at is None:
        user.phone = None  # a number typed into an unfinished sign-up: social sign-in does not require one, so do not hold it
    session.add(OAuthIdentity(user_id=user.id, provider=who.provider, subject=who.subject, email=who.email, last_login_at=now))
    session.flush()
    return service.start_session(session, user, ip, event=event)
