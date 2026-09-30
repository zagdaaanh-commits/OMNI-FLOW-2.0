"""/health/network diagnostics (no real network)."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import health as health_module
from app.routers.health import router


def _client(monkeypatch, handler):
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(
        health_module, "client_factory",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout),
    )
    return TestClient(app)


def _by_name(body):
    return {t["name"]: t for t in body["targets"]}


def test_all_targets_reachable_reports_ok_with_latency(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(404 if "facebook" in request.url.host else 200)

    body = _client(monkeypatch, handler).get("/health/network").json()
    assert body["status"] == "ok" and body["mode"] == "direct" and body["proxy"] is None
    assert sorted(seen) == ["graph.facebook.com", "open.bigmodel.cn"]
    targets = _by_name(body)
    # any HTTP answer (even 404/4xx) proves the network path works
    assert targets["meta_graph"]["reachable"] is True and targets["meta_graph"]["status_code"] == 404
    assert targets["zhipu_bigmodel"]["status_code"] == 200
    assert all(isinstance(t["latency_ms"], float) and t["latency_ms"] >= 0 for t in body["targets"])
    assert body["checked_at"].endswith("+00:00")


def test_one_target_timing_out_is_degraded(monkeypatch):
    def handler(request):
        if "facebook" in request.url.host:
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200)

    body = _client(monkeypatch, handler).get("/health/network").json()
    assert body["status"] == "degraded"
    meta = _by_name(body)["meta_graph"]
    assert meta["reachable"] is False and meta["status_code"] is None
    assert meta["error"].startswith("connect_timeout")
    assert _by_name(body)["zhipu_bigmodel"]["reachable"] is True


def test_everything_down(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("Temporary failure in name resolution: getaddrinfo failed")

    res = _client(monkeypatch, handler).get("/health/network")
    body = res.json()
    assert res.status_code == 200 and body["status"] == "down"
    assert all(t["error"].startswith("dns_error") for t in body["targets"])


@pytest.mark.parametrize(
    "exc,label",
    [
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ProxyError("bad proxy"), "proxy_error"),
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), "tls_error"),
        (httpx.ConnectError("connection refused"), "connect_error"),
    ],
)
def test_failure_classification(monkeypatch, exc, label):
    def handler(request):
        raise exc

    body = _client(monkeypatch, handler).get("/health/network").json()
    assert all(t["error"].startswith(label) for t in body["targets"])


def test_strict_mode_returns_503_unless_everything_is_reachable(monkeypatch):
    def handler(request):
        if "facebook" in request.url.host:
            raise httpx.ConnectTimeout("t")
        return httpx.Response(200)

    client = _client(monkeypatch, handler)
    assert client.get("/health/network").status_code == 200
    strict = client.get("/health/network?strict=true")
    assert strict.status_code == 503 and strict.json()["status"] == "degraded"

    healthy = _client(monkeypatch, lambda r: httpx.Response(200))
    assert healthy.get("/health/network?strict=true").status_code == 200


def test_proxy_mode_is_reported_with_masked_credentials(monkeypatch):
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "http://alice:s3cret@127.0.0.1:7890")
    body = _client(monkeypatch, lambda r: httpx.Response(200)).get("/health/network").json()
    assert body["mode"] == "proxy"
    assert body["proxy"] == "http://***:***@127.0.0.1:7890"
    assert "alice" not in str(body) and "s3cret" not in str(body)


def test_invalid_proxy_configuration_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "ftp://nope")
    res = _client(monkeypatch, lambda r: httpx.Response(200)).get("/health/network")
    body = res.json()
    assert res.status_code == 200 and body["status"] == "down" and "config_error" in body
    assert all(t["error"].startswith("config_error") for t in body["targets"])


def test_llm_provider_host_is_probed_once_and_deduplicated(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200)

    monkeypatch.setenv("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")  # same as a default target
    body = _client(monkeypatch, handler).get("/health/network").json()
    assert len(body["targets"]) == 2 and seen.count("open.bigmodel.cn") == 1

    seen.clear()
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("NETWORK_PROBE_EXTRA_URLS", "https://cdn.example.com, https://graph.facebook.com")
    body = _client(monkeypatch, handler).get("/health/network").json()
    assert sorted(seen) == ["api.deepseek.com", "cdn.example.com", "graph.facebook.com", "open.bigmodel.cn"]


def test_probe_timeout_setting_is_used(monkeypatch):
    captured = {}

    def factory(timeout):
        captured["timeout"] = timeout
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)), timeout=timeout)

    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(health_module, "client_factory", factory)
    monkeypatch.setenv("NETWORK_PROBE_TIMEOUT_SECONDS", "2.5")
    TestClient(app).get("/health/network")
    assert captured["timeout"] == 2.5


def test_default_factory_builds_a_proxy_aware_client(monkeypatch):
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "http://127.0.0.1:7890")
    client = health_module._default_client_factory(1.0)
    assert client._mounts and client.timeout.connect == 1.0
