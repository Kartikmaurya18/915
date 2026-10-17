"""Password hashing, access-token (JWT) and refresh-token primitives."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from src.auth import settings

ISSUER = "market-data"
AUDIENCE = "market-data-api"
ALGORITHM = "HS256"

_hasher = PasswordHasher()  # argon2id with the library's current defaults
# Verified against when the account does not exist, so "unknown email" and
# "wrong password" take comparable time and cannot be told apart by timing.
_DUMMY_HASH = _hasher.hash("timing-equalisation-placeholder")


class AuthError(Exception):
    """An authentication/authorization failure safe to show the client."""

    def __init__(self, code: str, message: str, status: int = 401, *, retry_after: int | None = None, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after  # seconds; sent as Retry-After on 429s
        self.extra = extra or {}  # additional JSON fields for the client (never secrets)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def create_access_token(user_id: str, role: str, now: datetime | None = None) -> tuple[str, datetime]:
    now = now or datetime.now(timezone.utc)
    expires = now + timedelta(seconds=settings.access_token_ttl_seconds())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": user_id,
            "role": role,
            "typ": "access",
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
            "jti": secrets.token_hex(8),
        },
        settings.jwt_secret(),
        algorithm=ALGORITHM,
    )
    return token, expires


def decode_access_token(token: str) -> dict:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret(),
            algorithms=[ALGORITHM],  # never trust the token's own "alg"
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token_expired", "Access token has expired.") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("invalid_token", "Invalid access token.") from exc
    if claims.get("typ") != "access":
        raise AuthError("invalid_token", "Invalid access token.")
    return claims


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# -- verification token: proves "this person just passed the password check / registered" for the short time
#    they have to finish entering their codes. It cannot be used as an access token (typ differs, and the access
#    decoder insists on typ=access) and it carries no role.

VERIFY_TYP = "verify"


def create_verification_token(user_id: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": user_id,
            "typ": VERIFY_TYP,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=settings.verification_token_ttl_seconds())).timestamp()),
            "jti": secrets.token_hex(8),
        },
        settings.jwt_secret(),
        algorithm=ALGORITHM,
    )


def decode_verification_token(token: str) -> str:
    """The user id the token was issued for, or AuthError."""
    try:
        claims = jwt.decode(
            token or "",
            settings.jwt_secret(),
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("verification_expired", "This verification session has expired. Sign in again to get new codes.") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("invalid_verification_token", "Invalid verification session. Sign in again.") from exc
    if claims.get("typ") != VERIFY_TYP:
        raise AuthError("invalid_verification_token", "Invalid verification session. Sign in again.")
    return claims["sub"]
