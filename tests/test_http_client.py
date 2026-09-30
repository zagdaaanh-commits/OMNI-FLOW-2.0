"""Dual-mode outbound network layer: direct (HK) vs proxy (mainland dev)."""
from __future__ import annotations

import httpx
import pytest

from tools import http_client
from tools.http_client import (
    build_async_httpx_client,
    build_httpx_client,
    get_http_client,
    get_outbound_proxy,
    mask_proxy_url,
    network_mode,
    reset_http_client,
)


def _proxy_mounts(client: httpx.Client) -> list:
    """Transports mounted for URL patterns (present only when a proxy is configured)."""
    return [t for t in client._mounts.values() if t is not None]


@pytest.mark.parametrize("value", ["", "   ", None])
def test_direct_mode_when_proxy_unset_or_blank(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("OUTBOUND_PROXY_URL", raising=False)
    else:
        monkeypatch.setenv("OUTBOUND_PROXY_URL", value)
    assert get_outbound_proxy() is None
    assert network_mode() == "direct"
    client = build_httpx_client()
    assert _proxy_mounts(client) == []
    client.close()


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:7890", "https://proxy.example.com:8443", "socks5://127.0.0.1:7891", "socks5h://user:pw@127.0.0.1:7891"],
)
def test_proxy_mode_accepts_supported_schemes(monkeypatch, url):
    monkeypatch.setenv("OUTBOUND_PROXY_URL", url)
    assert get_outbound_proxy() == url
    assert network_mode() == "proxy"
    client = build_httpx_client()
    assert _proxy_mounts(client), "proxy transports must be mounted in proxy mode"
    client.close()


@pytest.mark.parametrize("url", ["ftp://proxy:21", "127.0.0.1:7890", "gopher://x"])
def test_invalid_proxy_scheme_is_rejected(monkeypatch, url):
    monkeypatch.setenv("OUTBOUND_PROXY_URL", url)
    with pytest.raises(ValueError, match="Unsupported OUTBOUND_PROXY_URL scheme"):
        get_outbound_proxy()


def test_mask_proxy_url_hides_credentials():
    assert mask_proxy_url(None) is None
    assert mask_proxy_url("http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    masked = mask_proxy_url("socks5h://alice:s3cret@10.0.0.5:1080")
    assert "alice" not in masked and "s3cret" not in masked
    assert masked == "socks5h://***:***@10.0.0.5:1080"


def test_ambient_proxy_variables_never_hijack_direct_mode(monkeypatch):
    """trust_env is off, so HTTPS_PROXY on the server cannot silently reroute traffic."""
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://evil.example:3128")
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "")
    client = build_httpx_client()
    assert client.trust_env is False
    assert _proxy_mounts(client) == []
    client.close()


def test_tls_verification_stays_enabled_in_proxy_mode(monkeypatch):
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "http://127.0.0.1:7890")
    client = build_httpx_client()
    # Custom verify=False would be visible via the transport pool's ssl context settings.
    for transport in _proxy_mounts(client):
        ctx = transport._pool._ssl_context
        assert ctx is not None and ctx.verify_mode != 0  # ssl.CERT_NONE == 0
    client.close()


def test_default_timeout_and_user_agent(monkeypatch):
    monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "7")
    client = build_httpx_client()
    assert client.timeout.read == 7
    assert client.headers["user-agent"].startswith("OmniFlow/")
    assert build_httpx_client(timeout=3).timeout.connect == 3
    client.close()


def test_async_client_honours_proxy(monkeypatch):
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "http://127.0.0.1:7890")
    client = build_async_httpx_client()
    assert client._mounts, "async client must also mount the proxy"


def test_shared_client_is_singleton_and_rebuilds_when_proxy_changes(monkeypatch):
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "")
    first = get_http_client()
    assert get_http_client() is first
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "http://127.0.0.1:7890")
    second = get_http_client()
    assert second is not first
    assert _proxy_mounts(second)
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "")
    third = get_http_client()
    assert third is not second and not _proxy_mounts(third)


def test_reset_http_client_closes_and_clears():
    client = get_http_client()
    reset_http_client()
    assert client.is_closed
    assert http_client._shared_client is None
    assert get_http_client() is not client
