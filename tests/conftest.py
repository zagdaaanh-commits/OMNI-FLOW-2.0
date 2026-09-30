"""Hermetic test environment.

Runs BEFORE ``app.main`` is imported by any test module: real credentials in ``.env`` are
never loaded, no test can reach Meta/LLM providers, and the database is a throw-away file.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_TEST_DB_DIR = tempfile.mkdtemp(prefix="omniflow-tests-")

os.environ["OMNIFLOW_DISABLE_DOTENV"] = "1"
os.environ["APP_ENV"] = "test"
os.environ["APP_SECRET_KEY"] = "test-secret-key-please-do-not-use-in-production-0123456789"
os.environ["DATABASE_PATH"] = str(Path(_TEST_DB_DIR) / "test-marketing.db")
os.environ["SCHEDULER_MODE"] = "off"
os.environ["LLM_BACKOFF_BASE_SECONDS"] = "0"
os.environ["PBKDF2_ITERATIONS"] = "1000"  # fast password hashing in tests
for _name in (
    "OPENAI_API_KEY", "OPEN_AI_KEY", "ZHIPUAI_API_KEY", "OPENAI_BASE_URL", "MODEL_NAME", "OPENAI_MODEL",
    "OPENANAI_BASE_URI_MODEL", "VISION_MODEL_NAME",
    "FACEBOOK_PAGE_ACCESS_TOKEN", "FACEBOOK_USER_ACCESS_TOKEN", "FACEBOOK_PAGE_ID",
    "META_ACCESS_TOKEN", "META_PAGE_ID", "META_IG_USER_ID", "META_APP_ID", "META_APP_SECRET", "META_REDIRECT_URI",
    "OUTBOUND_PROXY_URL", "DATABASE_URL", "REDIS_URL", "REQUIRE_AUTH", "ALLOW_TENANT_HEADER",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
):
    os.environ[_name] = ""


import socket  # noqa: E402


class ExternalNetworkBlocked(RuntimeError):
    """Raised when a test tries to open a non-loopback socket."""


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch):
    """Hermetic guarantee: tests may only talk to loopback (e.g. a local Postgres), never the Internet."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _allowed(address) -> bool:
        if isinstance(address, (str, bytes)):  # AF_UNIX
            return True
        return str(address[0]) in {"127.0.0.1", "::1", "localhost", "0.0.0.0"}

    def guarded_connect(self, address):
        if not _allowed(address):
            raise ExternalNetworkBlocked(f"External network access is blocked in tests: {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _allowed(address):
            raise ExternalNetworkBlocked(f"External network access is blocked in tests: {address!r}")
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield


@pytest.fixture(autouse=True)
def _isolate_process_state():
    """Undo env mutations (several endpoints write os.environ) and reset shared clients."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    from tools.http_client import reset_http_client

    reset_http_client()
    try:
        from agents.llm_client import reset_llm_state

        reset_llm_state()
    except Exception:  # noqa: BLE001
        pass
    try:
        from tools.meta_api import MetaAPIClient

        MetaAPIClient._cached_page_tokens.clear()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- Graph API stub
from typing import Any, Callable, Dict, List, Optional  # noqa: E402

import httpx  # noqa: E402


class GraphStub:
    """Route table backed by ``httpx.MockTransport``; records every request it serves."""

    def __init__(self) -> None:
        self.routes: List[Dict[str, Any]] = []
        self.calls: List[httpx.Request] = []
        self.unmatched: List[httpx.Request] = []

    def add(
        self,
        method: str,
        contains: str,
        *,
        status: int = 200,
        json: Optional[dict] = None,
        handler: Optional[Callable[[httpx.Request], httpx.Response]] = None,
    ) -> "GraphStub":
        self.routes.append({"method": method.upper(), "contains": contains, "status": status, "json": json, "handler": handler})
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        for route in self.routes:
            if route["method"] == request.method and route["contains"] in str(request.url):
                if route["handler"]:
                    return route["handler"](request)
                return httpx.Response(route["status"], json=route["json"] if route["json"] is not None else {})
        self.unmatched.append(request)
        return httpx.Response(599, json={"error": {"message": f"unmatched test request {request.method} {request.url}"}})

    def requests_to(self, contains: str) -> List[httpx.Request]:
        return [r for r in self.calls if contains in str(r.url)]

    @staticmethod
    def body_text(request: httpx.Request) -> str:
        return request.content.decode("utf-8", errors="ignore")


@pytest.fixture
def graph_stub(monkeypatch):
    """Replace the shared outbound client used by MetaAPIClient with a MockTransport client."""
    stub = GraphStub()
    client = httpx.Client(transport=httpx.MockTransport(stub.handle))
    monkeypatch.setattr("tools.meta_api.get_http_client", lambda: client)
    yield stub
    client.close()
