"""Auth configuration, read from the environment on every call so tests (and
operators using a secret manager that injects late) always see current values."""

from __future__ import annotations

import os

MIN_JWT_SECRET_LENGTH = 32


def app_env() -> str:
    return os.getenv("APP_ENV", "development").strip().lower()


def is_production() -> bool:
    return app_env() in ("production", "prod")


def auth_mode() -> str:
    """"required": every protected route needs a valid access token and all
    paper/settings state is per-user. "disabled": a single implicit local user
    and the original global behaviour - for local development and the legacy UI
    only. Production defaults to (and insists on) "required"."""
    default = "required" if is_production() else "disabled"
    mode = os.getenv("AUTH_MODE", default).strip().lower()
    if mode not in ("required", "disabled"):
        raise ValueError(f"AUTH_MODE must be 'required' or 'disabled', got {mode!r}")
    return mode


def auth_required() -> bool:
    return auth_mode() == "required"


def jwt_secret() -> str:
    return os.getenv("JWT_SECRET", "")


def access_token_ttl_seconds() -> int:
    return int(float(os.getenv("ACCESS_TOKEN_TTL_MINUTES", "15")) * 60)


def refresh_token_ttl_seconds() -> int:
    return int(float(os.getenv("REFRESH_TOKEN_TTL_DAYS", "14")) * 86400)


def registration_open() -> bool:
    return os.getenv("REGISTRATION_OPEN", "true").strip().lower() in ("1", "true", "yes")


def cookie_secure() -> bool:
    explicit = os.getenv("COOKIE_SECURE")
    if explicit is not None:
        return explicit.strip().lower() in ("1", "true", "yes")
    return is_production()


def trusted_proxy_hops() -> int:
    """How many reverse proxies sit in front of the app and append to
    X-Forwarded-For. 0 means never trust that header (the safe default)."""
    return max(0, int(os.getenv("TRUSTED_PROXY_HOPS", "0")))


def max_ws_connections_per_user() -> int:
    return int(os.getenv("WS_MAX_CONNECTIONS_PER_USER", "5"))


def max_ws_connections_total() -> int:
    return int(os.getenv("WS_MAX_CONNECTIONS_TOTAL", "5000"))


def validate_startup_config() -> None:
    """Fail fast on an unsafe configuration instead of serving it."""
    mode = auth_mode()
    if is_production() and mode != "required":
        raise RuntimeError("Refusing to start: APP_ENV=production requires AUTH_MODE=required.")
    if mode == "required":
        secret = jwt_secret()
        if len(secret) < MIN_JWT_SECRET_LENGTH:
            raise RuntimeError(
                f"Refusing to start: AUTH_MODE=required needs JWT_SECRET of at least "
                f"{MIN_JWT_SECRET_LENGTH} characters."
            )
