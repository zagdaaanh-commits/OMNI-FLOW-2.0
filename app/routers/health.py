"""Network diagnostics: ``GET /health/network``.

Reports reachability and round-trip latency from THIS server to the external services the
platform depends on, plus whether traffic is going direct (Hong Kong cloud) or through
``OUTBOUND_PROXY_URL`` (mainland development).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.config import env_float, env_str
from app.redaction import redact
from tools.http_client import build_async_httpx_client, get_outbound_proxy, mask_proxy_url

logger = logging.getLogger("omniflow.health")

router = APIRouter(tags=["health"])

DEFAULT_TARGETS = [
    ("meta_graph", "https://graph.facebook.com"),
    ("zhipu_bigmodel", "https://open.bigmodel.cn"),
]

# Factory hook so tests can inject an httpx.MockTransport-backed client.
ClientFactory = Callable[[float], httpx.AsyncClient]


def _default_client_factory(timeout: float) -> httpx.AsyncClient:
    return build_async_httpx_client(timeout=timeout)


client_factory: ClientFactory = _default_client_factory


def _classify_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ProxyError):
        return "proxy_error"
    if isinstance(exc, httpx.ConnectError):
        text = str(exc).lower()
        if "certificate" in text or "ssl" in text or "tls" in text:
            return "tls_error"
        if "name or service" in text or "getaddrinfo" in text or "nodename" in text or "resolve" in text:
            return "dns_error"
        return "connect_error"
    if isinstance(exc, httpx.HTTPError):
        return "http_error"
    return type(exc).__name__


def _targets() -> List[Dict[str, str]]:
    targets: List[Dict[str, str]] = [{"name": n, "url": u} for n, u in DEFAULT_TARGETS]
    extra_urls: List[str] = []
    base = env_str("OPENAI_BASE_URL")
    if base:
        parts = urlsplit(base)
        if parts.scheme and parts.netloc:
            extra_urls.append(f"{parts.scheme}://{parts.netloc}")
    extra_urls.extend(u.strip() for u in env_str("NETWORK_PROBE_EXTRA_URLS").split(",") if u.strip())
    seen = {urlsplit(t["url"]).netloc for t in targets}
    for url in extra_urls:
        netloc = urlsplit(url).netloc
        if netloc and netloc not in seen:
            seen.add(netloc)
            targets.append({"name": netloc, "url": url})
    return targets


async def _probe(client: httpx.AsyncClient, target: Dict[str, str]) -> Dict[str, Any]:
    started = time.perf_counter()
    result: Dict[str, Any] = {
        "name": target["name"], "url": target["url"], "reachable": False,
        "status_code": None, "latency_ms": None, "error": None,
    }
    try:
        # Any HTTP response (even 4xx) proves the network path and TLS handshake work.
        resp = await client.get(target["url"], follow_redirects=False)
        result.update(reachable=True, status_code=resp.status_code)
    except Exception as exc:  # noqa: BLE001 - diagnostic must never raise
        result["error"] = f"{_classify_error(exc)}: {redact(exc)[:200]}"
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


@router.get("/health/network")
async def network_health(strict: bool = Query(default=False, description="Return 503 unless every target is reachable")):
    timeout = env_float("NETWORK_PROBE_TIMEOUT_SECONDS", 5.0)
    proxy: Optional[str]
    try:
        proxy = get_outbound_proxy()
        config_error = None
    except ValueError as exc:
        proxy, config_error = None, str(exc)

    targets = _targets()
    if config_error:
        results = [
            {**t, "reachable": False, "status_code": None, "latency_ms": None, "error": f"config_error: {config_error}"}
            for t in targets
        ]
    else:
        async with client_factory(timeout) as client:
            results = list(await asyncio.gather(*(_probe(client, t) for t in targets)))

    reachable = sum(1 for r in results if r["reachable"])
    status = "ok" if reachable == len(results) else ("down" if reachable == 0 else "degraded")
    body = {
        "status": status,
        "mode": "proxy" if proxy else "direct",
        "proxy": mask_proxy_url(proxy),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "targets": results,
    }
    if config_error:
        body["config_error"] = config_error
    if strict and status != "ok":
        return JSONResponse(status_code=503, content=body)
    return body
