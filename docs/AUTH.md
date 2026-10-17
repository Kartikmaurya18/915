# Accounts: registration with one-time codes (OTP) by email and SMS

New accounts prove they control their **email address** and **mobile number** with a 6-digit code before they can sign in. The same codes
power **forgot password**. Trading is a simulation; no real orders are placed.

## What people see

1. **Create account**: email, mobile number (country code picker, India first), password. The app texts and emails a code.
2. **Verify your account**: one box per channel. Each can be done in either order; a tick appears as each is proven; the second one signs you in.
3. **Signing in later, before verifying**: you land on the same verify screen (codes are re-sent if none are still valid), not on an error.
4. **Forgot password**: pick email or mobile, get a code, choose a new password. Every signed-in session of that account is ended.

Accounts created **before** this feature are marked email-verified by the upgrade and are not asked for a phone number. Nobody is locked out.

## Trying it locally (no email or SMS account needed)

With `AUTH_MODE=required` and the default `VERIFICATION_MODE=both`, the **console senders** print the codes into the server's own log instead of
sending them:

```powershell
$env:AUTH_MODE="required"; $env:JWT_SECRET="any-random-string-of-32-or-more-characters"
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m uvicorn src.api.server:app --port 8000 2>&1 | Tee-Object server.log
```

Register in the browser, then look in the terminal for a line like:

```
DEV ONLY, not delivered (sms -> +919876543210): Your Market Data verification code is 125129. It expires in 10 minutes. ...
```

The console senders are for development only. **In production the app refuses to start with them.**

## Turning it on for real

Set these on every gateway (see also `deploy/production.env.template`):

| Setting | Meaning |
|---|---|
| `VERIFICATION_MODE` | `both` (default), `email`, `phone`, or `off` (no verification, the original behaviour) |
| `EMAIL_PROVIDER` | `smtp` (or `console` for development) |
| `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_SECURITY` (`starttls` default, `ssl`, `none`), `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` | Any SMTP service |
| `SMS_PROVIDER` | `twilio`, `webhook` (or `console` for development) |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` **or** `TWILIO_MESSAGING_SERVICE_SID` | Twilio's Messages API (`POST /2010-04-01/Accounts/{sid}/Messages.json`, form-encoded, basic auth) |
| `SMS_WEBHOOK_URL`, `SMS_WEBHOOK_TOKEN` | Any other SMS gateway: the app POSTs `{"to": "+91...", "message": "..."}` as JSON with `Authorization: Bearer <token>`. Must be `https` in production. |
| `OTP_LENGTH` (6), `OTP_TTL_MINUTES` (10), `OTP_MAX_ATTEMPTS` (5), `OTP_RESEND_SECONDS` (60), `OTP_MAX_SENDS_PER_HOUR` (5), `VERIFICATION_TOKEN_TTL_MINUTES` (30) | Tuning |

The app **checks this at start-up** and refuses to run in production if a required channel has no real provider, listing exactly what is missing. Only
the channels you require are checked (`VERIFICATION_MODE=email` needs no SMS provider).

### Choosing providers (your decision; nothing here picks one for you)

* **Email:** any SMTP service works (your own mail host, a transactional provider, or a Gmail/Workspace app password for a small start). Send from a domain
  with **SPF and DKIM** configured, or the codes will land in spam. Gmail's free SMTP is rate-limited and not meant for sign-up volume.
* **SMS in India:** carriers require the **sender and message template to be registered under TRAI's DLT rules**. A provider that "accepts" the request can
  still have the carrier drop the message if it is not registered. Whatever provider you pick will walk you through DLT registration; it can take days, so
  start early. Use `SMS_PROVIDER=webhook` to connect an Indian gateway (the webhook body is deliberately minimal so you can map it to the gateway's API in a
  small function), or `twilio` if you use it. **Both senders were tested against local stand-ins, not against a live account.**
* **Until SMS is ready:** run with `VERIFICATION_MODE=email`. Switching to `both` later asks only new registrations for a phone number.

## Sign in with Google (or any OpenID Connect provider)

People can choose **Continue with Google** on the sign-in and sign-up pages. The same button supports any OpenID Connect provider (Microsoft Entra,
Okta, Keycloak, ...) through `OAUTH_PROVIDERS=oidc`. It is off until you configure it; with no provider the pages look exactly as before.

### Setting up Google (your account, your credentials)

1. In the [Google Cloud console](https://console.cloud.google.com/) create a project, configure the **OAuth consent screen** (app name, support email;
   keep the scopes to `openid`, `email`, `profile`), then **Credentials, Create credentials, OAuth client ID, Web application**.
2. Under **Authorised redirect URIs** add exactly `<PUBLIC_BASE_URL>/api/auth/oauth/google/callback`, for example
   `http://localhost:8000/api/auth/oauth/google/callback` for local testing and `https://app.example.com/api/auth/oauth/google/callback` in production.
   It must match character for character (Google rejects a mismatch). Use `localhost`, not `127.0.0.1`, for local testing.
3. Set on the gateways:

| Setting | Meaning |
|---|---|
| `OAUTH_PROVIDERS` | `google`, `oidc`, or `google,oidc`. Empty (default) = no social sign-in. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | From step 1. Keep the secret in your secret manager. |
| `PUBLIC_BASE_URL` | The address people reach the app at, e.g. `https://app.example.com`. **Required in production and must be https**: the provider redirects back to it. |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_LABEL` | For the generic provider. Endpoints are found from `<issuer>/.well-known/openid-configuration`. |

Google's endpoints, signing algorithm (RS256) and PKCE support were checked against its published discovery document. **Nothing has been run against a
live Google account**: the flow was tested end to end against a fake provider with a real signing key, including in a real browser across two different
sites. Your first real sign-in is the first real test, and the consent screen may need Google's verification before it works for people outside your org.

### How it works, and what it protects against

* **Authorization code flow with PKCE (S256), run by the server.** The browser is only sent to the provider and back; the client secret never reaches it.
* A short-lived, signed, HttpOnly `SameSite=Lax` cookie holds the one-time `state`, `nonce` and PKCE verifier. The callback must present the matching
  state **from the same browser**, which blocks login CSRF (forcing someone into an attacker's account).
* The provider's **ID token is verified**: signature against its published keys (RS256/ES256 only, never `none` or a shared-secret algorithm), issuer,
  audience, expiry, and the nonce. A token for another app, another issuer, an expired one, a replayed one and a forged one are all refused.
* **The provider must say the email is verified.** An unverified address never becomes an account or a link.
* On success the server sets the **same HttpOnly refresh cookie** as a password sign-in and redirects to `/oauth/complete`, which trades it for an access
  token held in memory. **No token or code is ever in a URL the page can see.** No provider token is stored: only the stable subject id and the email.
* Accounts are matched on the provider's **subject id, never the email**, so a changed or recycled address cannot take over an account.
* **Linking to an existing account by email** happens only because the provider vouches for the email. If the existing account's email was **never
  verified here**, its password is destroyed and its sessions revoked first (this is "pre-hijacking": an attacker registers a victim's address with their
  own password and waits for the victim to use Google). The old password then no longer works; the real owner can set a new one with "Forgot password".
* Social accounts get **no usable password** (a random one nobody knows) and are **not asked for a phone number**; their email counts as verified because
  the provider verified it. `VERIFICATION_MODE=both` therefore applies to password sign-ups only. If you need a verified phone for everyone, social
  sign-in would have to be disabled or a "add your phone" step built.
* Disabled accounts cannot sign in this way; `REGISTRATION_OPEN=false` stops new social accounts too; per-IP rate limits apply to both endpoints.
* Audit events: `oauth_register`, `oauth_link`, `oauth_login`, `oauth_unverified_account_reclaimed`.

### Not included

* **Google/OIDC only.** GitHub and Facebook are not OpenID Connect providers (GitHub needs a different, email-lookup flow) and are not built.
* No "link another provider" or "unlink" screen, and no list of connected accounts. A person who signs in with a second provider using the same
  verified email is linked to the same account automatically.
* The authorization `code` and `state` appear in the callback URL (that is how OAuth works), so they can show in your web server's access logs. They are
  single use and bound to the browser's PKCE verifier, so a logged one cannot be redeemed, but do not log them to places you would not log session data.

## Security properties (each is covered by a test)

* Codes are random (`secrets`), numeric, and stored only as an **HMAC keyed from `JWT_SECRET`**, bound to the challenge, purpose and destination. The code
  never appears in the database, in API responses, or in logs (except the development console sender).
* **5 wrong guesses burn a code.** The attempt counter is advanced atomically *before* the guess is compared, so parallel requests cannot exceed it.
* A code works **once** and expires after 10 minutes. Asking for a new one retires the old one.
* **Resend cooldown** (60 s) and an **hourly cap** (5) per account and channel, counted in the database so they hold across every gateway. Per-IP limits
  (shared through Redis) cover sign-up, resend and reset on top.
* A half-finished sign-up holds a **verification token**: short-lived, single-purpose, and **not accepted as an access token** (and vice versa). Signing in
  to an unverified account issues no session and no refresh cookie.
* "Needs verification" is **not counted as a failed login**, so people are never locked out for being unverified.
* **Forgot password gives the same answer** whether or not the account exists, and only sends to a contact that account has already verified. A reset
  code cannot be used to verify, nor the reverse. A successful reset revokes every refresh token.
* An **abandoned, never-verified sign-up can be redone** (for example with a corrected phone number): it holds nothing reachable without verification. A
  **verified** email or number cannot be taken over.
* Delivery runs **after the database commit** and never changes the API's answer: a failing provider is logged (masked destination, no code, no provider
  body) and counted in `otp_delivery_total{result="error"}`; the person simply asks again. The alert `VerificationCodesNotDelivering` watches it.
* Audit log events: `register`, `otp_resent`, `otp_failed`, `contact_verified`, `password_reset`.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/auth/options` | What the form should ask for (`verification`, `channels`, `registration_open`). Public; names no account. |
| `POST /api/auth/register` `{email, password, phone?}` | `202` with `verification_token`, `pending`, masked `destinations`; or `201` with a session when verification is off |
| `POST /api/auth/verify` `{verification_token, channel, code}` | Check one code; signs in when the last one is done |
| `POST /api/auth/resend` `{verification_token, channel}` | New code (cooldown and hourly cap apply; `429` with `Retry-After`) |
| `POST /api/auth/login` | `403 verification_required` (with a token and pending channels) if contacts are unverified |
| `POST /api/auth/password/forgot` `{email, channel}` | Always `202` |
| `POST /api/auth/password/reset` `{email, channel, code, new_password}` | Sets the password and signs the account out everywhere |

Phone numbers are **E.164** (`+919876543210`); the web form builds that from the country picker.

## Not included (be aware)

* **No way to add or change a phone number after sign-up.** Older accounts have none, and are not asked for one.
* **Not two-factor authentication.** Codes verify contact details at sign-up and reset passwords; they are not asked at every login. TOTP or per-login OTP
  would be a separate feature.
* No CAPTCHA or bot challenge on sign-up; the per-IP and per-account limits are the defence. SMS pumping (an attacker triggering many paid texts) is a real
  cost risk with any OTP system: keep the IP limits, watch your provider's spend alerts, and consider geo-permissions on your SMS account.
* Disposable-email and VoIP-number detection is not done.
* Delivery has been tested against local stand-ins only. The first real send through your provider is the first real test.
