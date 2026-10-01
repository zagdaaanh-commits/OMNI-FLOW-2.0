"""Per-workspace notification webhook: SSRF guard, test-before-save, masking, isolation, delivery."""
from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

import app.routers.agency as agency
import app.routers.notifications as notifications
from app.main import app, store

client = TestClient(app)

FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/abcd-SECRET-1234"
CUSTOM_URL = "https://hooks.example.com/omniflow/SECRETPATH9876"


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(notifications, "resolve_host", lambda host: ["93.184.216.34"])


@pytest.fixture
def outbound(monkeypatch):
    """Capture webhook POSTs; tests set `outbound.reply` to choose the response."""
    class Outbound:
        calls = []
        reply = staticmethod(lambda request: httpx.Response(200, json={"code": 0, "msg": "success"}))

    Outbound.calls = []

    def handler(request):
        Outbound.calls.append(request)
        return Outbound.reply(request)

    monkeypatch.setattr(
        notifications,
        "build_async_httpx_client",
        lambda timeout=None, **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return Outbound


def _register():
    res = client.post("/auth/register", json={"email": f"hook_{uuid4().hex[:8]}@example.com", "password": "Passw0rd!", "full_name": "Hook"})
    assert res.status_code == 200, res.text
    body = res.json()
    return body["user"]["tenant_id"], {"Authorization": f"Bearer {body['token']}"}


# ---------------------------------------------------------------------- URL guard
@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1", "fd00::1", "0.0.0.0"])
def test_non_public_hosts_are_refused(monkeypatch, outbound, address):
    monkeypatch.setattr(notifications, "resolve_host", lambda host: [address])
    _, headers = _register()
    res = client.post("/integrations/webhook", json={"url": "https://internal.example.com/hook"}, headers=headers)
    assert res.status_code == 400 and "public" in res.json()["detail"]
    assert outbound.calls == []


@pytest.mark.parametrize("url", ["ftp://hooks.example.com/x", "https:///nohost", "https://user:pw@hooks.example.com/x"])
def test_malformed_urls_are_refused(public_dns, outbound, url):
    _, headers = _register()
    assert client.post("/integrations/webhook", json={"url": url}, headers=headers).status_code == 400
    assert outbound.calls == []


def test_production_requires_https(public_dns, outbound, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(notifications.WebhookURLError):
        notifications.validate_public_url("http://hooks.example.com/x")
    assert notifications.validate_public_url("https://hooks.example.com/x")


# ------------------------------------------------------------------- test & save
def test_feishu_webhook_is_tested_then_saved_masked(public_dns, outbound):
    tenant_id, headers = _register()
    res = client.post("/integrations/webhook", json={"url": FEISHU_URL}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "connected", "provider": "feishu", "masked_url": "open.feishu.cn/…1234"}

    (request,) = outbound.calls
    assert str(request.url) == FEISHU_URL
    body = json.loads(request.content)
    assert body["msg_type"] == "text" and "OmniFlow" in body["content"]["text"]

    saved = store.get_connected_account(None, "webhook", tenant_id=tenant_id)
    assert saved["access_token"] == FEISHU_URL
    status = client.get("/integrations/status", headers=headers)
    assert status.json()["webhook"]["status"] == "connected"
    assert "SECRET" not in status.text  # only the masked form reaches the browser


def test_rejected_test_message_is_not_saved(public_dns, outbound):
    outbound.reply = staticmethod(lambda request: httpx.Response(200, json={"code": 19021, "msg": "sign match fail"}))
    tenant_id, headers = _register()
    res = client.post("/integrations/webhook", json={"url": FEISHU_URL}, headers=headers)
    assert res.status_code == 400 and "sign match fail" in res.json()["detail"]
    assert store.get_connected_account(None, "webhook", tenant_id=tenant_id) is None


def test_custom_endpoint_errors_and_unreachable_hosts_are_reported(public_dns, outbound):
    _, headers = _register()
    outbound.reply = staticmethod(lambda request: httpx.Response(500))
    assert "HTTP 500" in client.post("/integrations/webhook", json={"url": CUSTOM_URL}, headers=headers).json()["detail"]

    def unreachable(request):
        raise httpx.ConnectError("refused", request=request)

    outbound.reply = staticmethod(unreachable)
    res = client.post("/integrations/webhook", json={"url": CUSTOM_URL}, headers=headers)
    assert res.status_code == 400 and "could not be reached" in res.json()["detail"]


def test_one_webhook_per_workspace_and_workspaces_are_isolated(public_dns, outbound):
    tenant_a, a = _register()
    tenant_b, b = _register()
    assert client.post("/integrations/webhook", json={"url": FEISHU_URL}, headers=a).status_code == 200
    assert client.post("/integrations/webhook", json={"url": CUSTOM_URL}, headers=a).status_code == 200
    hooks = [x for x in store.list_connected_accounts(None, tenant_id=tenant_a) if x["platform"] == "webhook"]
    assert [h["access_token"] for h in hooks] == [CUSTOM_URL]
    assert client.get("/integrations/status", headers=b).json()["webhook"]["status"] == "not_connected"

    client.post("/integrations/disconnect", json={"platform": "webhook"}, headers=a)
    assert client.get("/integrations/status", headers=a).json()["webhook"]["status"] == "not_connected"


# ------------------------------------------------------------------ lead delivery
def test_agency_application_is_delivered_to_the_workspace_webhook(public_dns, outbound, monkeypatch):
    operator_calls = []
    monkeypatch.setenv("LEAD_NOTIFICATION_WEBHOOK", "https://crm.example.com/leads")
    monkeypatch.setattr(
        agency,
        "build_async_httpx_client",
        lambda timeout=None, **kw: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: operator_calls.append(r) or httpx.Response(204))),
    )
    _, headers = _register()
    assert client.post("/integrations/webhook", json={"url": FEISHU_URL}, headers=headers).status_code == 200
    outbound.calls.clear()

    application = {
        "company_name": "Shenzhen Example Trading Co., Ltd.",
        "credit_code": "91330100799655058B",
        "store_url": "https://shop.example.com",
        "contact": "wechat: example",
    }
    res = client.post("/api/agency/apply", json=application, headers=headers)
    assert res.status_code == 200

    (merchant_hook,) = outbound.calls
    text = json.loads(merchant_hook.content)["content"]["text"]
    assert "Shenzhen Example Trading" in text and res.json()["id"] in text
    assert len(operator_calls) == 1  # the operator's CRM still gets the lead


def test_workspace_without_a_webhook_sends_nothing(public_dns, outbound):
    _, headers = _register()
    application = {"company_name": "A", "credit_code": "91330100799655058B", "store_url": "https://a.example.com", "contact": "x"}
    assert client.post("/api/agency/apply", json=application, headers=headers).status_code == 200
    assert outbound.calls == []


def test_signup_without_a_company_stores_no_placeholder():
    res = client.post("/auth/register", json={"email": f"nc_{uuid4().hex[:8]}@example.com", "password": "Passw0rd!", "full_name": "No Company"})
    assert res.status_code == 200
    assert res.json()["user"]["company"] == ""
