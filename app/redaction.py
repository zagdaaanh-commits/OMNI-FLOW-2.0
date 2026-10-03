"""Scrub secrets and personal identifiers from text that can reach a client or a stored error.

Meta Graph API errors and exception strings routinely embed things merchants must never see:
access tokens in request URLs, the app id ("...has not authorized application 2550...") and
Facebook user ids. Every error message that leaves the backend goes through :func:`redact`.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Configured secrets are removed verbatim wherever they appear.
_SECRET_ENV_VARS = (
    "META_APP_SECRET",
    "META_APP_ID",
    "FACEBOOK_PAGE_ACCESS_TOKEN",
    "FACEBOOK_USER_ACCESS_TOKEN",
    "META_ACCESS_TOKEN",
    "META_VERIFY_TOKEN",
    "OPENAI_API_KEY",
    "OPEN_AI_KEY",
    "ZHIPUAI_API_KEY",
    "APP_SECRET_KEY",
    "REDIS_PASSWORD",
    "LEAD_NOTIFICATION_WEBHOOK",
    "SUPABASE_SERVICE_ROLE_KEY",
)

_PATTERNS = (
    # secrets passed as query/form parameters (httpx error messages include the full URL)
    (re.compile(r"(?i)\b(access_token|input_token|fb_exchange_token|client_secret|appsecret_proof|"
                r"api_key|password|secret|token|code)=([^&\s'\"<>]+)"), r"\1=[redacted]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"\bEA[A-Za-z0-9]{20,}"), "[redacted-token]"),               # Meta access tokens
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), "[redacted-key]"),               # OpenAI-style keys
    (re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b"), "[redacted-key]"),     # Zhipu keys
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1[redacted]@"),              # credentials in URLs
    (re.compile(r"(?<!\w)(?<!\d\.)\d{9,}(?!\w|\.\d)"), "[id]"),               # Facebook app/user/page ids (not decimals)
)


def redact(text: Any) -> str:
    """Return ``text`` with secrets and long numeric identifiers replaced by placeholders."""
    if text is None:
        return ""
    out = str(text)
    for name in _SECRET_ENV_VARS:
        value = (os.getenv(name) or "").strip()
        if len(value) >= 8:
            out = out.replace(value, "[redacted]")
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def describe_exception(exc: BaseException, limit: int = 300) -> str:
    """``Type: message`` for logs or stored task errors, redacted and length-capped."""
    return redact(f"{type(exc).__name__}: {exc}")[:limit]
