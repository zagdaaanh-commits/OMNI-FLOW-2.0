"""Meta ad-account binding (POST /tools/meta/ad-account/connect) and honest /integrations/status."""
from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from db.sqlite_store import SQLiteStore

client = TestClient(app)

AD_ACCOUNT = {
    "id": "act_123456789",
    "account_id": "123456789",
    "name": "Example Shop Ads",
    "account_status": 1,
    "currency": "USD",
    "timezone_name": "Asia/Shanghai",
}


def _register():
    email = f"ads_{uuid4().hex[:8]}@example.com"
    res = client.post("/auth/register", json={"email": email, "password": "Passw0rd!", "full_name": "Ads User"})
    assert res.status_code == 200, res.text
    body = res.json()
    return body["user"]["tenant_id"], {"Authorization": f"Bearer {body['token']}"}


# ------------------------------------------------------------------ ad account binding
def test_ad_account_is_verified_with_graph_then_saved(graph_stub):
    tenant_id, headers = _register()
    graph_stub.add("GET", "/act_123456789?", json=AD_ACCOUNT)

    res = client.post(
        "/tools/meta/ad-account/connect",
        json={"ad_account_id": "123456789", "access_token": "SYSTEM_USER_TOKEN"},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {
        "success": True,
        "connected": True,
        "ad_account_id": "act_123456789",
        "name": "Example Shop Ads",
        "currency": "USD",
        "timezone": "Asia/Shanghai",
        "account_status": 1,
        "account_status_label": "active",
    }
    assert "SYSTEM_USER_TOKEN" not in res.text

    (request,) = graph_stub.requests_to("/act_123456789")
    query = parse_qs(urlsplit(str(request.url)).query)
    assert query["access_token"] == ["SYSTEM_USER_TOKEN"]
    assert "account_status" in query["fields"][0]

    saved = store.get_connected_account(None, "meta_ads", tenant_id=tenant_id)
    assert saved["account_id"] == "act_123456789" and saved["access_token"] == "SYSTEM_USER_TOKEN"
    status = client.get("/integrations/status", headers=headers).json()["meta_ads"]
    assert status["status"] == "connected" and status["account_name"] == "Example Shop Ads"


def test_ad_account_rejected_by_graph_is_not_saved(graph_stub):
    tenant_id, headers = _register()
    graph_stub.add("GET", "/act_555?", status=400, json={"error": {"message": "Invalid OAuth access token.", "code": 190}})
    res = client.post("/tools/meta/ad-account/connect", json={"ad_account_id": "act_555", "access_token": "BAD"}, headers=headers)
    assert res.status_code == 400
    assert "Invalid OAuth access token." in res.json()["detail"]
    assert store.get_connected_account(None, "meta_ads", tenant_id=tenant_id) is None


def test_ad_account_transport_failure_is_a_502(graph_stub):
    def boom(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    graph_stub.add("GET", "/act_777?", handler=boom)
    _, headers = _register()
    res = client.post("/tools/meta/ad-account/connect", json={"ad_account_id": "act_777", "access_token": "T"}, headers=headers)
    assert res.status_code == 502


@pytest.mark.parametrize(
    "body",
    [
        {"ad_account_id": "act_abc", "access_token": "T"},
        {"ad_account_id": "me/feed", "access_token": "T"},
        {"ad_account_id": "", "access_token": "T"},
        {"ad_account_id": "act_1", "access_token": "two tokens"},
        {"ad_account_id": "act_1", "access_token": "   "},
    ],
)
def test_ad_account_input_is_validated_before_calling_graph(graph_stub, body):
    assert client.post("/tools/meta/ad-account/connect", json=body).status_code == 422
    assert graph_stub.calls == []


def test_ad_account_binding_never_touches_the_page_settings(graph_stub, monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "PAGE_FROM_ENV")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "PAGE_TOKEN_FROM_ENV")
    graph_stub.add("GET", "/act_123456789?", json=AD_ACCOUNT)
    res = client.post("/tools/meta/ad-account/connect", json={"ad_account_id": "act_123456789", "access_token": "SYSTEM_USER_TOKEN"})
    assert res.status_code == 200
    assert os.environ["FACEBOOK_PAGE_ID"] == "PAGE_FROM_ENV"
    assert os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"] == "PAGE_TOKEN_FROM_ENV"


# ------------------------------------------------------------------ integration status
@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    s = SQLiteStore(str(tmp_path / "status.db"))
    monkeypatch.setattr("app.main.store", s)
    return s


def test_status_ignores_api_keys_in_the_settings_file(fresh_store, monkeypatch):
    monkeypatch.setattr(
        "app.main.load_api_settings",
        lambda: {"meta_access_token": "EAAB_placeholder", "tiktok_api_key": "tk", "xiaohongshu_api_key": "xhs", "wechat_app_id": "wx"},
    )
    data = client.get("/integrations/status").json()
    assert {key: item["status"] for key, item in data.items()} == {
        "meta": "not_connected",
        "instagram": "not_connected",
        "tiktok": "not_connected",
        "x": "not_connected",
        "xiaohongshu": "not_connected",
        "wechat": "not_connected",
        "meta_ads": "not_connected",
        "webhook": "not_connected",
    }
    assert all(item["account_name"] is None and item["account_id"] is None for item in data.values())


def test_status_reports_saved_accounts(fresh_store):
    fresh_store.save_connected_account("global", "wechat", "gh_real", "Real Official Account", "WX_TOKEN")
    data = client.get("/integrations/status").json()
    assert data["wechat"]["status"] == "connected" and data["wechat"]["account_name"] == "Real Official Account"
    assert data["xiaohongshu"]["status"] == "not_connected"


def test_env_page_is_verified_live_and_instagram_needs_its_own_id(fresh_store, graph_stub, monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "PAGE_ENV")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "PAGE_TOKEN_ENV_1234")
    graph_stub.add("GET", "/PAGE_ENV?", json={"id": "PAGE_ENV", "name": "Real Shop Page"})

    data = client.get("/integrations/status").json()
    assert data["meta"]["status"] == "connected"
    assert data["meta"]["account_name"] == "Real Shop Page" and data["meta"]["account_id"] == "PAGE_ENV"
    assert "PAGE_TOKEN_ENV_1234" not in str(data)
    assert data["instagram"]["status"] == "not_connected"  # a Meta token alone is not an Instagram account

    monkeypatch.setenv("META_IG_USER_ID", "17841400000000000")
    assert client.get("/integrations/status").json()["instagram"]["status"] == "connected"


def test_env_page_with_a_rejected_token_is_not_connected(fresh_store, graph_stub, monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "PAGE_ENV")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "EXPIRED_TOKEN")
    graph_stub.add("GET", "/PAGE_ENV?", status=400, json={"error": {"message": "Session has expired", "code": 190}})
    meta = client.get("/integrations/status").json()["meta"]
    assert meta["status"] == "not_connected" and meta["error"]


def test_other_workspaces_never_see_the_env_page(fresh_store, graph_stub, monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "PAGE_ENV")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "PAGE_TOKEN_ENV")
    tenant = fresh_store.create_tenant("Other")
    from app.tenancy import issue_access_token

    user = fresh_store.create_user(f"o_{uuid4().hex[:6]}@example.com", "Other", "Passw0rd!", tenant_id=tenant["id"])
    headers = {"Authorization": f"Bearer {issue_access_token(user['id'], tenant['id'])}"}
    assert client.get("/integrations/status", headers=headers).json()["meta"]["status"] == "not_connected"
    assert graph_stub.calls == []
