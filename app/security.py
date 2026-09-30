"""Stateless HMAC-SHA256 signed tokens (OAuth ``state`` values and API bearer tokens).

Format: ``<base64url(json payload)>.<base64url(hmac)>`` with ``iat``/``exp`` claims.
Stdlib only; constant-time signature comparison.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from typing import Any, Dict

from app.config import env_str, is_production, load_environment

logger = logging.getLogger("omniflow.security")


class InvalidTokenError(ValueError):
    """Signature mismatch, malformed token, or expired token."""


_ephemeral_key: bytes | None = None
_key_lock = threading.Lock()


def get_secret_key() -> bytes:
    """Return the signing key from ``APP_SECRET_KEY``.

    Development/test without a key gets a random per-process key (tokens will not
    survive restarts or work across workers).  Production refuses to run without one.
    """
    global _ephemeral_key
    load_environment()
    configured = env_str("APP_SECRET_KEY")
    if configured:
        if is_production() and len(configured) < 32:
            raise RuntimeError("APP_SECRET_KEY must be at least 32 characters in production.")
        return configured.encode("utf-8")
    if is_production():
        raise RuntimeError("APP_SECRET_KEY is required when APP_ENV=production.")
    with _key_lock:
        if _ephemeral_key is None:
            _ephemeral_key = secrets.token_bytes(32)
            logger.warning(
                "APP_SECRET_KEY not set; using an ephemeral development key. "
                "Tokens will be invalidated on restart. Set APP_SECRET_KEY for any shared deployment."
            )
        return _ephemeral_key


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _signature(body: str) -> str:
    digest = hmac.new(get_secret_key(), body.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def sign_payload(payload: Dict[str, Any], ttl_seconds: int) -> str:
    """Sign ``payload`` (JSON-serialisable) adding ``iat`` and ``exp`` claims."""
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    now = int(time.time())
    claims = dict(payload)
    claims["iat"] = now
    claims["exp"] = now + int(ttl_seconds)
    body = _b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_signature(body)}"


def verify_payload(token: str) -> Dict[str, Any]:
    """Verify signature and expiry; return the claims or raise :class:`InvalidTokenError`."""
    if not token or not isinstance(token, str) or token.count(".") != 1:
        raise InvalidTokenError("Malformed token")
    body, signature = token.split(".", 1)
    try:
        expected = _signature(body)
    except (UnicodeEncodeError, ValueError) as exc:
        raise InvalidTokenError("Malformed token") from exc
    if not hmac.compare_digest(expected, signature):
        raise InvalidTokenError("Invalid token signature")
    try:
        claims = json.loads(_b64decode(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvalidTokenError("Malformed token payload") from exc
    if not isinstance(claims, dict):
        raise InvalidTokenError("Malformed token payload")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        raise InvalidTokenError("Token expired")
    return claims
