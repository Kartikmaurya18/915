"""/api/auth/* - registration, login, token refresh, logout.

Access tokens are short-lived and returned in the response body (the SPA keeps
them in memory only). The refresh token never reaches JavaScript: it lives in an
HttpOnly, SameSite=Lax cookie scoped to /api/auth, and refresh/logout also require
a custom request header, which forces a CORS preflight and so blocks cross-site
form posts (CSRF). All routes 404 when AUTH_MODE=disabled.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from src.auth import service, settings
from src.auth.deps import AuthUser, client_ip, current_user
from src.auth.ratelimit import SlidingWindowLimiter
from src.db.models import User
from src.db.session import run_in_transaction, session_scope

REFRESH_COOKIE = "refresh_token"
COOKIE_PATH = "/api/auth"

login_by_ip = SlidingWindowLimiter(max_events=20, window_seconds=300)
login_by_account = SlidingWindowLimiter(max_events=5, window_seconds=300)
register_by_ip = SlidingWindowLimiter(max_events=10, window_seconds=3600)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _require_enabled() -> None:
    if not settings.auth_required():
        raise HTTPException(status_code=404, detail="Not found")


def _require_csrf_header(request: Request) -> None:
    if not request.headers.get("x-requested-with"):
        raise HTTPException(status_code=403, detail={"code": "csrf", "message": "Missing X-Requested-With header."})


def _too_many(limiter: SlidingWindowLimiter) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={"code": "rate_limited", "message": "Too many attempts. Try again later."},
        headers={"Retry-After": str(int(limiter.window))},
    )


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


def _user_dict(user: User) -> dict:
    return {"id": user.id, "email": user.email, "role": user.role}


def _respond(response: Response, result: service.AuthSession) -> dict:
    response.set_cookie(
        REFRESH_COOKIE,
        result.refresh_token,
        max_age=settings.refresh_token_ttl_seconds(),
        httponly=True,
        secure=settings.cookie_secure(),
        samesite="lax",
        path=COOKIE_PATH,
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "access_token": result.access_token,
        "token_type": "bearer",
        "expires_at": result.access_expires_at.isoformat(),
        "user": _user_dict(result.user),
    }


@router.post("/register", status_code=201)
def register(body: Credentials, request: Request, response: Response) -> dict:
    _require_enabled()
    ip = client_ip(request)
    if not register_by_ip.hit(ip):
        raise _too_many(register_by_ip)
    def work(session):
        user = service.register(session, body.email, body.password, ip)
        return service.start_session(session, user, ip, event="login")

    return _respond(response, run_in_transaction(work))


@router.post("/login")
def login(body: Credentials, request: Request, response: Response) -> dict:
    _require_enabled()
    ip = client_ip(request)
    account = body.email.strip().lower()
    if login_by_ip.is_blocked(ip) or login_by_account.is_blocked(account):
        raise _too_many(login_by_account)
    try:
        payload = _respond(response, run_in_transaction(lambda s: service.login(s, body.email, body.password, ip)))
    except service.AuthError:
        login_by_ip.record(ip)
        login_by_account.record(account)
        raise
    login_by_account.reset(account)
    return payload


@router.post("/refresh")
def refresh(request: Request, response: Response) -> dict:
    _require_enabled()
    _require_csrf_header(request)
    ip = client_ip(request)
    if login_by_ip.is_blocked(ip):
        raise _too_many(login_by_ip)
    try:
        result = run_in_transaction(lambda s: service.refresh(s, request.cookies.get(REFRESH_COOKIE), ip))
        return _respond(response, result)
    except service.AuthError:
        login_by_ip.record(ip)
        response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
        raise


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> Response:
    _require_enabled()
    _require_csrf_header(request)
    run_in_transaction(lambda s: service.logout(s, request.cookies.get(REFRESH_COOKIE), client_ip(request)))
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
    response.status_code = 204
    return response


@router.get("/me")
def me(user: AuthUser = Depends(current_user)) -> dict:
    _require_enabled()
    with session_scope() as session:
        row = session.scalar(select(User).where(User.id == user.id))
        if row is None or not row.is_active:
            raise HTTPException(status_code=401, detail={"code": "not_authenticated", "message": "Account not found."})
        return _user_dict(row)

