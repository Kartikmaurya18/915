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


# -- Verification (OTP by email and SMS) ---------------------------------------------------------------------

VERIFICATION_MODES = ("off", "email", "phone", "both")
EMAIL_PROVIDERS = ("console", "smtp")
SMS_PROVIDERS = ("console", "twilio", "webhook")


def verification_mode() -> str:
    """Which contact details a new account must prove it controls before it can sign in.

    "both" (default): email and mobile. "email" or "phone": just that one. "off": no verification (the
    original behaviour; for local development and the automated tests)."""
    mode = os.getenv("VERIFICATION_MODE", "both").strip().lower()
    if mode not in VERIFICATION_MODES:
        raise ValueError(f"VERIFICATION_MODE must be one of {VERIFICATION_MODES}, got {mode!r}")
    return mode


def required_channels() -> tuple[str, ...]:
    return {"off": (), "email": ("email",), "phone": ("phone",), "both": ("email", "phone")}[verification_mode()]


def email_provider() -> str:
    return os.getenv("EMAIL_PROVIDER", "console").strip().lower()


def sms_provider() -> str:
    return os.getenv("SMS_PROVIDER", "console").strip().lower()


def otp_length() -> int:
    return max(4, min(8, int(os.getenv("OTP_LENGTH", "6"))))


def otp_ttl_seconds() -> int:
    return int(float(os.getenv("OTP_TTL_MINUTES", "10")) * 60)


def otp_max_attempts() -> int:
    return max(1, int(os.getenv("OTP_MAX_ATTEMPTS", "5")))


def otp_resend_seconds() -> int:
    return max(0, int(os.getenv("OTP_RESEND_SECONDS", "60")))


def otp_max_sends_per_hour() -> int:
    return max(1, int(os.getenv("OTP_MAX_SENDS_PER_HOUR", "5")))


def verification_token_ttl_seconds() -> int:
    """How long a half-registered person has to finish verifying before they must sign in again."""
    return int(float(os.getenv("VERIFICATION_TOKEN_TTL_MINUTES", "30")) * 60)


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
        _validate_delivery()


def _validate_delivery() -> None:
    """Every contact channel a new account must verify needs a real way to deliver codes. In production the
    development "console" sender (which prints codes into the log) is refused outright."""
    problems: list[str] = []
    channels = required_channels()
    if "email" in channels:
        provider = email_provider()
        if provider not in EMAIL_PROVIDERS:
            problems.append(f"EMAIL_PROVIDER must be one of {EMAIL_PROVIDERS}, got {provider!r}")
        elif provider == "console" and is_production():
            problems.append("EMAIL_PROVIDER=console prints codes to the log and is not allowed in production (use smtp)")
        elif provider == "smtp":
            for name in ("SMTP_HOST", "EMAIL_FROM"):
                if not os.getenv(name):
                    problems.append(f"{name} is required for EMAIL_PROVIDER=smtp")
    if "phone" in channels:
        provider = sms_provider()
        if provider not in SMS_PROVIDERS:
            problems.append(f"SMS_PROVIDER must be one of {SMS_PROVIDERS}, got {provider!r}")
        elif provider == "console" and is_production():
            problems.append("SMS_PROVIDER=console prints codes to the log and is not allowed in production (use twilio or webhook)")
        elif provider == "twilio":
            for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"):
                if not os.getenv(name):
                    problems.append(f"{name} is required for SMS_PROVIDER=twilio")
            if not (os.getenv("TWILIO_FROM") or os.getenv("TWILIO_MESSAGING_SERVICE_SID")):
                problems.append("TWILIO_FROM or TWILIO_MESSAGING_SERVICE_SID is required for SMS_PROVIDER=twilio")
        elif provider == "webhook":
            url = os.getenv("SMS_WEBHOOK_URL", "")
            if not url:
                problems.append("SMS_WEBHOOK_URL is required for SMS_PROVIDER=webhook")
            elif is_production() and not url.lower().startswith("https://"):
                problems.append("SMS_WEBHOOK_URL must be https in production")
    if problems:
        raise RuntimeError(
            "Refusing to start: account verification (VERIFICATION_MODE=" + verification_mode() + ") is not fully configured: "
            + "; ".join(problems) + ". Set VERIFICATION_MODE=off only if you deliberately do not want verification."
        )
