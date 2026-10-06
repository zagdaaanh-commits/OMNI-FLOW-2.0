"""Email verification codes: sign-up (/auth/register/send-code + code) and password reset."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import List, Tuple
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import mailer, verification
from app.main import app, store
from app.plans import utcnow

client = TestClient(app)


@pytest.fixture
def outbox(monkeypatch) -> List[Tuple[str, str, str]]:
    """SMTP is "configured" and every sent email lands here as (to, subject, body)."""
    sent: List[Tuple[str, str, str]] = []
    monkeypatch.setenv("EMAIL_VERIFICATION_REQUIRED", "true")
    monkeypatch.setattr(mailer, "is_configured", lambda: True)
    monkeypatch.setattr(mailer, "send_email", lambda to, subject, text: sent.append((to, subject, text)))
    return sent


def _email() -> str:
    return f"verify_{uuid4().hex[:10]}@example.com"


def _code(outbox, to: str) -> str:
    subject = [s for (rcpt, s, _) in outbox if rcpt == to][-1]
    return re.search(r"\b(\d{6})\b", subject).group(1)


def _register(email: str, code=None, password: str = "Passw0rd!"):
    body = {"email": email, "password": password, "full_name": "Verified", "company": ""}
    if code is not None:
        body["code"] = code
    return client.post("/auth/register", json=body)


# ------------------------------------------------------------------ sign-up
def test_register_requires_a_code_when_verification_is_on(outbox):
    res = _register(_email())
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == verification.CODE_REQUIRED


def test_register_with_the_emailed_code_signs_the_user_in(outbox):
    email = _email()
    sent = client.post("/auth/register/send-code", json={"email": email, "lang": "en"})
    assert sent.status_code == 200, sent.text
    assert sent.json()["expires_in"] == 600 and sent.json()["resend_in"] == 60
    assert len(outbox) == 1 and outbox[0][0] == email
    assert "sign-up code" in outbox[0][1]

    code = _code(outbox, email)
    wrong = "000000" if code != "000000" else "111111"
    bad = _register(email, wrong)
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == verification.CODE_INVALID

    res = _register(email, code)
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["email"] == email
    # The code is single-use.
    assert store.get_verification_code(email=email, purpose=verification.PURPOSE_REGISTER) is None


def test_code_is_stored_only_as_a_hash(outbox):
    email = _email()
    client.post("/auth/register/send-code", json={"email": email})
    row = store.get_verification_code(email=email, purpose=verification.PURPOSE_REGISTER)
    code = _code(outbox, email)
    assert code not in row["code_hash"]
    assert row["code_hash"] == verification.hash_code(email, verification.PURPOSE_REGISTER, code)
    assert "注册验证码" in outbox[-1][1]  # Chinese email unless lang=en


def test_send_code_refuses_an_already_registered_email(outbox):
    email = _email()
    client.post("/auth/register/send-code", json={"email": email})
    assert _register(email, _code(outbox, email)).status_code == 200
    res = client.post("/auth/register/send-code", json={"email": email.upper()})
    assert res.status_code == 400
    assert len(outbox) == 1


def test_send_code_rejects_a_malformed_email(outbox):
    assert client.post("/auth/register/send-code", json={"email": "not-an-email"}).status_code == 422
    assert outbox == []


def test_resend_is_rate_limited_and_replaces_the_old_code(outbox, monkeypatch):
    email = _email()
    assert client.post("/auth/register/send-code", json={"email": email}).status_code == 200
    again = client.post("/auth/register/send-code", json={"email": email})
    assert again.status_code == 429
    assert again.json()["detail"]["code"] == verification.CODE_RECENTLY_SENT
    assert 1 <= int(again.headers["Retry-After"]) <= 60
    assert len(outbox) == 1

    first = _code(outbox, email)
    monkeypatch.setenv("VERIFICATION_RESEND_SECONDS", "0")
    assert client.post("/auth/register/send-code", json={"email": email}).status_code == 200
    second = _code(outbox, email)
    if first != second:
        assert _register(email, first).json()["detail"]["code"] == verification.CODE_INVALID
    assert _register(email, second).status_code == 200


def test_too_many_wrong_codes_lock_the_code(outbox):
    email = _email()
    client.post("/auth/register/send-code", json={"email": email})
    code = _code(outbox, email)
    wrong = "000000" if code != "000000" else "111111"
    statuses = [_register(email, wrong).status_code for _ in range(verification.max_attempts())]
    assert statuses[:-1] == [400] * (verification.max_attempts() - 1) and statuses[-1] == 429
    locked = _register(email, code)
    assert locked.status_code == 429
    assert locked.json()["detail"]["code"] == verification.CODE_ATTEMPTS_EXCEEDED


def test_expired_code_is_rejected(outbox):
    email = _email()
    code = "123456"
    store.save_verification_code(
        email=email,
        purpose=verification.PURPOSE_REGISTER,
        code_hash=verification.hash_code(email, verification.PURPOSE_REGISTER, code),
        expires_at=utcnow() - timedelta(seconds=1),
    )
    res = _register(email, code)
    assert res.status_code == 400 and res.json()["detail"]["code"] == verification.CODE_EXPIRED


def test_register_code_cannot_reset_a_password(outbox):
    """Codes are bound to their purpose."""
    email = _email()
    client.post("/auth/register/send-code", json={"email": email})
    code = _code(outbox, email)
    res = client.post("/auth/password/reset", json={"email": email, "code": code, "new_password": "NewPassw0rd!"})
    assert res.status_code == 400


def test_failed_delivery_returns_502_and_allows_an_immediate_retry(outbox, monkeypatch):
    def broken(to, subject, text):
        raise mailer.MailDeliveryError("SMTPServerDisconnected")

    monkeypatch.setattr(mailer, "send_email", broken)
    email = _email()
    res = client.post("/auth/register/send-code", json={"email": email})
    assert res.status_code == 502 and res.json()["detail"]["code"] == verification.EMAIL_DELIVERY_FAILED
    assert store.get_verification_code(email=email, purpose=verification.PURPOSE_REGISTER) is None

    monkeypatch.setattr(mailer, "send_email", lambda to, subject, text: outbox.append((to, subject, text)))
    assert client.post("/auth/register/send-code", json={"email": email}).status_code == 200


def test_production_without_smtp_refuses_to_issue_codes(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(mailer, "is_configured", lambda: False)
    res = client.post("/auth/register/send-code", json={"email": _email()})
    assert res.status_code == 503 and res.json()["detail"]["code"] == verification.EMAIL_NOT_CONFIGURED


def test_development_without_smtp_logs_the_code(monkeypatch, caplog):
    monkeypatch.setattr(mailer, "is_configured", lambda: False)
    email = _email()
    with caplog.at_level("WARNING", logger="omniflow.verification"):
        assert client.post("/auth/register/send-code", json={"email": email}).status_code == 200
    assert any(email in r.getMessage() and re.search(r"\b\d{6}\b", r.getMessage()) for r in caplog.records)


# ----------------------------------------------------------- password reset
def _existing_user(outbox) -> str:
    email = _email()
    client.post("/auth/register/send-code", json={"email": email})
    assert _register(email, _code(outbox, email)).status_code == 200
    return email


def test_password_reset_with_the_emailed_code(outbox):
    email = _existing_user(outbox)
    sent = client.post("/auth/password/send-code", json={"email": email, "lang": "en"})
    assert sent.status_code == 200
    assert "password reset code" in outbox[-1][1]
    code = _code(outbox, email)

    res = client.post("/auth/password/reset", json={"email": email, "code": code, "new_password": "BrandNew123"})
    assert res.status_code == 200, res.text
    assert res.json()["user"]["email"] == email and "password_hash" not in res.json()["user"]
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {res.json()['token']}"}).status_code == 200

    assert client.post("/auth/login", json={"email": email, "password": "Passw0rd!"}).status_code == 401
    assert client.post("/auth/login", json={"email": email, "password": "BrandNew123"}).status_code == 200
    # Single use.
    again = client.post("/auth/password/reset", json={"email": email, "code": code, "new_password": "Another123"})
    assert again.status_code == 400


def test_password_reset_code_for_an_unknown_email_looks_the_same_but_sends_nothing(outbox):
    before = len(outbox)
    res = client.post("/auth/password/send-code", json={"email": _email()})
    assert res.status_code == 200 and res.json()["status"] == "sent"
    assert len(outbox) == before


def test_password_reset_rejects_wrong_code_and_short_password(outbox):
    email = _existing_user(outbox)
    client.post("/auth/password/send-code", json={"email": email})
    code = _code(outbox, email)
    wrong = "000000" if code != "000000" else "111111"
    bad = client.post("/auth/password/reset", json={"email": email, "code": wrong, "new_password": "BrandNew123"})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == verification.CODE_INVALID
    short = client.post("/auth/password/reset", json={"email": email, "code": code, "new_password": "short"})
    assert short.status_code == 422
    assert client.post("/auth/login", json={"email": email, "password": "Passw0rd!"}).status_code == 200


# ------------------------------------------------------------------- mailer
class _FakeSMTP:
    instances: List["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.calls, self.messages = host, port, [], []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message):
        self.messages.append(message)


def test_mailer_uses_starttls_and_login(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "noreply@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret")
    assert mailer.is_configured()

    mailer.send_email("merchant@example.com", "Subject 123456", "Body")
    smtp = _FakeSMTP.instances[-1]
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.calls == ["starttls", ("login", "noreply@example.com", "smtp-secret")]
    sent = smtp.messages[0]
    assert sent["To"] == "merchant@example.com" and sent["Subject"] == "Subject 123456"
    assert "noreply@example.com" in sent["From"]


def test_mailer_uses_implicit_tls_on_port_465(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr("smtplib.SMTP_SSL", _FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_FROM", "OmniFlow <noreply@example.com>")
    mailer.send_email("merchant@example.com", "Subject", "Body")
    smtp = _FakeSMTP.instances[-1]
    assert smtp.port == 465 and smtp.calls == []  # no STARTTLS, no login without a username


def test_mailer_wraps_smtp_errors(monkeypatch):
    def refuse(*args, **kwargs):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr("smtplib.SMTP", refuse)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    with pytest.raises(mailer.MailDeliveryError):
        mailer.send_email("merchant@example.com", "Subject", "Body")
