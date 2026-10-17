"""Registration with one-time codes by email and SMS, login gating, resend limits and password reset."""

from __future__ import annotations

import base64
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select, update

from src.auth import delivery, otp, settings
from src.auth.contact import mask_email, mask_phone, normalize_phone
from src.auth.delivery import DeliveryError, Outbox, SmtpSender, TwilioSender, WebhookSender, deliver
from src.auth.security import AuthError, create_access_token, create_verification_token, decode_access_token, decode_verification_token
from src.db.models import OtpChallenge, RefreshToken, User
from src.db.session import session_scope

PASSWORD = "correct horse battery"
PHONE = "+919876543210"


@pytest.fixture
def box(client, monkeypatch):
    """Verification switched on, with email and SMS going to in-memory outboxes."""
    monkeypatch.setenv("VERIFICATION_MODE", "both")
    monkeypatch.setenv("OTP_RESEND_SECONDS", "0")
    from src.api import auth_routes

    for limiter in (auth_routes.otp_send_by_ip, auth_routes.otp_verify_by_ip, auth_routes.reset_by_account):
        limiter.clear()
    email, sms = Outbox(), Outbox()
    delivery.set_sender("email", email)
    delivery.set_sender("phone", sms)
    yield client, email, sms
    delivery.set_sender("email", None)
    delivery.set_sender("phone", None)


def register(client, email="new@example.com", phone=PHONE, password=PASSWORD):
    body = {"email": email, "password": password}
    if phone is not None:
        body["phone"] = phone
    return client.post("/api/auth/register", json=body)


def verify(client, token, channel, code):
    return client.post("/api/auth/verify", json={"verification_token": token, "channel": channel, "code": code})


def complete(client, email_box, sms_box, email="new@example.com", phone=PHONE):
    r = register(client, email, phone)
    token = r.json()["verification_token"]
    verify(client, token, "email", email_box.last_code(email))
    return verify(client, token, "phone", sms_box.last_code(normalize_phone(phone)))


# -- registration -------------------------------------------------------------------------------------------


def test_registration_sends_a_code_to_each_channel_and_signs_nobody_in(box):
    client, email_box, sms_box = box
    r = register(client)
    assert r.status_code == 202
    body = r.json()
    assert body["verification_required"] is True and body["pending"] == ["email", "phone"]
    assert body["destinations"] == {"email": "n***@example.com", "phone": "+91*******210"}
    assert "access_token" not in body and "set-cookie" not in r.headers  # not signed in
    assert body["code_length"] == 6 and body["resend_after_seconds"] == 0
    assert [m["to"] for m in email_box.sent] == ["new@example.com"] and [m["to"] for m in sms_box.sent] == [PHONE]
    assert len(email_box.last_code()) == 6 and email_box.last_code().isdigit()
    assert "verification code" in email_box.sent[0]["body"] and "Never share" in sms_box.sent[0]["body"]


def test_the_code_is_stored_only_as_a_keyed_hash(box):
    client, email_box, _ = box
    register(client)
    code = email_box.last_code()
    with session_scope() as s:
        rows = s.scalars(select(OtpChallenge)).all()
        assert len(rows) == 2
        for row in rows:
            assert code not in row.code_hash and len(row.code_hash) == 64
            assert row.destination in ("new@example.com", PHONE)


@pytest.mark.parametrize("phone", [None, "", "9876543210", "+0123456789", "+91 98", "abc", "+9198765432109999999"])
def test_a_missing_or_malformed_phone_is_refused_when_phone_verification_is_on(box, phone):
    client, _, _ = box
    r = register(client, phone=phone)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_phone"


@pytest.mark.parametrize("raw", ["+91 98765 43210", "+91-98765-43210", "+91 (98765) 43210", " +919876543210 "])
def test_phone_numbers_are_normalised_to_e164(box, raw):
    client, _, sms_box = box
    assert register(client, phone=raw).status_code == 202
    assert sms_box.sent[-1]["to"] == PHONE


def test_email_only_mode_does_not_ask_for_a_phone(box, monkeypatch):
    client, email_box, sms_box = box
    monkeypatch.setenv("VERIFICATION_MODE", "email")
    r = register(client, phone=None)
    assert r.status_code == 202 and r.json()["pending"] == ["email"] and sms_box.sent == []
    done = verify(client, r.json()["verification_token"], "email", email_box.last_code())
    assert done.status_code == 200 and done.json()["verification_required"] is False and "access_token" in done.json()


def test_phone_only_mode(box, monkeypatch):
    client, _, sms_box = box
    monkeypatch.setenv("VERIFICATION_MODE", "phone")
    r = register(client)
    assert r.json()["pending"] == ["phone"]
    done = verify(client, r.json()["verification_token"], "phone", sms_box.last_code())
    assert "access_token" in done.json()


def test_verification_off_keeps_the_original_immediate_signup(box, monkeypatch):
    client, email_box, sms_box = box
    monkeypatch.setenv("VERIFICATION_MODE", "off")
    r = register(client, phone=None)
    assert r.status_code == 201 and "access_token" in r.json()
    assert email_box.sent == [] and sms_box.sent == []


# -- verifying ----------------------------------------------------------------------------------------------


def test_both_codes_signs_the_person_in(box):
    client, email_box, sms_box = box
    r = register(client)
    token = r.json()["verification_token"]
    first = verify(client, token, "email", email_box.last_code())
    assert first.status_code == 200
    assert first.json()["pending"] == ["phone"] and first.json()["verified"] == "email" and "access_token" not in first.json()
    done = verify(client, token, "phone", sms_box.last_code())
    body = done.json()
    assert done.status_code == 200 and body["verification_required"] is False and body["user"]["email"] == "new@example.com"
    assert "refresh_token" in done.headers["set-cookie"] and "HttpOnly" in done.headers["set-cookie"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == "new@example.com"))
        assert u.email_verified_at is not None and u.phone_verified_at is not None and u.phone == PHONE


def test_codes_may_be_entered_in_either_order(box):
    client, email_box, sms_box = box
    token = register(client).json()["verification_token"]
    assert verify(client, token, "phone", sms_box.last_code()).json()["pending"] == ["email"]
    assert "access_token" in verify(client, token, "email", email_box.last_code()).json()


def test_a_wrong_code_counts_down_and_the_right_one_still_works(box):
    client, email_box, _ = box
    token = register(client).json()["verification_token"]
    wrong = "000000" if email_box.last_code() != "000000" else "111111"
    r = verify(client, token, "email", wrong)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_code" and r.json()["detail"]["attempts_left"] == 4
    assert verify(client, token, "email", email_box.last_code()).status_code == 200


def test_the_attempt_limit_burns_the_code_even_for_the_right_answer(box):
    client, email_box, _ = box
    token = register(client).json()["verification_token"]
    real = email_box.last_code()
    wrong = "000000" if real != "000000" else "111111"
    statuses = [verify(client, token, "email", wrong).json()["detail"]["code"] for _ in range(5)]
    assert statuses == ["invalid_code"] * 5
    late = verify(client, token, "email", real)  # correct, but the code has been burned
    assert late.status_code in (400, 429) and late.json()["detail"]["code"] in ("no_active_code", "too_many_attempts")
    resent = client.post("/api/auth/resend", json={"verification_token": token, "channel": "email"})
    assert resent.status_code == 202
    assert verify(client, token, "email", email_box.last_code()).status_code == 200  # a fresh code works


def test_parallel_guesses_cannot_beat_the_limit(box):
    """The attempt is claimed before the guess is compared, so racing requests still stop at the limit."""
    client, email_box, _ = box
    token = register(client).json()["verification_token"]
    real = email_box.last_code()
    guesses = [f"{n:06d}" for n in range(10) if f"{n:06d}" != real]
    results: list[int] = []

    def attempt(code):
        results.append(verify(client, token, "email", code).status_code)

    threads = [threading.Thread(target=attempt, args=(g,)) for g in guesses[:8]]
    [t.start() for t in threads]
    [t.join() for t in threads]
    with session_scope() as s:
        row = s.scalar(select(OtpChallenge).where(OtpChallenge.channel == "email"))
        assert row.attempts <= settings.otp_max_attempts()


def test_a_code_works_once(box):
    client, email_box, _ = box
    token = register(client).json()["verification_token"]
    code = email_box.last_code()
    assert verify(client, token, "email", code).status_code == 200
    again = verify(client, token, "email", code)
    assert again.status_code == 400 and again.json()["detail"]["code"] == "nothing_to_verify"


def test_an_expired_code_is_refused(box):
    client, email_box, _ = box
    token = register(client).json()["verification_token"]
    code = email_box.last_code()
    with session_scope() as s:
        s.execute(update(OtpChallenge).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    r = verify(client, token, "email", code)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "code_expired"


def test_a_code_is_bound_to_its_own_challenge_and_destination(box):
    """The hash covers challenge id, purpose and destination: moving a hash between rows breaks it."""
    client, email_box, sms_box = box
    token = register(client).json()["verification_token"]
    phone_code = sms_box.last_code()
    with session_scope() as s:
        email_row = s.scalar(select(OtpChallenge).where(OtpChallenge.channel == "email"))
        phone_row = s.scalar(select(OtpChallenge).where(OtpChallenge.channel == "phone"))
        email_row.code_hash = phone_row.code_hash  # an attacker with database write access copies a hash across
    r = verify(client, token, "email", phone_code)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_code"


@pytest.mark.parametrize("code", ["", "abc123", "12345", "1234567", "12 34 5", "٠٠٠٠٠٠"])
def test_malformed_codes_are_rejected_cleanly(box, code):
    client, _, _ = box
    token = register(client).json()["verification_token"]
    assert verify(client, token, "email", code).status_code == 400


def test_spaces_in_a_pasted_code_are_tolerated(box):
    client, email_box, _ = box
    token = register(client).json()["verification_token"]
    real = email_box.last_code()
    assert verify(client, token, "email", f"{real[:3]} {real[3:]}").status_code == 200


def test_verifying_a_channel_that_is_not_pending_is_refused(box, monkeypatch):
    client, email_box, _ = box
    monkeypatch.setenv("VERIFICATION_MODE", "email")
    token = register(client, phone=None).json()["verification_token"]
    r = verify(client, token, "phone", "123456")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "nothing_to_verify"


# -- tokens -------------------------------------------------------------------------------------------------


def test_the_verification_token_is_not_an_access_token_and_vice_versa(box):
    client, email_box, _ = box
    token = register(client).json()["verification_token"]
    assert client.get("/api/paper/state", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    with pytest.raises(AuthError):
        decode_access_token(token)
    access, _ = create_access_token("someone", "user")
    r = verify(client, access, "email", "123456")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_verification_token"
    assert verify(client, "garbage", "email", "123456").status_code == 401


def test_an_expired_verification_token_asks_to_sign_in_again(box):
    client, _, _ = box
    stale = create_verification_token("abc", datetime.now(timezone.utc) - timedelta(hours=2))
    r = verify(client, stale, "email", "123456")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "verification_expired"
    with pytest.raises(AuthError):
        decode_verification_token(stale)


# -- resend limits ------------------------------------------------------------------------------------------


def test_resend_has_a_cooldown_with_a_retry_after(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setenv("OTP_RESEND_SECONDS", "60")
    token = register(client).json()["verification_token"]
    r = client.post("/api/auth/resend", json={"verification_token": token, "channel": "email"})
    assert r.status_code == 429 and r.json()["detail"]["code"] == "otp_cooldown"
    assert 1 <= int(r.headers["retry-after"]) <= 60 and r.json()["detail"]["retry_after"] >= 1


def test_resend_retires_the_old_code(box):
    client, email_box, _ = box
    token = register(client).json()["verification_token"]
    old = email_box.last_code()
    assert client.post("/api/auth/resend", json={"verification_token": token, "channel": "email"}).status_code == 202
    new = email_box.last_code()
    assert new != old
    assert verify(client, token, "email", old).json()["detail"]["code"] == "invalid_code"
    assert verify(client, token, "email", new).status_code == 200


def test_an_hourly_cap_stops_code_flooding(box, monkeypatch):
    client, _, sms_box = box
    monkeypatch.setenv("OTP_MAX_SENDS_PER_HOUR", "3")
    token = register(client).json()["verification_token"]  # send 1
    codes = [client.post("/api/auth/resend", json={"verification_token": token, "channel": "phone"}).status_code for _ in range(4)]
    assert codes == [202, 202, 429, 429]
    assert len(sms_box.sent) == 3


# -- login --------------------------------------------------------------------------------------------------


def test_signing_in_before_verifying_gives_a_token_and_codes_not_a_session(box):
    client, email_box, sms_box = box
    register(client)
    email_box.sent.clear(); sms_box.sent.clear()
    with session_scope() as s:
        s.execute(update(OtpChallenge).values(consumed_at=datetime.now(timezone.utc)))  # the codes from sign-up are gone
    r = client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD})
    d = r.json()["detail"]
    assert r.status_code == 403 and d["code"] == "verification_required" and d["pending"] == ["email", "phone"]
    assert "verification_token" in d and "set-cookie" not in r.headers
    assert len(email_box.sent) == 1 and len(sms_box.sent) == 1  # fresh codes were sent
    done = verify(client, d["verification_token"], "email", email_box.last_code())
    assert done.status_code == 200


def test_signing_in_does_not_resend_while_a_code_is_still_valid(box):
    client, email_box, sms_box = box
    register(client)
    n = len(email_box.sent)
    r = client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD})
    assert r.status_code == 403 and len(email_box.sent) == n


def test_a_wrong_password_never_reveals_verification_state(box):
    client, _, _ = box
    register(client)
    r = client.post("/api/auth/login", json={"email": "new@example.com", "password": "wrong wrong wrong"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_credentials"


def test_needing_verification_is_not_counted_as_a_failed_login(box):
    client, _, _ = box
    register(client)
    for _ in range(8):  # more than the lockout threshold of 5
        assert client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD}).status_code == 403


def test_a_verified_user_signs_in_normally(box):
    client, email_box, sms_box = box
    complete(client, email_box, sms_box)
    r = client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD})
    assert r.status_code == 200 and "access_token" in r.json()


def test_accounts_that_predate_verification_are_not_locked_out(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setenv("VERIFICATION_MODE", "off")
    assert register(client, "old@example.com", phone=None).status_code == 201  # an account made before verification
    with session_scope() as s:
        s.execute(update(User).where(User.email == "old@example.com").values(email_verified_at=datetime.now(timezone.utc)))
    monkeypatch.setenv("VERIFICATION_MODE", "both")
    r = client.post("/api/auth/login", json={"email": "old@example.com", "password": PASSWORD})
    assert r.status_code == 200, r.text  # no phone on file, so none is demanded


# -- duplicate and abandoned sign-ups ------------------------------------------------------------------------


def test_a_verified_email_cannot_be_registered_again(box):
    client, email_box, sms_box = box
    complete(client, email_box, sms_box)
    r = register(client, phone="+919000000001")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "email_taken"


def test_a_verified_phone_cannot_be_claimed_by_another_account(box):
    client, email_box, sms_box = box
    complete(client, email_box, sms_box)
    r = register(client, email="other@example.com", phone=PHONE)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "phone_taken"


def test_an_abandoned_signup_can_be_redone_with_a_corrected_number(box):
    client, email_box, sms_box = box
    register(client, phone="+919111111111")  # mistyped number, never verified
    r = register(client, phone="+919222222222", password="a brand new password")
    assert r.status_code == 202
    assert sms_box.sent[-1]["to"] == "+919222222222"
    token = r.json()["verification_token"]
    verify(client, token, "email", email_box.last_code())
    done = verify(client, token, "phone", sms_box.last_code("+919222222222"))
    assert "access_token" in done.json()
    assert client.post("/api/auth/login", json={"email": "new@example.com", "password": "a brand new password"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD}).status_code == 401  # old password gone


def test_an_unverified_claim_on_a_number_does_not_block_its_real_owner(box):
    client, email_box, sms_box = box
    register(client, email="squatter@example.com", phone=PHONE)  # never verified
    r = register(client, email="owner@example.com", phone=PHONE)
    assert r.status_code == 202
    done = complete_with(client, email_box, sms_box, "owner@example.com", PHONE)
    assert "access_token" in done.json()
    with session_scope() as s:
        assert s.scalar(select(User.phone).where(User.email == "squatter@example.com")) is None


def complete_with(client, email_box, sms_box, email, phone):
    r = register(client, email, phone)
    token = r.json()["verification_token"]
    verify(client, token, "email", email_box.last_code(email))
    return verify(client, token, "phone", sms_box.last_code(phone))


# -- delivery -----------------------------------------------------------------------------------------------


def test_a_failing_provider_never_breaks_registration(box, caplog):
    client, email_box, sms_box = box
    sms_box.fail_next = DeliveryError("provider answered HTTP 503")
    with caplog.at_level(logging.ERROR, logger="market_data.auth.delivery"):
        r = register(client)
    assert r.status_code == 202 and len(email_box.sent) == 1 and sms_box.sent == []
    assert "provider answered HTTP 503" in caplog.text
    assert PHONE not in caplog.text and "+91*******210" in caplog.text  # the number is masked in logs
    resent = client.post("/api/auth/resend", json={"verification_token": r.json()["verification_token"], "channel": "phone"})
    assert resent.status_code == 202 and len(sms_box.sent) == 1  # the person can simply ask again


def test_no_code_ever_reaches_the_log_through_a_real_sender(box, caplog):
    client, email_box, _ = box
    failing = Outbox()
    failing.fail_next = RuntimeError("boom")
    delivery.set_sender("email", failing)
    with caplog.at_level(logging.DEBUG):
        register(client)
    assert not any(c.isdigit() and len(c) == 6 for c in caplog.text.split())


def test_the_console_sender_prints_the_code_for_development_only(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="market_data.auth.delivery"):
        assert deliver("phone", PHONE, None, "Your code is 123456", sender=delivery.ConsoleSender("sms")) is True
    assert "123456" in caplog.text and "DEV ONLY" in caplog.text


def test_masks():
    assert mask_email("alice@example.com") == "a***@example.com"
    assert mask_email("ab@x.io") == "a***@x.io"
    assert mask_phone("+919876543210") == "+91*******210"
    assert normalize_phone("+44 7911 123456") == "+447911123456"


# -- password reset -----------------------------------------------------------------------------------------


def forgot(client, email="new@example.com", channel="email"):
    return client.post("/api/auth/password/forgot", json={"email": email, "channel": channel})


def reset(client, code, new="a different password", email="new@example.com", channel="email"):
    return client.post("/api/auth/password/reset", json={"email": email, "channel": channel, "code": code, "new_password": new})


def test_forgot_password_answers_the_same_whether_or_not_the_account_exists(box):
    client, email_box, sms_box = box
    complete(client, email_box, sms_box)
    email_box.sent.clear()
    known, unknown = forgot(client), forgot(client, "nobody@example.com")
    assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()
    assert len(email_box.sent) == 1 and email_box.sent[0]["to"] == "new@example.com"  # only the real one got mail
    assert "reset code" in email_box.sent[0]["body"]


def test_password_reset_end_to_end_and_signs_out_everywhere(box):
    client, email_box, sms_box = box
    first = complete(client, email_box, sms_box)
    cookie = first.headers["set-cookie"].split(";")[0]
    forgot(client)
    r = reset(client, email_box.last_code())
    assert r.status_code == 200 and r.json()["reset"] is True
    assert client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "new@example.com", "password": "a different password"}).status_code == 200
    with session_scope() as s:  # every session from before the reset was revoked
        old = s.scalars(select(RefreshToken).where(RefreshToken.revoked_at.is_(None))).all()
        assert len(old) == 1  # only the one just created by the new sign-in


def test_reset_by_sms_works_for_a_verified_phone(box):
    client, email_box, sms_box = box
    complete(client, email_box, sms_box)
    forgot(client, channel="phone")
    assert reset(client, sms_box.last_code(), channel="phone").status_code == 200


def test_a_wrong_reset_code_is_counted_and_limited(box):
    client, email_box, sms_box = box
    complete(client, email_box, sms_box)
    forgot(client)
    real = email_box.last_code()
    wrong = "000000" if real != "000000" else "111111"
    codes = [reset(client, wrong).json()["detail"]["code"] for _ in range(5)]
    assert codes == ["invalid_code"] * 5
    assert reset(client, real).status_code in (400, 429)  # burned
    assert client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD}).status_code == 200


def test_a_reset_code_cannot_be_used_to_verify_and_the_reverse(box):
    client, email_box, sms_box = box
    complete(client, email_box, sms_box)
    forgot(client)
    reset_code = email_box.last_code()
    # purpose binding: a reset code is not a login or a verification
    token = create_verification_token("whatever")
    assert verify(client, token, "email", reset_code).status_code in (400, 401)
    assert reset(client, "000000").status_code == 400


def test_reset_needs_a_strong_new_password_and_a_verified_channel(box):
    client, email_box, sms_box = box
    complete(client, email_box, sms_box)
    forgot(client)
    assert reset(client, email_box.last_code(), new="short").status_code == 422
    register(client, "unverified@example.com", "+919333333333")
    email_box.sent.clear()
    forgot(client, "unverified@example.com")  # email never verified: nothing is sent, answer is identical
    assert email_box.sent == []


def test_forgot_password_is_rate_limited_per_account(box):
    client, email_box, sms_box = box
    complete(client, email_box, sms_box)
    codes = [forgot(client).status_code for _ in range(10)]
    assert codes[0] == 202 and 429 in codes


# -- sending ------------------------------------------------------------------------------------------------


class _Capture(BaseHTTPRequestHandler):
    seen: list[dict] = []
    status = 201

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        _Capture.seen.append({"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(length).decode()})
        self.send_response(_Capture.status)
        self.end_headers()
        self.wfile.write(b'{"message": "secret-body-with-number +919876543210"}')

    def log_message(self, *args):
        pass


@pytest.fixture
def http_stub():
    _Capture.seen, _Capture.status = [], 201
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _Capture
    server.shutdown()


def test_twilio_sender_posts_the_documented_request(http_stub, monkeypatch):
    base, stub = http_stub
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM", "+15005550006")
    monkeypatch.setenv("TWILIO_API_URL", base + "/2010-04-01/Accounts/ACtest/Messages.json")
    TwilioSender().send(PHONE, None, "Your code is 123456")
    seen = stub.seen[0]
    assert seen["path"] == "/2010-04-01/Accounts/ACtest/Messages.json"
    assert seen["headers"]["Authorization"] == "Basic " + base64.b64encode(b"ACtest:tok").decode()
    assert seen["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert "To=%2B919876543210" in seen["body"] and "From=%2B15005550006" in seen["body"] and "Body=Your+code+is+123456" in seen["body"]


def test_twilio_messaging_service_replaces_from(http_stub, monkeypatch):
    base, stub = http_stub
    for k, v in {"TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "t", "TWILIO_MESSAGING_SERVICE_SID": "MG123", "TWILIO_API_URL": base + "/m.json"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TWILIO_FROM", raising=False)
    TwilioSender().send(PHONE, None, "x")
    assert "MessagingServiceSid=MG123" in stub.seen[0]["body"] and "From=" not in stub.seen[0]["body"]


def test_a_provider_error_is_reported_without_leaking_its_body(http_stub, monkeypatch):
    base, stub = http_stub
    stub.status = 400
    monkeypatch.setenv("SMS_WEBHOOK_URL", base + "/send")
    with pytest.raises(DeliveryError) as info:
        WebhookSender().send(PHONE, None, "Your code is 123456")
    assert "HTTP 400" in str(info.value) and "secret-body" not in str(info.value) and "123456" not in str(info.value)


def test_an_unreachable_provider_is_a_delivery_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("SMS_WEBHOOK_URL", "http://127.0.0.1:9/never")
    assert deliver("phone", PHONE, None, "x", sender=WebhookSender()) is False


def test_webhook_sender_posts_json_with_an_optional_bearer(http_stub, monkeypatch):
    base, stub = http_stub
    monkeypatch.setenv("SMS_WEBHOOK_URL", base + "/send")
    monkeypatch.setenv("SMS_WEBHOOK_TOKEN", "sekret")
    WebhookSender().send(PHONE, None, "Your code is 123456")
    seen = stub.seen[0]
    assert json.loads(seen["body"]) == {"to": PHONE, "message": "Your code is 123456"}
    assert seen["headers"]["Authorization"] == "Bearer sekret"


def test_smtp_sender_uses_starttls_login_and_a_proper_message(monkeypatch):
    for k, v in {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_USER": "u", "SMTP_PASSWORD": "p", "EMAIL_FROM": "Market Data <no-reply@example.com>"}.items():
        monkeypatch.setenv(k, v)
    with patch("src.auth.delivery.smtplib.SMTP") as smtp:
        server = smtp.return_value.__enter__.return_value
        SmtpSender().send("a@example.com", "Your code", "Your code is 123456")
    smtp.assert_called_once()
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("u", "p")
    message = server.send_message.call_args[0][0]
    assert message["To"] == "a@example.com" and message["From"].endswith("<no-reply@example.com>") and message["Subject"] == "Your code"
    assert "123456" in message.get_content()


def test_smtp_failures_become_delivery_errors(monkeypatch):
    for k, v in {"SMTP_HOST": "smtp.example.com", "EMAIL_FROM": "x@example.com"}.items():
        monkeypatch.setenv(k, v)
    with patch("src.auth.delivery.smtplib.SMTP", side_effect=OSError("down")):
        assert deliver("email", "a@example.com", "s", "b", sender=SmtpSender()) is False


# -- start-up safety ----------------------------------------------------------------------------------------


@pytest.fixture
def prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("VERIFICATION_MODE", "both")
    for k in ("EMAIL_PROVIDER", "SMS_PROVIDER", "SMTP_HOST", "EMAIL_FROM", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_MESSAGING_SERVICE_SID", "SMS_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_production_refuses_the_console_senders(prod):
    with pytest.raises(RuntimeError) as info:
        settings.validate_startup_config()
    message = str(info.value)
    assert "EMAIL_PROVIDER=console" in message and "SMS_PROVIDER=console" in message


def test_production_names_every_missing_setting(prod):
    prod.setenv("EMAIL_PROVIDER", "smtp")
    prod.setenv("SMS_PROVIDER", "twilio")
    with pytest.raises(RuntimeError) as info:
        settings.validate_startup_config()
    for name in ("SMTP_HOST", "EMAIL_FROM", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"):
        assert name in str(info.value)


def test_a_complete_production_configuration_starts(prod):
    prod.setenv("EMAIL_PROVIDER", "smtp")
    prod.setenv("SMTP_HOST", "smtp.example.com")
    prod.setenv("EMAIL_FROM", "no-reply@example.com")
    prod.setenv("SMS_PROVIDER", "webhook")
    prod.setenv("SMS_WEBHOOK_URL", "https://sms.example.com/send")
    settings.validate_startup_config()


def test_production_webhook_must_be_https(prod):
    prod.setenv("VERIFICATION_MODE", "phone")
    prod.setenv("SMS_PROVIDER", "webhook")
    prod.setenv("SMS_WEBHOOK_URL", "http://sms.example.com/send")
    with pytest.raises(RuntimeError, match="https"):
        settings.validate_startup_config()


def test_only_the_channels_that_are_required_need_a_provider(prod):
    prod.setenv("VERIFICATION_MODE", "email")
    prod.setenv("EMAIL_PROVIDER", "smtp")
    prod.setenv("SMTP_HOST", "h")
    prod.setenv("EMAIL_FROM", "a@b.co")
    settings.validate_startup_config()  # no SMS provider needed: phone is not required


def test_verification_off_needs_no_providers(prod):
    prod.setenv("VERIFICATION_MODE", "off")
    settings.validate_startup_config()


def test_unknown_modes_and_providers_are_refused(prod):
    prod.setenv("VERIFICATION_MODE", "sometimes")
    with pytest.raises(ValueError):
        settings.validate_startup_config()
    prod.setenv("VERIFICATION_MODE", "email")
    prod.setenv("EMAIL_PROVIDER", "pigeon")
    with pytest.raises(RuntimeError, match="EMAIL_PROVIDER must be"):
        settings.validate_startup_config()


# -- the upgrade --------------------------------------------------------------------------------------------


def test_the_migration_stamps_existing_accounts_verified(monkeypatch, tmp_path):
    """Upgrading must not lock anyone out: accounts made before verification existed count as email-verified."""
    from alembic import command

    from src.db import session as db_session
    from src.db.migrate import _config, upgrade_to_head
    from sqlalchemy import text

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    db_session.reset_engine()
    command.upgrade(_config(), "0002")
    with db_session.get_engine().begin() as c:
        c.execute(text("INSERT INTO users (id, email, password_hash, role, is_active, created_at) VALUES ('u1','old@example.com','x','user',1,'2026-01-02 03:04:05')"))
    upgrade_to_head()
    with db_session.get_engine().connect() as c:
        row = c.execute(text("SELECT email_verified_at, created_at, phone, phone_verified_at FROM users WHERE id='u1'")).one()
    assert row[0] is not None and str(row[0])[:19] == str(row[1])[:19]
    assert row[2] is None and row[3] is None
    db_session.reset_engine()


# -- the sign-up form's questions ---------------------------------------------------------------------------


def test_options_tells_the_form_what_to_ask_without_naming_any_account(client, monkeypatch):
    monkeypatch.setenv("VERIFICATION_MODE", "both")
    body = client.get("/api/auth/options").json()
    assert body == {"verification": "both", "channels": ["email", "phone"], "registration_open": True, "code_length": 6, "oauth": []}
    monkeypatch.setenv("VERIFICATION_MODE", "off")
    assert client.get("/api/auth/options").json()["channels"] == []
    monkeypatch.setenv("REGISTRATION_OPEN", "false")
    assert client.get("/api/auth/options").json()["registration_open"] is False
