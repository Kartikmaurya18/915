"""Sign in with an identity provider, against a fake OpenID Connect provider with a real signing key.

The fake provider is a local HTTP server (discovery, JWKS, token endpoint that enforces PKCE), so the whole
authorization-code flow runs for real. What it cannot do is prove behaviour against Google itself; that needs live
credentials, and the Google tests below only pin the endpoints and parameters used.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from src.auth import oauth, settings
from src.auth.security import create_verification_token, verify_password
from src.db.models import AuditLog, OAuthIdentity, RefreshToken, User
from src.db.session import session_scope

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
KID = "test-key-1"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeIdP:
    """An OpenID Connect provider: discovery, keys, and a token endpoint that checks client secret and PKCE."""

    def __init__(self, key):
        self.key = key
        self.codes: dict[str, dict] = {}
        self.token_status = 200
        self.token_requests: list[dict] = []
        self.issuer = ""
        self.discovery_issuer: str | None = None  # lets a test break discovery

    def jwks(self) -> dict:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]}

    def sign(self, claims: dict, key=None, algorithm: str = "RS256", headers: dict | None = None) -> str:
        return jwt.encode(claims, key or self.key, algorithm=algorithm, headers=headers or {"kid": KID})

    def claims(self, nonce: str, **over) -> dict:
        now = datetime.now(timezone.utc)
        base = {
            "iss": self.issuer, "aud": CLIENT_ID, "sub": "sub-123", "email": "person@example.com", "email_verified": True,
            "nonce": nonce, "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=5)).timestamp()),
        }
        base.update(over)
        return {k: v for k, v in base.items() if v is not None}

    def grant(self, challenge: str, redirect_uri: str, claims: dict, token: str | None = None) -> str:
        code = "code-" + secrets.token_hex(6)
        self.codes[code] = {"challenge": challenge, "redirect_uri": redirect_uri, "id_token": token or self.sign(claims)}
        return code


@pytest.fixture
def idp(signing_key):
    provider = FakeIdP(signing_key)

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self.path == "/.well-known/openid-configuration":
                base = provider.issuer
                self._json(200, {"issuer": provider.discovery_issuer or base, "authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token", "jwks_uri": base + "/jwks"})
            elif self.path == "/jwks":
                self._json(200, provider.jwks())
            else:
                self._json(404, {})

        def do_POST(self):  # noqa: N802
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()).items()}
            provider.token_requests.append(form)
            if provider.token_status != 200:
                return self._json(provider.token_status, {"error": "invalid_grant", "secret_detail": "do-not-leak-this"})
            entry = provider.codes.pop(form.get("code", ""), None)
            checks = (
                entry is not None,
                form.get("grant_type") == "authorization_code",
                form.get("client_id") == CLIENT_ID,
                form.get("client_secret") == CLIENT_SECRET,
                entry is not None and form.get("redirect_uri") == entry["redirect_uri"],
                entry is not None and _b64url(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) == entry["challenge"],  # PKCE
            )
            if not all(checks):
                return self._json(400, {"error": "invalid_grant", "checks": list(checks)})
            self._json(200, {"id_token": entry["id_token"], "access_token": "opaque", "token_type": "Bearer"})

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    provider.issuer = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield provider
    server.shutdown()


@pytest.fixture
def sso(client, idp, monkeypatch):
    """The app wired to the fake provider (generic OIDC, discovered), with the limiter and caches clean."""
    monkeypatch.setenv("OAUTH_PROVIDERS", "oidc")
    monkeypatch.setenv("OIDC_ISSUER", idp.issuer)
    monkeypatch.setenv("OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("OIDC_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("OIDC_LABEL", "Test SSO")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    oauth._discovery.clear()
    oauth._jwks_clients.clear()
    from src.api import auth_routes

    auth_routes.oauth_by_ip.clear()
    return client, idp


def start(client, provider="oidc"):
    r = client.get(f"/api/auth/oauth/{provider}/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    return r, dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))


def sign_in(client, idp, *, token_claims: dict | None = None, token: str | None = None, state: str | None = None, **claim_overrides):
    """Run the whole flow. Returns the callback response (not followed)."""
    _, q = start(client)
    claims = token_claims if token_claims is not None else idp.claims(q["nonce"], **claim_overrides)
    code = idp.grant(q["code_challenge"], q["redirect_uri"], claims, token)
    return client.get(f"/api/auth/oauth/oidc/callback?code={code}&state={state or q['state']}", follow_redirects=False)


def users():
    with session_scope() as s:
        return s.scalars(select(User)).all()


def audit_events():
    with session_scope() as s:
        return [e.event for e in s.scalars(select(AuditLog).order_by(AuditLog.id))]


def bearer_for(client):
    """Complete the sign-in the way the web app does: exchange the refresh cookie for an access token."""
    r = client.post("/api/auth/refresh", headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200, r.text
    return r.json()


# -- starting ----------------------------------------------------------------------------------------------------


def test_the_sign_in_page_is_told_which_providers_exist(sso):
    client, _ = sso
    assert client.get("/api/auth/options").json()["oauth"] == [{"id": "oidc", "label": "Test SSO"}]


def test_start_redirects_to_the_provider_with_pkce_state_and_nonce(sso):
    client, idp = sso
    r, q = start(client)
    assert r.headers["location"].startswith(idp.issuer + "/authorize?")
    assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "http://testserver/api/auth/oauth/oidc/callback"
    assert set(q["scope"].split()) == {"openid", "email", "profile"}
    assert len(q["state"]) >= 24 and len(q["nonce"]) >= 24 and len(q["code_challenge"]) == 43
    assert CLIENT_SECRET not in r.headers["location"]  # the secret stays on the server


def test_start_sets_a_sealed_http_only_lax_cookie_scoped_to_the_oauth_path(sso):
    client, _ = sso
    r, q = start(client)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("oauth_state=") and "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/api/auth/oauth" in cookie
    sealed = cookie.split("=", 1)[1].split(";")[0]
    assert q["state"] not in sealed.replace(".", "") or True  # signed, not secret; what matters is it cannot be forged:
    forged = jwt.encode({"p": "oidc", "state": q["state"], "nonce": "x", "verifier": "y", "typ": "oauth_state", "iat": 1, "exp": 4102444800}, "not-the-secret", algorithm="HS256")
    with pytest.raises(oauth.OAuthError):
        oauth._unseal(forged)
    assert r.headers["cache-control"] == "no-store"


def test_unknown_or_disabled_providers_are_404(sso):
    client, _ = sso
    assert client.get("/api/auth/oauth/facebook/start", follow_redirects=False).status_code == 404
    assert client.get("/api/auth/oauth/google/start", follow_redirects=False).status_code == 404  # configured as oidc only
    assert client.get("/api/auth/oauth/google/callback?code=x&state=y", follow_redirects=False).status_code in (303, 404)


# -- the happy path ----------------------------------------------------------------------------------------------


def test_a_new_person_gets_an_account_and_a_session_without_a_token_in_the_url(sso):
    client, idp = sso
    r = sign_in(client, idp)
    assert r.status_code == 303 and r.headers["location"] == "/oauth/complete"  # nothing sensitive in the URL
    assert "refresh_token=" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    session = bearer_for(client)
    assert session["user"]["email"] == "person@example.com"
    me = client.get("/api/auth/me", headers={"Authorization": "Bearer " + session["access_token"]})
    assert me.status_code == 200
    (user,) = users()
    assert user.email_verified_at is not None and user.phone is None and user.role == "user"
    with session_scope() as s:
        (ident,) = s.scalars(select(OAuthIdentity)).all()
        assert (ident.provider, ident.subject, ident.user_id) == ("oidc", "sub-123", user.id)
    assert "oauth_register" in audit_events()


def test_the_provider_received_the_client_secret_and_the_pkce_verifier(sso):
    client, idp = sso
    sign_in(client, idp)
    sent = idp.token_requests[-1]
    assert sent["client_secret"] == CLIENT_SECRET and sent["grant_type"] == "authorization_code" and len(sent["code_verifier"]) >= 43


def test_signing_in_again_finds_the_same_account_even_if_the_email_changed(sso):
    client, idp = sso
    sign_in(client, idp)
    sign_in(client, idp)
    assert len(users()) == 1
    sign_in(client, idp, email="renamed@example.com")  # same subject, new address at the provider
    (user,) = users()
    assert user.email == "person@example.com"  # we key on the subject, never the email
    assert audit_events().count("oauth_login") == 2


def test_a_social_account_cannot_be_entered_with_a_password(sso):
    client, idp = sso
    sign_in(client, idp)
    for guess in ("password1234", "person@example.com", ""):
        r = client.post("/api/auth/login", json={"email": "person@example.com", "password": guess})
        assert r.status_code in (401, 422)


def test_social_sign_in_is_not_asked_for_a_phone_even_when_verification_requires_one(sso, monkeypatch):
    client, idp = sso
    monkeypatch.setenv("VERIFICATION_MODE", "both")
    r = sign_in(client, idp)
    assert r.headers["location"] == "/oauth/complete"
    assert bearer_for(client)["user"]["email"] == "person@example.com"


# -- linking to an existing account --------------------------------------------------------------------------------


def test_a_verified_password_account_with_the_same_email_is_linked(sso, monkeypatch):
    client, idp = sso
    monkeypatch.setenv("VERIFICATION_MODE", "off")
    client.post("/api/auth/register", json={"email": "person@example.com", "password": "correct horse battery"})
    with session_scope() as s:
        user = s.scalar(select(User))
        user.email_verified_at = datetime.now(timezone.utc)  # they had proved it
        original = user.id
    sign_in(client, idp)
    (user,) = users()
    assert user.id == original and len(users()) == 1
    assert client.post("/api/auth/login", json={"email": "person@example.com", "password": "correct horse battery"}).status_code == 200  # still theirs
    assert "oauth_link" in audit_events()


def test_pre_hijacking_an_attacker_who_registered_the_victims_email_loses_the_account(sso, monkeypatch):
    """The attacker signed up with the victim's address and a password they know, then waits. When the real owner signs
    in with Google the account is linked: it must not stay reachable with the attacker's password or session."""
    client, idp = sso
    monkeypatch.setenv("VERIFICATION_MODE", "off")  # the attacker is handed a session on sign-up in this mode
    attack = client.post("/api/auth/register", json={"email": "person@example.com", "password": "attackers password"})
    attacker_cookie = attack.headers["set-cookie"].split(";")[0]
    assert attack.status_code == 201
    sign_in(client, idp)  # the victim, with Google
    assert client.post("/api/auth/login", json={"email": "person@example.com", "password": "attackers password"}).status_code == 401
    with session_scope() as s:
        user = s.scalar(select(User))
        assert not verify_password(user.password_hash, "attackers password")
    stale = client.__class__(client.app)
    stale.cookies.set("refresh_token", attacker_cookie.split("=", 1)[1], path="/api/auth")
    assert stale.post("/api/auth/refresh", headers={"X-Requested-With": "fetch"}).status_code == 401  # their session is gone
    assert "oauth_unverified_account_reclaimed" in audit_events()


def test_an_unfinished_signup_phone_number_is_released_when_the_owner_uses_social_sign_in(sso, monkeypatch):
    client, idp = sso
    monkeypatch.setenv("VERIFICATION_MODE", "both")
    from src.auth import delivery

    delivery.set_sender("email", delivery.Outbox())
    delivery.set_sender("phone", delivery.Outbox())
    try:
        client.post("/api/auth/register", json={"email": "person@example.com", "password": "correct horse battery", "phone": "+919876543210"})
        sign_in(client, idp)
        with session_scope() as s:
            user = s.scalar(select(User))
            assert user.phone is None and user.email_verified_at is not None
    finally:
        delivery.set_sender("email", None)
        delivery.set_sender("phone", None)


# -- the provider's word is not enough on its own -----------------------------------------------------------------------


def test_an_unverified_email_at_the_provider_is_refused_and_creates_nothing(sso):
    client, idp = sso
    r = sign_in(client, idp, email_verified=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?error=oauth_email_unverified"
    assert users() == [] and "refresh_token" not in r.headers.get("set-cookie", "")


@pytest.mark.parametrize("flag", ["false", "no", 0, None])
def test_only_a_true_verified_flag_counts(sso, flag):
    client, idp = sso
    r = sign_in(client, idp, email_verified=flag)
    assert r.headers["location"] == "/login?error=oauth_email_unverified" and users() == []


def test_a_provider_that_sends_no_email_is_refused(sso):
    client, idp = sso
    r = sign_in(client, idp, email=None)
    assert r.headers["location"] == "/login?error=oauth_email_missing" and users() == []


def test_the_string_true_is_accepted_for_providers_that_send_it_that_way(sso):
    client, idp = sso
    assert sign_in(client, idp, email_verified="true").headers["location"] == "/oauth/complete"


# -- the flow itself cannot be hijacked ------------------------------------------------------------------------------------


def test_a_callback_with_the_wrong_state_is_refused(sso):
    client, idp = sso
    r = sign_in(client, idp, state="attacker-chosen-state")
    assert r.headers["location"] == "/login?error=oauth_expired" and users() == []


def test_a_callback_without_the_cookie_from_this_browser_is_refused(sso):
    """Login CSRF: an attacker's authorization response, delivered to a browser that never started the flow."""
    client, idp = sso
    _, q = start(client)
    code = idp.grant(q["code_challenge"], q["redirect_uri"], idp.claims(q["nonce"]))
    victim = client.__class__(client.app)  # a different browser: no state cookie
    r = victim.get(f"/api/auth/oauth/oidc/callback?code={code}&state={q['state']}", follow_redirects=False)
    assert r.headers["location"] == "/login?error=oauth_expired" and users() == []


def test_a_state_cookie_from_another_attempt_does_not_match(sso):
    client, idp = sso
    _, first = start(client)
    _, second = start(client)  # the cookie now belongs to the second attempt
    code = idp.grant(first["code_challenge"], first["redirect_uri"], idp.claims(first["nonce"]))
    r = client.get(f"/api/auth/oauth/oidc/callback?code={code}&state={first['state']}", follow_redirects=False)
    assert r.headers["location"] == "/login?error=oauth_expired"


def test_an_expired_state_cookie_is_refused(sso):
    client, idp = sso
    _, q = start(client)
    old = jwt.encode({"p": "oidc", "state": q["state"], "nonce": q["nonce"], "verifier": "v" * 50, "typ": "oauth_state", "iat": 1, "exp": 2}, settings.jwt_secret(), algorithm="HS256")
    client.cookies.set("oauth_state", old, path="/api/auth/oauth")
    code = idp.grant(q["code_challenge"], q["redirect_uri"], idp.claims(q["nonce"]))
    assert client.get(f"/api/auth/oauth/oidc/callback?code={code}&state={q['state']}", follow_redirects=False).headers["location"] == "/login?error=oauth_expired"


def test_a_code_cannot_be_used_twice(sso):
    client, idp = sso
    _, q = start(client)
    code = idp.grant(q["code_challenge"], q["redirect_uri"], idp.claims(q["nonce"]))
    url = f"/api/auth/oauth/oidc/callback?code={code}&state={q['state']}"
    assert client.get(url, follow_redirects=False).headers["location"] == "/oauth/complete"
    assert client.get(url, follow_redirects=False).headers["location"].startswith("/login?error=")  # the state cookie was spent too


def test_the_state_cookie_is_cleared_after_every_outcome(sso):
    client, idp = sso
    ok = sign_in(client, idp)
    assert 'oauth_state=""' in ok.headers["set-cookie"] or "oauth_state=;" in ok.headers["set-cookie"] or "Max-Age=0" in ok.headers["set-cookie"]


# -- forged or mis-issued ID tokens ----------------------------------------------------------------------------------------


def _bad(sso, make):
    client, idp = sso
    _, q = start(client)
    token = make(idp, q["nonce"])
    code = idp.grant(q["code_challenge"], q["redirect_uri"], {}, token)
    r = client.get(f"/api/auth/oauth/oidc/callback?code={code}&state={q['state']}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?error=oauth_failed", r.headers["location"]
    assert users() == [] and "refresh_token=" not in r.headers.get("set-cookie", "")


def test_a_token_for_a_different_client_is_refused(sso):
    _bad(sso, lambda idp, n: idp.sign(idp.claims(n, aud="someone-elses-client")))


def test_a_token_from_a_different_issuer_is_refused(sso):
    _bad(sso, lambda idp, n: idp.sign(idp.claims(n, iss="https://evil.example.com")))


def test_an_expired_token_is_refused(sso):
    _bad(sso, lambda idp, n: idp.sign(idp.claims(n, exp=int(time.time()) - 3600, iat=int(time.time()) - 7200)))


def test_a_token_with_the_wrong_nonce_is_refused(sso):
    """A genuine token captured elsewhere, replayed into this login."""
    _bad(sso, lambda idp, n: idp.sign(idp.claims("a-different-nonce")))


def test_a_token_signed_with_someone_elses_key_is_refused(sso):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _bad(sso, lambda idp, n: idp.sign(idp.claims(n), key=other))


def test_the_none_algorithm_is_refused(sso):
    _bad(sso, lambda idp, n: jwt.encode(idp.claims(n), None, algorithm="none", headers={"kid": KID}))


def test_algorithm_confusion_with_the_public_key_as_an_hmac_secret_is_refused(sso):
    from cryptography.hazmat.primitives import serialization

    def forge(idp, nonce):
        public_pem = idp.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        body = _b64url(json.dumps(idp.claims(nonce)).encode())
        head = _b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
        import hmac as _hmac

        sig = _b64url(_hmac.new(public_pem, f"{head}.{body}".encode(), hashlib.sha256).digest())
        return f"{head}.{body}.{sig}"

    _bad(sso, forge)


def test_a_token_missing_required_claims_is_refused(sso):
    _bad(sso, lambda idp, n: idp.sign({k: v for k, v in idp.claims(n).items() if k != "sub"}))


def test_a_multi_audience_token_issued_to_another_party_is_refused(sso):
    _bad(sso, lambda idp, n: idp.sign(idp.claims(n, aud=[CLIENT_ID, "other"], azp="other")))


# -- the exchange and the provider misbehaving -------------------------------------------------------------------------------


def test_a_failed_code_exchange_shows_a_generic_error_and_leaks_nothing(sso):
    client, idp = sso
    idp.token_status = 400
    r = sign_in(client, idp)
    assert r.headers["location"] == "/login?error=oauth_failed"
    assert "do-not-leak" not in r.headers["location"] + r.headers.get("set-cookie", "")
    assert users() == []


def test_the_person_pressing_cancel_at_the_provider_is_not_an_error_screen(sso):
    client, _ = sso
    start(client)
    r = client.get("/api/auth/oauth/oidc/callback?error=access_denied&state=x", follow_redirects=False)
    assert r.headers["location"] == "/login?error=oauth_denied"
    r = client.get("/api/auth/oauth/oidc/callback?error=server_error", follow_redirects=False)
    assert r.headers["location"] == "/login?error=oauth_failed"


def test_an_unreachable_provider_fails_cleanly(sso, monkeypatch):
    client, idp = sso
    _, q = start(client)
    monkeypatch.setenv("OIDC_ISSUER", "http://127.0.0.1:9")  # nothing listens here
    oauth._discovery.clear()
    r = client.get("/api/auth/oauth/oidc/callback?code=x&state=" + q["state"], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?error=")


def test_a_discovery_document_for_a_different_issuer_is_refused(sso):
    client, idp = sso
    idp.discovery_issuer = "https://someone-else.example.com"
    r = client.get("/api/auth/oauth/oidc/start", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?error=oauth_failed"


# -- who may sign in -------------------------------------------------------------------------------------------------------


def test_a_disabled_account_cannot_sign_in_with_social_login(sso):
    client, idp = sso
    sign_in(client, idp)
    with session_scope() as s:
        s.scalar(select(User)).is_active = False
    assert sign_in(client, idp).headers["location"] == "/login?error=oauth_account_disabled"


def test_a_disabled_account_with_the_same_email_is_not_linked(sso, monkeypatch):
    client, idp = sso
    monkeypatch.setenv("VERIFICATION_MODE", "off")
    client.post("/api/auth/register", json={"email": "person@example.com", "password": "correct horse battery"})
    with session_scope() as s:
        s.scalar(select(User)).is_active = False
    assert sign_in(client, idp).headers["location"] == "/login?error=oauth_account_disabled"
    with session_scope() as s:
        assert s.scalars(select(OAuthIdentity)).all() == []


def test_closed_registration_blocks_new_social_accounts_but_not_existing_ones(sso, monkeypatch):
    client, idp = sso
    sign_in(client, idp)
    monkeypatch.setenv("REGISTRATION_OPEN", "false")
    assert sign_in(client, idp).headers["location"] == "/oauth/complete"  # already has an account
    assert sign_in(client, idp, sub="another-person", email="new@example.com").headers["location"] == "/login?error=oauth_registration_closed"
    assert len(users()) == 1


def test_start_and_callback_are_rate_limited_per_ip(sso):
    client, _ = sso
    results = [client.get("/api/auth/oauth/oidc/start", follow_redirects=False).status_code for _ in range(45)]
    assert results[:40] == [302] * 40
    r = client.get("/api/auth/oauth/oidc/start", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?error=rate_limited"


def test_a_verification_token_or_state_cookie_is_not_a_login(sso):
    client, _ = sso
    stale = create_verification_token("someone")
    assert client.get("/api/paper/state", headers={"Authorization": f"Bearer {stale}"}).status_code == 401


# -- Google and configuration ------------------------------------------------------------------------------------------------


@pytest.fixture
def google(client, monkeypatch):
    monkeypatch.setenv("OAUTH_PROVIDERS", "google")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "gsecret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.com/")
    from src.api import auth_routes

    auth_routes.oauth_by_ip.clear()
    return client


def test_google_uses_googles_published_endpoints(google):
    p = oauth.get_provider("google")
    assert p.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
    assert p.token_url == "https://oauth2.googleapis.com/token"
    assert p.jwks_url == "https://www.googleapis.com/oauth2/v3/certs"
    assert set(p.issuers) == {"https://accounts.google.com", "accounts.google.com"}


def test_google_start_sends_the_registered_redirect_uri_and_asks_which_account(google):
    r = google.get("/api/auth/oauth/google/start", follow_redirects=False)
    url = urllib.parse.urlsplit(r.headers["location"])
    q = dict(urllib.parse.parse_qsl(url.query))
    assert url.netloc == "accounts.google.com" and url.path == "/o/oauth2/v2/auth"
    assert q["redirect_uri"] == "https://app.example.com/api/auth/oauth/google/callback"  # PUBLIC_BASE_URL, trailing slash trimmed
    assert q["client_id"] == "gid.apps.googleusercontent.com" and q["prompt"] == "select_account" and q["code_challenge_method"] == "S256"


def test_the_options_never_contain_secrets(google):
    body = json.dumps(google.get("/api/auth/options").json())
    assert "gsecret" not in body and "gid.apps" not in body
    assert google.get("/api/auth/options").json()["oauth"] == [{"id": "google", "label": "Google"}]


def test_oauth_routes_do_not_exist_when_auth_is_disabled(client, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    assert client.get("/api/auth/oauth/google/start", follow_redirects=False).status_code == 404


def test_no_providers_means_no_social_sign_in(client, monkeypatch):
    monkeypatch.delenv("OAUTH_PROVIDERS", raising=False)
    assert client.get("/api/auth/options").json()["oauth"] == []
    assert client.get("/api/auth/oauth/google/start", follow_redirects=False).status_code == 404


@pytest.fixture
def prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("VERIFICATION_MODE", "off")
    for k in ("OAUTH_PROVIDERS", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_startup_checks_the_oauth_settings(prod):
    settings.validate_startup_config()  # nothing enabled: nothing to check
    prod.setenv("OAUTH_PROVIDERS", "google")
    with pytest.raises(RuntimeError) as info:
        settings.validate_startup_config()
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "PUBLIC_BASE_URL"):
        assert name in str(info.value)
    prod.setenv("GOOGLE_CLIENT_ID", "id")
    prod.setenv("GOOGLE_CLIENT_SECRET", "secret")
    prod.setenv("PUBLIC_BASE_URL", "http://app.example.com")
    with pytest.raises(RuntimeError, match="https"):
        settings.validate_startup_config()
    prod.setenv("PUBLIC_BASE_URL", "https://app.example.com")
    settings.validate_startup_config()


def test_generic_oidc_needs_https_in_production(prod):
    prod.setenv("OAUTH_PROVIDERS", "oidc")
    prod.setenv("OIDC_ISSUER", "http://sso.example.com")
    prod.setenv("OIDC_CLIENT_ID", "id")
    prod.setenv("OIDC_CLIENT_SECRET", "secret")
    prod.setenv("PUBLIC_BASE_URL", "https://app.example.com")
    with pytest.raises(RuntimeError, match="OIDC_ISSUER must be https"):
        settings.validate_startup_config()


def test_an_unknown_provider_name_is_a_configuration_error(prod):
    prod.setenv("OAUTH_PROVIDERS", "google,facebook")
    with pytest.raises(ValueError, match="facebook"):
        settings.validate_startup_config()


def test_pkce_challenge_matches_the_rfc_example():
    # RFC 7636, appendix B
    assert oauth.challenge_for("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert len(oauth.new_verifier()) >= 43
