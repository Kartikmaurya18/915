"""/api/auth/* - registration, login, token refresh, logout.

Access tokens are short-lived and returned in the response body (the SPA keeps
them in memory only). The refresh token never reaches JavaScript: it lives in an
HttpOnly, SameSite=Lax cookie scoped to /api/auth, and refresh/logout also require
a custom request header, which forces a CORS preflight and so blocks cross-site
form posts (CSRF). All routes 404 when AUTH_MODE=disabled.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from src.auth import delivery, otp, service, settings, verification
from src.auth.deps import AuthUser, client_ip, current_user
from src.auth.security import create_verification_token, decode_verification_token
from src.auth.ratelimit import make_limiter
from src.db.models import User
from src.db.session import run_in_transaction, session_scope

REFRESH_COOKIE = "refresh_token"
COOKIE_PATH = "/api/auth"

# Shared through Redis when REDIS_URL is set, so the limits hold across gateway replicas (see ratelimit.py).
login_by_ip = make_limiter("login_ip", max_events=20, window_seconds=300)
login_by_account = make_limiter("login_account", max_events=5, window_seconds=300)
register_by_ip = make_limiter("register_ip", max_events=10, window_seconds=3600)
# One-time-code endpoints. Per account the database also enforces a cooldown and an hourly cap (otp.issue).
otp_send_by_ip = make_limiter("otp_send_ip", max_events=20, window_seconds=3600)
otp_verify_by_ip = make_limiter("otp_verify_ip", max_events=40, window_seconds=900)
reset_by_account = make_limiter("reset_account", max_events=8, window_seconds=3600)


def _account_key(email: str) -> str:
    """A stable tag for an email that is not the address itself (limiter keys can be stored in Redis)."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:32]

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _require_enabled() -> None:
    if not settings.auth_required():
        raise HTTPException(status_code=404, detail="Not found")


def _require_csrf_header(request: Request) -> None:
    if not request.headers.get("x-requested-with"):
        raise HTTPException(status_code=403, detail={"code": "csrf", "message": "Missing X-Requested-With header."})


def _too_many(limiter) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={"code": "rate_limited", "message": "Too many attempts. Try again later."},
        headers={"Retry-After": str(int(limiter.window))},
    )


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class Registration(Credentials):
    # E.164, with or without spaces. Required when VERIFICATION_MODE includes "phone".
    phone: str | None = Field(default=None, max_length=32)


class VerifyBody(BaseModel):
    verification_token: str = Field(max_length=2048)
    channel: str = Field(max_length=8)
    code: str = Field(max_length=16)


class ResendBody(BaseModel):
    verification_token: str = Field(max_length=2048)
    channel: str = Field(max_length=8)


class ForgotBody(BaseModel):
    email: str = Field(max_length=254)
    channel: str = Field(default="email", max_length=8)


class ResetBody(BaseModel):
    email: str = Field(max_length=254)
    channel: str = Field(default="email", max_length=8)
    code: str = Field(max_length=16)
    new_password: str = Field(max_length=128)


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


def _send_codes(background: BackgroundTasks, issued: list[otp.Issued]) -> None:
    """Deliver after the response is committed: a slow or failing provider never delays or rolls back anything."""
    for item in issued:
        subject, body = otp.message_for(item.purpose, item.code)
        background.add_task(delivery.deliver, item.channel, item.destination, subject, body)


def _pending_response(pending: verification.Pending, status_note: str = "verification_required") -> dict:
    return {
        "verification_required": True,
        "verification_token": create_verification_token(pending.user.id),
        "pending": pending.channels,
        "destinations": pending.destinations(),
        "resend_after_seconds": settings.otp_resend_seconds(),
        "code_length": settings.otp_length(),
        "code_ttl_seconds": settings.otp_ttl_seconds(),
    }


@router.get("/options")
def options() -> dict:
    """What the sign-up form should ask for. Public, and says nothing about any account."""
    _require_enabled()
    return {
        "verification": settings.verification_mode(),
        "channels": list(settings.required_channels()),
        "registration_open": settings.registration_open(),
        "code_length": settings.otp_length(),
    }


@router.post("/register", status_code=201)
def register(body: Registration, request: Request, response: Response, background: BackgroundTasks) -> dict:
    _require_enabled()
    ip = client_ip(request)
    if not register_by_ip.hit(ip):
        raise _too_many(register_by_ip)

    if not settings.required_channels():  # verification off: the original, immediate sign-up
        def work(session):
            user = service.register(session, body.email, body.password, ip)
            return service.start_session(session, user, ip, event="login")

        return _respond(response, run_in_transaction(work))

    pending = run_in_transaction(lambda s: verification.register(s, body.email, body.phone, body.password, ip))
    _send_codes(background, pending.issued)
    response.status_code = 202  # accepted, not finished: nothing is signed in yet
    response.headers["Cache-Control"] = "no-store"
    return _pending_response(pending)


@router.post("/verify")
def verify(body: VerifyBody, request: Request, response: Response) -> dict:
    """Check one code. When the last required contact is verified this signs the person in, like /login."""
    _require_enabled()
    ip = client_ip(request)
    if not otp_verify_by_ip.hit(ip):
        raise _too_many(otp_verify_by_ip)
    user_id = decode_verification_token(body.verification_token)

    def work(session):
        user, pending = verification.verify(session, user_id, body.channel, body.code, ip)
        if pending:
            return None, pending
        return service.start_session(session, user, ip, event="login"), []

    session_result, still_pending = run_in_transaction(work)
    response.headers["Cache-Control"] = "no-store"
    if session_result is None:
        return {"verification_required": True, "verification_token": body.verification_token, "pending": still_pending, "verified": body.channel}
    return {**_respond(response, session_result), "verification_required": False}


@router.post("/resend")
def resend(body: ResendBody, request: Request, background: BackgroundTasks, response: Response) -> dict:
    _require_enabled()
    ip = client_ip(request)
    if not otp_send_by_ip.hit(ip):
        raise _too_many(otp_send_by_ip)
    user_id = decode_verification_token(body.verification_token)
    issued = run_in_transaction(lambda s: verification.resend(s, user_id, body.channel, ip))
    _send_codes(background, [issued])
    response.status_code = 202
    return {"sent": True, "channel": body.channel, "resend_after_seconds": settings.otp_resend_seconds()}


@router.post("/password/forgot", status_code=202)
def forgot_password(body: ForgotBody, request: Request, background: BackgroundTasks) -> dict:
    """Always answers the same way, whether or not the account exists."""
    _require_enabled()
    ip = client_ip(request)
    account = _account_key(body.email)
    if not otp_send_by_ip.hit(ip) or not reset_by_account.hit(account):
        raise _too_many(otp_send_by_ip)
    issued = run_in_transaction(lambda s: verification.request_reset(s, body.email, body.channel))
    if issued is not None:
        _send_codes(background, [issued])
    return {"sent": True, "message": "If that account exists, a code is on its way.", "resend_after_seconds": settings.otp_resend_seconds()}


@router.post("/password/reset")
def reset_password(body: ResetBody, request: Request) -> dict:
    _require_enabled()
    ip = client_ip(request)
    if not otp_verify_by_ip.hit(ip):
        raise _too_many(otp_verify_by_ip)
    run_in_transaction(lambda s: verification.reset_password(s, body.email, body.channel, body.code, body.new_password, ip))
    return {"reset": True, "message": "Password changed. Sign in with your new password."}


@router.post("/login")
def login(body: Credentials, request: Request, response: Response):
    _require_enabled()
    ip = client_ip(request)
    account = _account_key(body.email)
    if login_by_ip.is_blocked(ip) or login_by_account.is_blocked(account):
        raise _too_many(login_by_account)
    try:
        payload = _respond(response, run_in_transaction(lambda s: service.login(s, body.email, body.password, ip)))
    except service.VerificationRequired as exc:
        # Right password, unverified contacts: not a failed login. Make sure codes exist and hand back a token.
        pending = run_in_transaction(lambda s: verification.prepare_login(s, exc.user_id))
        login_by_account.reset(account)
        # FastAPI drops background tasks when a route raises, so this response carries its own.
        tasks = BackgroundTasks()
        _send_codes(tasks, pending.issued)
        return JSONResponse(
            status_code=403,
            content={"detail": {"code": "verification_required", "message": exc.message, **_pending_response(pending)}},
            headers={"Cache-Control": "no-store"},
            background=tasks,
        )
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

