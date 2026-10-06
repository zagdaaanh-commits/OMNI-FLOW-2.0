"""One-time 6-digit email codes for sign-up and password reset.

* A code is stored only as an HMAC (keyed with ``APP_SECRET_KEY``) together with its email,
  purpose, expiry and failed-attempt count; one pending code per (email, purpose).
* Requesting a new code replaces the old one. Codes can be re-requested after
  ``VERIFICATION_RESEND_SECONDS`` (default 60) and expire after ``VERIFICATION_CODE_TTL_SECONDS``
  (default 600). ``VERIFICATION_MAX_ATTEMPTS`` (default 5) wrong guesses invalidate the code.
* A correct code is consumed atomically, so it works exactly once.
* Without SMTP (``app.mailer``) production refuses to issue codes; development and tests log
  the code instead so the flow can be tried locally.

Errors are ``HTTPException`` with ``detail = {"code": ..., "message": ...}``.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
from datetime import timedelta
from typing import Any, Dict, Optional

from fastapi import HTTPException

from app import mailer
from app.config import env_bool, env_int, is_production
from app.plans import parse_time, utcnow
from app.security import get_secret_key

logger = logging.getLogger("omniflow.verification")

PURPOSE_REGISTER = "register"
PURPOSE_RESET_PASSWORD = "reset_password"
PURPOSES = (PURPOSE_REGISTER, PURPOSE_RESET_PASSWORD)

CODE_RECENTLY_SENT = "CODE_RECENTLY_SENT"
CODE_REQUIRED = "CODE_REQUIRED"
CODE_INVALID = "CODE_INVALID"
CODE_EXPIRED = "CODE_EXPIRED"
CODE_ATTEMPTS_EXCEEDED = "CODE_ATTEMPTS_EXCEEDED"
EMAIL_NOT_CONFIGURED = "EMAIL_NOT_CONFIGURED"
EMAIL_DELIVERY_FAILED = "EMAIL_DELIVERY_FAILED"


def code_ttl_seconds() -> int:
    return max(60, env_int("VERIFICATION_CODE_TTL_SECONDS", 600))


def resend_seconds() -> int:
    return max(0, env_int("VERIFICATION_RESEND_SECONDS", 60))


def max_attempts() -> int:
    return max(1, env_int("VERIFICATION_MAX_ATTEMPTS", 5))


def registration_requires_code() -> bool:
    """``EMAIL_VERIFICATION_REQUIRED=false`` lets /auth/register create accounts without a code."""
    return env_bool("EMAIL_VERIFICATION_REQUIRED", True)


def _error(status_code: int, code: str, message: str, headers: Optional[Dict[str, str]] = None) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message}, headers=headers)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_code(email: str, purpose: str, code: str) -> str:
    message = f"{purpose}:{normalize_email(email)}:{code.strip()}".encode("utf-8")
    return hmac.new(get_secret_key(), message, hashlib.sha256).hexdigest()


def generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


_SUBJECTS = {
    PURPOSE_REGISTER: ("OmniFlow 注册验证码：{code}", "Your OmniFlow sign-up code: {code}"),
    PURPOSE_RESET_PASSWORD: ("OmniFlow 重置密码验证码：{code}", "Your OmniFlow password reset code: {code}"),
}
_BODIES = {
    PURPOSE_REGISTER: (
        "你的 OmniFlow 注册验证码是：{code}\n\n验证码 {minutes} 分钟内有效。如果这不是你本人的操作，请忽略此邮件。",
        "Your OmniFlow sign-up code is: {code}\n\nIt expires in {minutes} minutes. "
        "If you didn't try to create an account, you can ignore this email.",
    ),
    PURPOSE_RESET_PASSWORD: (
        "你的 OmniFlow 重置密码验证码是：{code}\n\n验证码 {minutes} 分钟内有效。如果你没有申请重置密码，请忽略此邮件，你的密码不会改变。",
        "Your OmniFlow password reset code is: {code}\n\nIt expires in {minutes} minutes. "
        "If you didn't ask to reset your password, ignore this email; your password stays the same.",
    ),
}


def _deliver(email: str, purpose: str, code: str, lang: str) -> None:
    index = 1 if lang == "en" else 0
    minutes = code_ttl_seconds() // 60
    subject = _SUBJECTS[purpose][index].format(code=code)
    body = _BODIES[purpose][index].format(code=code, minutes=minutes)
    if mailer.is_configured():
        mailer.send_email(email, subject, body)
        return
    # Only reachable outside production (issue_code refuses there): lets the flow be tried locally.
    logger.warning("SMTP is not configured; %s code for %s is %s (development only)", purpose, email, code)


def issue_code(store: Any, email: str, purpose: str, lang: Optional[str] = None) -> Dict[str, int]:
    """Create, store and email a new code; returns ``{"expires_in", "resend_in"}`` in seconds."""
    if purpose not in PURPOSES:
        raise ValueError(f"Unknown verification purpose: {purpose}")
    if is_production() and not mailer.is_configured():
        raise _error(503, EMAIL_NOT_CONFIGURED, "Email delivery is not configured on this server.")
    email = normalize_email(email)
    now = utcnow()

    pending = store.get_verification_code(email=email, purpose=purpose)
    sent_at = parse_time(pending.get("created_at")) if pending else None
    if sent_at is not None:
        remaining = (sent_at + timedelta(seconds=resend_seconds()) - now).total_seconds()
        if remaining > 0:
            wait = math.ceil(remaining)
            raise _error(
                429, CODE_RECENTLY_SENT, f"A code was just sent. Try again in {wait} seconds.", {"Retry-After": str(wait)}
            )

    code = generate_code()
    code_hash = hash_code(email, purpose, code)
    store.save_verification_code(
        email=email, purpose=purpose, code_hash=code_hash, expires_at=now + timedelta(seconds=code_ttl_seconds())
    )
    try:
        _deliver(email, purpose, code, "en" if lang == "en" else "zh")
    except mailer.MailDeliveryError as exc:
        # Nothing reached the inbox: drop the code so the merchant can retry right away.
        store.delete_verification_code(email=email, purpose=purpose, code_hash=code_hash)
        raise _error(502, EMAIL_DELIVERY_FAILED, "The verification email could not be sent. Please try again.") from exc
    return {"expires_in": code_ttl_seconds(), "resend_in": resend_seconds()}


def consume_code(store: Any, email: str, purpose: str, code: Optional[str]) -> None:
    """Check ``code`` and use it up; raises ``HTTPException`` (400 / 429) when it is not valid."""
    email = normalize_email(email)
    code = (code or "").strip()
    if not code:
        raise _error(400, CODE_REQUIRED, "Enter the verification code sent to your email.")
    pending = store.get_verification_code(email=email, purpose=purpose)
    if not pending:
        raise _error(400, CODE_EXPIRED, "This verification code has expired. Request a new one.")
    expires_at = parse_time(pending.get("expires_at"))
    if expires_at is None or expires_at <= utcnow():
        store.delete_verification_code(email=email, purpose=purpose, code_hash=pending["code_hash"])
        raise _error(400, CODE_EXPIRED, "This verification code has expired. Request a new one.")
    if int(pending.get("attempts") or 0) >= max_attempts():
        raise _error(429, CODE_ATTEMPTS_EXCEEDED, "Too many incorrect attempts. Request a new code.")
    if not hmac.compare_digest(pending["code_hash"], hash_code(email, purpose, code)):
        attempts = store.record_verification_failure(email=email, purpose=purpose)
        if attempts >= max_attempts():
            raise _error(429, CODE_ATTEMPTS_EXCEEDED, "Too many incorrect attempts. Request a new code.")
        raise _error(400, CODE_INVALID, "The verification code is incorrect.")
    # Delete-if-unchanged: of two concurrent requests with the same code only one succeeds.
    if not store.delete_verification_code(email=email, purpose=purpose, code_hash=pending["code_hash"]):
        raise _error(400, CODE_EXPIRED, "This verification code has expired. Request a new one.")
