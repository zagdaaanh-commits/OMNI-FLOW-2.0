"""Dual-mode outbound HTTP layer (Hong Kong direct vs. mainland proxy).

``OUTBOUND_PROXY_URL`` is the single source of truth for outbound routing:

* **Direct mode** (unset / empty) - production on a Hong Kong cloud host.  Traffic
  to Meta Graph API, CDNs and LLM providers goes straight out.  ``trust_env`` is
  disabled so stray ``HTTP_PROXY``/``HTTPS_PROXY`` variables cannot hijack it.
* **Proxy mode** (e.g. ``http://127.0.0.1:7890`` or ``socks5h://127.0.0.1:7891``)
  - mainland local development or a private egress node.  HTTPS is tunnelled
  with ``CONNECT`` (or SOCKS), so TLS stays end-to-end and certificate
  verification remains enabled.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Literal, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import env_float, env_str, load_environment

logger = logging.getLogger("omniflow.http")

SUPPORTED_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
USER_AGENT = "OmniFlow/2.0 (+https://omniflow.ai)"
DEFAULT_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0)

NetworkMode = Literal["direct", "proxy"]


def get_outbound_proxy() -> Optional[str]:
    """Return the configured outbound proxy URL, or ``None`` for direct mode.

    Raises ``ValueError`` for unsupported schemes so misconfiguration fails loudly.
    """
    load_environment()
    raw = env_str("OUTBOUND_PROXY_URL")
    if not raw:
        return None
    scheme = urlsplit(raw).scheme.lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError(
            f"Unsupported OUTBOUND_PROXY_URL scheme '{scheme or '<none>'}'. "
            f"Use one of: {', '.join(s + '://' for s in SUPPORTED_PROXY_SCHEMES)}"
        )
    return raw


def network_mode() -> NetworkMode:
    return "proxy" if get_outbound_proxy() else "direct"


def mask_proxy_url(url: Optional[str]) -> Optional[str]:
    """Hide credentials embedded in a proxy URL (``user:pass@`` -> ``***:***@``)."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "***"
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, parts.query, parts.fragment))


def default_timeout() -> float:
    load_environment()
    return env_float("HTTP_TIMEOUT_SECONDS", 15.0)


def _client_kwargs(timeout: float | None, overrides: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "proxy": get_outbound_proxy(),
        "trust_env": False,
        "timeout": httpx.Timeout(timeout if timeout is not None else default_timeout()),
        "limits": DEFAULT_LIMITS,
        "headers": {"User-Agent": USER_AGENT},
        "follow_redirects": False,
    }
    kwargs.update(overrides)
    return kwargs


def build_httpx_client(timeout: float | None = None, **kwargs: Any) -> httpx.Client:
    """Build a synchronous client honouring ``OUTBOUND_PROXY_URL``."""
    return httpx.Client(**_client_kwargs(timeout, kwargs))


def build_async_httpx_client(timeout: float | None = None, **kwargs: Any) -> httpx.AsyncClient:
    """Build an asynchronous client honouring ``OUTBOUND_PROXY_URL``."""
    return httpx.AsyncClient(**_client_kwargs(timeout, kwargs))


_client_lock = threading.Lock()
_shared_client: Optional[httpx.Client] = None
_shared_signature: Optional[tuple[Optional[str], float]] = None


def get_http_client() -> httpx.Client:
    """Process-wide pooled client, rebuilt automatically if the proxy/timeout changes."""
    global _shared_client, _shared_signature
    signature = (get_outbound_proxy(), default_timeout())
    with _client_lock:
        if _shared_client is None or _shared_client.is_closed or _shared_signature != signature:
            # The previous client is intentionally not closed here: another thread
            # may still be mid-request on it.  It is released when garbage collected.
            _shared_client = build_httpx_client()
            _shared_signature = signature
            logger.info(
                "Outbound HTTP client ready (mode=%s, proxy=%s)",
                "proxy" if signature[0] else "direct",
                mask_proxy_url(signature[0]),
            )
        return _shared_client


def reset_http_client() -> None:
    """Close and drop the shared client (used by tests and config reloads)."""
    global _shared_client, _shared_signature
    with _client_lock:
        if _shared_client is not None and not _shared_client.is_closed:
            _shared_client.close()
        _shared_client = None
        _shared_signature = None
