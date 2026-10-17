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
