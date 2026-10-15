"""Authentication, token handling, admin gating and WebSocket auth."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from starlette.websockets import WebSocketDisconnect

from src.auth import service, settings
from src.auth.security import create_access_token
from src.db.models import AuditLog, RefreshToken, User
from src.db.session import session_scope
from tests.conftest import JWT_TEST_SECRET

CSRF = {"X-Requested-With": "fetch"}


def _register(client, email="alice@example.com", password="correct horse battery"):
    return client.post("/api/auth/register", json={"email": email, "password": password})


# -- registration & login ----------------------------------------------------------


def test_register_returns_access_token_and_httponly_refresh_cookie(client):
    response = _register(client)
    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer" and body["user"]["email"] == "alice@example.com"
    assert body["user"]["role"] == "user"
    set_cookie = response.headers["set-cookie"].lower()
    assert "refresh_token=" in set_cookie and "httponly" in set_cookie and "path=/api/auth" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "refresh_token" not in json.dumps(body)  # never exposed to JavaScript
    assert response.headers["cache-control"] == "no-store"

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200 and me.json()["email"] == "alice@example.com"


def test_passwords_are_stored_hashed_and_refresh_tokens_only_as_digests(client):
    body = _register(client).json()
    with session_scope() as session:
        user = session.query(User).one()
        assert user.password_hash.startswith("$argon2id$")
        assert "correct horse" not in user.password_hash
        stored = session.query(RefreshToken).one()
        raw = client.cookies.get("refresh_token")
        assert raw and raw != stored.token_hash and len(stored.token_hash) == 64
    assert body["access_token"]


def test_duplicate_email_is_rejected_case_insensitively(client):
    assert _register(client, "Bob@Example.com").status_code == 201
    second = _register(client, "bob@example.COM")
    assert second.status_code == 409 and second.json()["detail"]["code"] == "email_taken"


@pytest.mark.parametrize(
    "email,password,code",
    [
        ("not-an-email", "correct horse battery", "invalid_email"),
        ("a@b", "correct horse battery", "invalid_email"),
        ("ok@example.com", "short", "weak_password"),
        ("ok@example.com", "x" * 129, "weak_password"),
    ],
)
def test_registration_validation(client, email, password, code):
    response = _register(client, email, password)
    assert response.status_code == 422
    detail = response.json()["detail"]
    if isinstance(detail, dict):  # rejected by our own validation; length caps fail earlier, in the schema
        assert detail["code"] == code


def test_registration_can_be_closed(client, monkeypatch):
    monkeypatch.setenv("REGISTRATION_OPEN", "false")
    assert _register(client).status_code == 403


def test_login_failures_are_indistinguishable(client):
    _register(client, "carol@example.com")
    wrong_password = client.post("/api/auth/login", json={"email": "carol@example.com", "password": "nope-nope-nope"})
    unknown_user = client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"})
    malformed = client.post("/api/auth/login", json={"email": "###", "password": "nope-nope-nope"})
    assert wrong_password.status_code == unknown_user.status_code == malformed.status_code == 401
    assert wrong_password.json() == unknown_user.json() == malformed.json()


def test_failed_logins_are_audited_without_the_email_address(client):
    client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"})
    with session_scope() as session:
        entry = session.query(AuditLog).filter_by(event="login_failed").one()
        assert "ghost@example.com" not in json.dumps(entry.detail)


def test_repeated_failed_logins_lock_the_account_out_temporarily(client):
    _register(client, "dave@example.com")
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": "dave@example.com", "password": "wrong-wrong-1"}).status_code == 401
    locked = client.post("/api/auth/login", json={"email": "dave@example.com", "password": "correct horse battery"})
    assert locked.status_code == 429 and "retry-after" in locked.headers


def test_successful_login_clears_the_failure_counter(client):
    _register(client, "erin@example.com")
    for _ in range(3):
        client.post("/api/auth/login", json={"email": "erin@example.com", "password": "wrong-wrong-1"})
    ok = client.post("/api/auth/login", json={"email": "erin@example.com", "password": "correct horse battery"})
    assert ok.status_code == 200
    for _ in range(4):  # a fresh budget of 5
        assert client.post("/api/auth/login", json={"email": "erin@example.com", "password": "wrong-wrong-1"}).status_code == 401


def test_registration_is_rate_limited_per_ip(client):
    statuses = [_register(client, f"flood{i}@example.com").status_code for i in range(12)]
    assert statuses[:10] == [201] * 10 and statuses[10:] == [429, 429]


# -- access tokens -----------------------------------------------------------------


def test_protected_routes_require_a_valid_bearer_token(client):
    assert client.get("/api/snapshot").status_code == 401
    assert client.get("/api/paper/state").status_code == 401
    assert client.post("/api/paper/open", json={"option_type": "CE"}).status_code == 401
    assert client.get("/api/snapshot", headers={"Authorization": "Bearer garbage"}).status_code == 401
    assert client.get("/api/snapshot", headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.get("/api/health").status_code == 200  # liveness stays public


def test_expired_wrong_signature_and_alg_none_tokens_are_rejected(client):
    _register(client)
    user_id = "someone"
    expired, _ = create_access_token(user_id, "user", now=datetime.now(timezone.utc) - timedelta(hours=1))
    forged = jwt.encode(
        {"iss": "market-data", "aud": "market-data-api", "sub": user_id, "typ": "access",
         "iat": 1, "exp": 4102444800},
        "a-completely-different-secret-" + "y" * 30,
        algorithm="HS256",
    )
    unsigned = jwt.encode(
        {"iss": "market-data", "aud": "market-data-api", "sub": user_id, "typ": "access",
         "iat": 1, "exp": 4102444800},
        None,
        algorithm="none",
    )
    wrong_audience = jwt.encode(
        {"iss": "market-data", "aud": "someone-else", "sub": user_id, "typ": "access",
         "iat": 1, "exp": 4102444800},
        JWT_TEST_SECRET,
        algorithm="HS256",
    )
    refresh_typed = jwt.encode(
        {"iss": "market-data", "aud": "market-data-api", "sub": user_id, "typ": "refresh",
         "iat": 1, "exp": 4102444800},
        JWT_TEST_SECRET,
        algorithm="HS256",
    )
    for token in (expired, forged, unsigned, wrong_audience, refresh_typed):
        response = client.get("/api/snapshot", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401, token
    expired_response = client.get("/api/snapshot", headers={"Authorization": f"Bearer {expired}"})
    assert expired_response.json()["detail"]["code"] == "token_expired"


# -- refresh rotation --------------------------------------------------------------


def test_refresh_requires_the_csrf_header(client):
    _register(client)
    assert client.post("/api/auth/refresh").status_code == 403


def test_refresh_rotates_the_token_and_issues_a_new_access_token(client):
    _register(client)
    first_cookie = client.cookies.get("refresh_token")
    response = client.post("/api/auth/refresh", headers=CSRF)
    assert response.status_code == 200 and response.json()["access_token"]
    assert client.cookies.get("refresh_token") != first_cookie


def test_replaying_a_spent_refresh_token_revokes_the_whole_session(client):
    _register(client)
    stolen = client.cookies.get("refresh_token")
    assert client.post("/api/auth/refresh", headers=CSRF).status_code == 200  # legitimate rotation
    current = client.cookies.get("refresh_token")

    client.cookies.set("refresh_token", stolen, path="/api/auth")
    replay = client.post("/api/auth/refresh", headers=CSRF)
    assert replay.status_code == 401 and replay.json()["detail"]["code"] == "refresh_token_reused"

    client.cookies.set("refresh_token", current, path="/api/auth")  # the rotated one is now dead too
    assert client.post("/api/auth/refresh", headers=CSRF).status_code == 401
    with session_scope() as session:
        assert session.query(AuditLog).filter_by(event="refresh_reuse_detected").count() >= 1  # replay + use of the revoked rotation


def test_expired_refresh_token_is_rejected(client):
    _register(client)
    with session_scope() as session:
        token = session.query(RefreshToken).one()
        token.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    response = client.post("/api/auth/refresh", headers=CSRF)
    assert response.status_code == 401 and response.json()["detail"]["code"] == "refresh_token_expired"


def test_logout_revokes_the_session(client):
    _register(client)
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 204
    assert client.post("/api/auth/refresh", headers=CSRF).status_code == 401


def test_deactivated_user_cannot_refresh(client):
    _register(client, "frank@example.com")
    with session_scope() as session:
        session.query(User).one().is_active = False
    assert client.post("/api/auth/refresh", headers=CSRF).status_code == 401
    assert client.post("/api/auth/login", json={"email": "frank@example.com", "password": "correct horse battery"}).status_code == 401


# -- admin gating ------------------------------------------------------------------


def test_regular_users_cannot_use_operator_endpoints(client, new_user):
    _, headers = new_user()
    assert client.get("/api/logs", headers=headers).status_code == 403
    assert client.post("/api/settings/data-source", json={"data_source": "mock"}, headers=headers).status_code == 403
    assert client.post("/api/settings/atm", json={"atm_method": "price"}, headers=headers).status_code == 403
    assert client.post("/api/settings/candle-basis", json={"candle_price_basis": "underlying"}, headers=headers).status_code == 403
    assert client.get("/api/settings", headers=headers).status_code == 200  # read-only view is fine


def test_admin_role_is_granted_only_out_of_band_and_rechecked_in_the_database(client):
    email = "admin@example.com"
    body = _register(client, email).json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get("/api/logs", headers=headers).status_code == 403  # registering never grants admin

    with session_scope() as session:
        service.set_role(session, email, "admin")
    assert client.get("/api/logs", headers=headers).status_code == 200  # no re-login needed

    with session_scope() as session:
        service.set_role(session, email, "user")
    assert client.get("/api/logs", headers=headers).status_code == 403  # demotion is immediate


# -- configuration guards ----------------------------------------------------------


def test_production_refuses_to_run_without_auth(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_MODE", "disabled")
    with pytest.raises(RuntimeError, match="AUTH_MODE=required"):
        settings.validate_startup_config()


def test_production_defaults_to_required_auth(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AUTH_MODE", raising=False)
    assert settings.auth_mode() == "required"


def test_required_auth_needs_a_long_jwt_secret(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("JWT_SECRET", "too-short")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        settings.validate_startup_config()


def test_unknown_auth_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "maybe")
    with pytest.raises(ValueError):
        settings.auth_mode()


def test_auth_routes_do_not_exist_when_auth_is_disabled(monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.server import app, engine

    monkeypatch.setattr(engine, "start", lambda: None)  # no background tick thread needed here
    monkeypatch.setenv("AUTH_MODE", "disabled")
    assert TestClient(app).post("/api/auth/login", json={"email": "a@b.co", "password": "x" * 12}).status_code == 404


# -- other hardening ---------------------------------------------------------------


def test_security_headers_and_body_limit(client):
    response = client.get("/api/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    oversized = client.post("/api/auth/login", content=b"x" * 70_000, headers={"Content-Type": "application/json"})
    assert oversized.status_code == 413


def test_ready_is_503_while_the_engine_is_down_and_200_once_it_runs(client, monkeypatch):
    from src.api.server import engine

    down = client.get("/api/ready")  # this fixture runs without the tick thread
    assert down.status_code == 503
    assert down.json()["checks"] == {"engine": "down", "database": "ok"}

    monkeypatch.setattr(type(engine), "is_running", property(lambda self: True))
    up = client.get("/api/ready")
    assert up.status_code == 200 and up.json() == {"status": "ready", "checks": {"engine": "ok", "database": "ok"}}


def test_ready_flags_an_unmigrated_database(client, monkeypatch):
    from src.api.server import engine
    from src.db import migrate

    monkeypatch.setattr(type(engine), "is_running", property(lambda self: True))
    monkeypatch.setattr(migrate, "schema_status", lambda: (False, "current=None head=0001"))
    response = client.get("/api/ready")
    assert response.status_code == 503 and "schema_outdated" in response.json()["checks"]["database"]


# -- WebSocket ---------------------------------------------------------------------


def _expect_close(ws, code):
    with pytest.raises(WebSocketDisconnect) as info:
        ws.receive_text()
    assert info.value.code == code


def test_ws_requires_an_auth_frame_and_sends_nothing_before_it(client, monkeypatch):
    from src.api import server

    monkeypatch.setattr(server, "WS_AUTH_TIMEOUT_SECONDS", 0.3)
    with client.websocket_connect("/ws") as ws:
        _expect_close(ws, 4401)  # silence -> timeout -> closed, never sent a snapshot


@pytest.mark.parametrize("frame", ["not json", json.dumps({"type": "hello"}), json.dumps({"type": "auth", "token": 5}),
                                   json.dumps({"type": "auth", "token": "garbage"}), json.dumps([1, 2])])
def test_ws_rejects_bad_auth_frames(client, frame):
    with client.websocket_connect("/ws") as ws:
        ws.send_text(frame)
        _expect_close(ws, 4401)


def test_ws_streams_market_data_without_the_shared_paper_book(client, new_user):
    _, headers = new_user()
    token = headers["Authorization"].split(" ", 1)[1]
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        first = json.loads(ws.receive_text())
    assert "underlying" in first
    assert "paper_trading" not in first


def test_ws_connections_per_user_are_capped(client, new_user, monkeypatch):
    monkeypatch.setenv("WS_MAX_CONNECTIONS_PER_USER", "2")
    _, headers = new_user()
    frame = json.dumps({"type": "auth", "token": headers["Authorization"].split(" ", 1)[1]})
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b, client.websocket_connect("/ws") as c:
        a.send_text(frame)
        b.send_text(frame)
        a.receive_text()
        b.receive_text()
        c.send_text(frame)
        _expect_close(c, 4429)


def test_env_example_never_sets_security_settings_actively():
    """.env.example is loaded as a fallback by src/config.py (and ships in the image), so any
    active line in it overrides defaults. Auth/DB/secret settings must stay commented out, or
    e.g. AUTH_MODE=disabled would silently defeat production's secure default."""
    from pathlib import Path

    sensitive = {"APP_ENV", "AUTH_MODE", "JWT_SECRET", "DATABASE_URL", "CORS_ORIGINS", "TRUSTED_PROXY_HOPS",
                 "ACCESS_TOKEN_TTL_MINUTES", "REFRESH_TOKEN_TTL_DAYS", "REGISTRATION_OPEN", "COOKIE_SECURE"}
    template = Path(__file__).resolve().parent.parent / ".env.example"
    active = {
        line.split("=", 1)[0].strip()
        for line in template.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert not (active & sensitive), f"active in .env.example: {sorted(active & sensitive)}"
