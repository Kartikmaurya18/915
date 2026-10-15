"""FastAPI dependencies: who is calling, and are they allowed."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from src.auth import settings
from src.auth.security import AuthError, decode_access_token
from src.db.models import User
from src.db.session import session_scope


@dataclass(frozen=True)
class AuthUser:
    id: str
    role: str
    email: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# Stand-in identity when AUTH_MODE=disabled (local development and the legacy UI).
LOCAL_DEV_USER = AuthUser(id="local-dev", role="admin")


def client_ip(request: Request) -> str:
    hops = settings.trusted_proxy_hops()
    if hops:
        forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return request.client.host if request.client else "unknown"


def _unauthorized(exc: AuthError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail={"code": exc.code, "message": exc.message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def authenticate_token(token: str) -> AuthUser:
    """Validate an access token (used by REST and the WebSocket handshake)."""
    claims = decode_access_token(token)
    return AuthUser(id=claims["sub"], role=claims.get("role", "user"))


def current_user(request: Request) -> AuthUser:
    if not settings.auth_required():
        return LOCAL_DEV_USER
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized(AuthError("not_authenticated", "Authentication required."))
    try:
        return authenticate_token(token)
    except AuthError as exc:
        raise _unauthorized(exc) from exc


def require_admin(user: AuthUser = Depends(current_user)) -> AuthUser:
    """Admin-only. The role is re-read from the database rather than trusted
    from the (up to 15 minute old) token, so a demotion takes effect at once."""
    if not settings.auth_required():
        return user
    with session_scope() as session:
        row = session.scalar(select(User).where(User.id == user.id))
        if row is None or not row.is_active or row.role != "admin":
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Admin access required."})
    return user
