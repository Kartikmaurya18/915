"""Sign in with an identity provider: OAuth 2.0 authorization code flow with PKCE, over OpenID Connect.

The browser is only ever sent to the provider and back; the code is exchanged, and the provider's ID token verified,
by this server. Verified before anyone is trusted:

  * the **state** the provider echoes equals the one sealed in a short-lived cookie that only this browser holds
    (blocks login CSRF: forcing someone into an attacker's account)
  * the **code verifier** (PKCE, S256) proves the code exchange comes from whoever started the flow
  * the ID token's **signature** (provider's published keys), **issuer**, **audience**, **expiry** and **nonce**
  * the provider says the **email is verified**: an unverified address is never trusted as an identity

Nothing from the provider is kept except its stable subject id and the verified email. No provider access token is
stored: this app only needs to know who signed in. Failures raise OAuthError with a short code the UI can show; the
detail goes to the log and never to the browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from src.auth import settings

log = logging.getLogger("market_data.auth.oauth")

TIMEOUT_SECONDS = 10.0
STATE_TTL_SECONDS = 600
STATE_TYP = "oauth_state"
DISCOVERY_TTL_SECONDS = 3600

GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
ID_TOKEN_ALGORITHMS = ["RS256", "ES256"]  # never "none" and never HS*: the provider's public keys, not a shared secret


class OAuthError(Exception):
    """`code` is a stable short slug safe to show; `detail` is for the log only."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    jwks_url: str
    issuers: tuple[str, ...]
    scopes: str = "openid email profile"
    extra_params: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ProviderIdentity:
    provider: str
    subject: str
    email: str


# -- configuration ------------------------------------------------------------------------------------------------


_discovery: dict[str, tuple[float, dict]] = {}


def _require_https_in_production(url: str, what: str) -> None:
    if settings.is_production() and not url.lower().startswith("https://"):
        raise OAuthError("provider_misconfigured", f"{what} must be https in production")


def _discover(issuer: str) -> dict:
    cached = _discovery.get(issuer)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    _require_https_in_production(issuer, "OIDC_ISSUER")
    doc = _get_json(issuer.rstrip("/") + "/.well-known/openid-configuration")
    if str(doc.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
        raise OAuthError("provider_misconfigured", "discovery document issuer does not match OIDC_ISSUER")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(key):
            raise OAuthError("provider_misconfigured", f"discovery document has no {key}")
        _require_https_in_production(str(doc[key]), key)
    _discovery[issuer] = (time.monotonic() + DISCOVERY_TTL_SECONDS, doc)
    return doc


def get_provider(provider_id: str) -> Provider:
    import os

    if provider_id not in settings.oauth_providers():
        raise OAuthError("unknown_provider", provider_id)
    if provider_id == "google":
        return Provider(
            id="google",
            label="Google",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            authorize_url=GOOGLE_AUTHORIZE,
            token_url=GOOGLE_TOKEN,
            jwks_url=GOOGLE_JWKS,
            issuers=GOOGLE_ISSUERS,
            extra_params=(("prompt", "select_account"),),  # let people choose which Google account, not silently the last one
        )
    issuer = os.environ["OIDC_ISSUER"].rstrip("/")
    doc = _discover(issuer)
    return Provider(
        id="oidc",
        label=os.getenv("OIDC_LABEL", "Single sign-on"),
        client_id=os.environ["OIDC_CLIENT_ID"],
        client_secret=os.environ["OIDC_CLIENT_SECRET"],
        authorize_url=doc["authorization_endpoint"],
        token_url=doc["token_endpoint"],
        jwks_url=doc["jwks_uri"],
        issuers=(issuer, issuer + "/"),
    )


def enabled() -> list[dict]:
    """What the sign-in page may offer. No secrets, no network: labels only."""
    import os

    labels = {"google": "Google", "oidc": os.getenv("OIDC_LABEL", "Single sign-on")}
    return [{"id": p, "label": labels[p]} for p in settings.oauth_providers()]


# -- HTTP (standard library, like the delivery module: no new dependency) ---------------------------------------


def _get_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=TIMEOUT_SECONDS) as r:  # noqa: S310
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError("provider_unreachable", f"GET {urllib.parse.urlsplit(url).netloc}: {exc.__class__.__name__}") from None


def _post_form(url: str, fields: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as r:  # noqa: S310 - operator-configured provider URL
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # the body can contain the code or client details: do not read it into the log
        raise OAuthError("token_exchange_failed", f"token endpoint answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError("provider_unreachable", f"POST {urllib.parse.urlsplit(url).netloc}: {exc.__class__.__name__}") from None


# -- starting the flow ----------------------------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_verifier() -> str:
    return secrets.token_urlsafe(48)  # 64 characters: within RFC 7636's 43-128


def challenge_for(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _seal(payload: dict) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {**payload, "typ": STATE_TYP, "iat": int(now.timestamp()), "exp": int((now + timedelta(seconds=STATE_TTL_SECONDS)).timestamp())},
        settings.jwt_secret(),
        algorithm="HS256",
    )


def _unseal(token: str) -> dict:
    try:
        claims = jwt.decode(token or "", settings.jwt_secret(), algorithms=["HS256"], options={"require": ["exp", "iat"]})
    except jwt.PyJWTError:
        raise OAuthError("state_expired", "state cookie missing, invalid or expired") from None
    if claims.get("typ") != STATE_TYP:
        raise OAuthError("state_expired", "wrong token type in state cookie")
    return claims


def begin(provider: Provider, redirect_uri: str) -> tuple[str, str]:
    """(URL to send the browser to, sealed state cookie value)."""
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), new_verifier()
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "scope": provider.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
        **dict(provider.extra_params),
    }
    cookie = _seal({"p": provider.id, "state": state, "nonce": nonce, "verifier": verifier})
    return provider.authorize_url + "?" + urllib.parse.urlencode(params), cookie


# -- finishing the flow ---------------------------------------------------------------------------------------------


_jwks_clients: dict[str, jwt.PyJWKClient] = {}


def _jwks(url: str) -> jwt.PyJWKClient:
    client = _jwks_clients.get(url)
    if client is None:
        client = _jwks_clients[url] = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=TIMEOUT_SECONDS)
    return client


def verify_id_token(provider: Provider, id_token: str, nonce: str) -> ProviderIdentity:
    try:
        key = _jwks(provider.jwks_url).get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token,
            key,
            algorithms=ID_TOKEN_ALGORITHMS,
            audience=provider.client_id,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            leeway=60,
        )
    except jwt.PyJWKClientError as exc:
        raise OAuthError("provider_unreachable", f"signing keys unavailable: {exc.__class__.__name__}") from None
    except jwt.PyJWTError as exc:
        raise OAuthError("id_token_invalid", exc.__class__.__name__) from None
    if claims["iss"] not in provider.issuers:
        raise OAuthError("id_token_invalid", "unexpected issuer")
    audience = claims["aud"]
    if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != provider.client_id:
        raise OAuthError("id_token_invalid", "token was issued to another party")
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OAuthError("id_token_invalid", "nonce mismatch (replayed or foreign token)")
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise OAuthError("email_missing", "provider returned no email")
    if claims.get("email_verified") not in (True, "true"):
        raise OAuthError("email_unverified", "provider has not verified this email")
    return ProviderIdentity(provider.id, str(claims["sub"]), email)


def finish(provider: Provider, redirect_uri: str, code: str, state: str, state_cookie: str) -> ProviderIdentity:
    sealed = _unseal(state_cookie)
    if sealed.get("p") != provider.id or not hmac.compare_digest(str(sealed.get("state", "")), state or ""):
        raise OAuthError("state_mismatch", "state does not match this browser's login attempt")
    if not code:
        raise OAuthError("token_exchange_failed", "no code")
    tokens = _post_form(
        provider.token_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": provider.client_id,
            "client_secret": provider.client_secret,
            "code_verifier": sealed["verifier"],
        },
    )
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise OAuthError("token_exchange_failed", "no id_token in the token response")
    return verify_id_token(provider, id_token, str(sealed["nonce"]))
