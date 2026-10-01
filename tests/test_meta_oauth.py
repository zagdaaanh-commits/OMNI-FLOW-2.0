"""/auth/facebook/login and /auth/facebook/callback (mock Graph, in-memory store)."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.meta_oauth import router
from app.security import sign_payload
from app.tenancy import issue_access_token
from db.sqlite_store import SQLiteStore


@pytest.fixture
def oauth_env(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "app-123")
    monkeypatch.setenv("META_APP_SECRET", "shhh-secret")
    monkeypatch.setenv("META_REDIRECT_URI", "https://app.example.com/auth/facebook/callback")


@pytest.fixture
def env(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.store = SQLiteStore(str(tmp_path / "oauth.db"))
    hook_calls = []
    app.state.on_meta_connected = lambda tid, page: hook_calls.append((tid, page["id"]))
    client = TestClient(app, follow_redirects=False)
    return client, app.state.store, hook_calls


def _state_from(location: str) -> str:
    return parse_qs(urlsplit(location).query)["state"][0]


def test_login_redirects_to_facebook_dialog(env, oauth_env):
    client, _, _ = env
    res = client.get("/auth/facebook/login")
    assert res.status_code == 307
    parts = urlsplit(res.headers["location"])
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert parts.netloc == "www.facebook.com" and parts.path.endswith("/dialog/oauth")
    assert q["client_id"] == "app-123"
    assert q["redirect_uri"] == "https://app.example.com/auth/facebook/callback"
    assert "pages_manage_posts" in q["scope"] and q["response_type"] == "code"
    assert q["state"]


def test_login_json_format_and_state_carries_tenant(env, oauth_env):
    client, _, _ = env
    token = issue_access_token("user-9", "acme")
    res = client.get("/auth/facebook/login?format=json", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    body = res.json()
    assert body["authorization_url"].startswith("https://www.facebook.com/")
    from app.security import verify_payload

    claims = verify_payload(body["state"])
    assert claims["tid"] == "acme" and claims["uid"] == "user-9"


def test_login_not_configured_is_503(env):
    client, _, _ = env
    res = client.get("/auth/facebook/login")
    assert res.status_code == 503 and "META_APP_ID" in res.json()["detail"]


def test_login_rejects_open_redirect_targets(env, oauth_env):
    client, _, _ = env
    from app.security import verify_payload

    for bad in ("https://evil.example.com", "//evil.example.com", "/\\evil"):
        state = client.get("/auth/facebook/login", params={"next": bad, "format": "json"}).json()["state"]
        assert verify_payload(state)["next"] is None
    state = client.get("/auth/facebook/login", params={"next": "/dashboard", "format": "json"}).json()["state"]
    assert verify_payload(state)["next"] == "/dashboard"


@pytest.mark.parametrize("state", [None, "garbage", "a.b"])
def test_callback_rejects_missing_or_bad_state(env, oauth_env, state):
    client, _, _ = env
    params = {"code": "c"}
    if state:
        params["state"] = state
    assert client.get("/auth/facebook/callback", params=params).status_code == 400


def test_callback_rejects_expired_and_wrong_type_state(env, oauth_env):
    client, _, _ = env
    wrong_type = sign_payload({"typ": "access", "tid": "acme"}, 60)
    assert client.get("/auth/facebook/callback", params={"code": "c", "state": wrong_type}).status_code == 400
    expired = sign_payload({"typ": "meta_oauth_state", "tid": "acme"}, 1)
    import time

    time.sleep(1.1)
    assert client.get("/auth/facebook/callback", params={"code": "c", "state": expired}).status_code == 400


def _graph_happy_path(graph_stub, pages):
    graph_stub.add(
        "GET", "/oauth/access_token",
        handler=lambda r: httpx.Response(200, json={"access_token": "LONG" if "fb_exchange_token" in str(r.url) else "SHORT"}),
    )
    graph_stub.add("GET", "/me/accounts", json={"data": pages})


def test_callback_happy_path_persists_pages_for_tenant_and_never_leaks_tokens(env, oauth_env, graph_stub):
    client, store, hook_calls = env
    tenant = store.create_tenant("Acme")
    _graph_happy_path(graph_stub, [
        {"id": "P1", "name": "Acme Shop", "access_token": "PAGE_TOKEN_1", "tasks": ["CREATE_CONTENT"]},
        {"id": "P2", "name": "Acme Outlet", "access_token": "PAGE_TOKEN_2"},
        {"id": "P3", "name": "No token page"},
    ])
    state = sign_payload({"typ": "meta_oauth_state", "tid": tenant["id"], "uid": "user-1", "next": None}, 60)
    res = client.get("/auth/facebook/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "connected" and body["tenant_id"] == tenant["id"]
    assert [p["id"] for p in body["pages"]] == ["P1", "P2"]
    assert "PAGE_TOKEN" not in res.text and "LONG" not in res.text and "SHORT" not in res.text

    accounts = store.list_connected_accounts(None, tenant_id=tenant["id"])
    assert {a["account_id"] for a in accounts} == {"P1", "P2"}
    assert store.get_connected_account(None, "meta", tenant_id=tenant["id"])["account_id"] == "P1"  # first page is the default
    assert store.list_connected_accounts(None, tenant_id="default") == []  # nothing leaked to other tenants
    assert hook_calls == [(tenant["id"], "P1")]

    # the long-lived exchange was performed with the short-lived token, and page listing used the long-lived one
    exchange = [r for r in graph_stub.calls if "fb_exchange_token" in str(r.url)]
    assert exchange and "fb_exchange_token=SHORT" in str(exchange[0].url)
    assert "access_token=LONG" in str(graph_stub.requests_to("/me/accounts")[0].url)


def test_callback_browser_flow_redirects_back_to_frontend(env, oauth_env, graph_stub, monkeypatch):
    client, store, _ = env
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.com")
    _graph_happy_path(graph_stub, [{"id": "P1", "name": "A", "access_token": "t"}])
    state = sign_payload({"typ": "meta_oauth_state", "tid": "default", "uid": None, "next": "/dashboard"}, 60)
    res = client.get("/auth/facebook/callback", params={"code": "c", "state": state})
    assert res.status_code == 303
    assert res.headers["location"] == "https://app.example.com/dashboard?meta_connected=1"


def test_callback_user_denied(env, oauth_env, graph_stub):
    client, store, _ = env
    state = sign_payload({"typ": "meta_oauth_state", "tid": "default", "uid": None, "next": None}, 60)
    res = client.get("/auth/facebook/callback", params={"error": "access_denied", "error_reason": "user_denied", "state": state, "format": "json"})
    assert res.status_code == 400 and res.json()["status"] == "denied"
    redirect = client.get("/auth/facebook/callback", params={"error": "access_denied", "state": state})
    assert redirect.status_code == 303 and "meta_connected=0" in redirect.headers["location"] and "access_denied" in redirect.headers["location"]
    assert graph_stub.calls == [] and store.list_connected_accounts(None) == []


def test_callback_graph_rejection_maps_to_502(env, oauth_env, graph_stub):
    client, _, _ = env
    graph_stub.add("GET", "/oauth/access_token", status=400, json={"error": {"message": "This authorization code has been used.", "code": 100}})
    state = sign_payload({"typ": "meta_oauth_state", "tid": "default", "uid": None, "next": None}, 60)
    res = client.get("/auth/facebook/callback", params={"code": "used", "state": state})
    assert res.status_code == 502 and "has been used" in res.json()["detail"]


def test_callback_falls_back_to_short_lived_token_if_long_lived_exchange_fails(env, oauth_env, graph_stub):
    client, store, _ = env

    def exchange(request):
        if "fb_exchange_token" in str(request.url):
            return httpx.Response(400, json={"error": {"message": "nope", "code": 1}})
        return httpx.Response(200, json={"access_token": "SHORT"})

    graph_stub.add("GET", "/oauth/access_token", handler=exchange)
    graph_stub.add("GET", "/me/accounts", json={"data": [{"id": "P1", "name": "A", "access_token": "t"}]})
    state = sign_payload({"typ": "meta_oauth_state", "tid": "default", "uid": None, "next": None}, 60)
    res = client.get("/auth/facebook/callback", params={"code": "c", "state": state, "format": "json"})
    assert res.status_code == 200
    assert "access_token=SHORT" in str(graph_stub.requests_to("/me/accounts")[0].url)


def test_legacy_callback_path_is_the_real_handler(env, oauth_env, graph_stub):
    client, _, _ = env
    graph_stub.add("GET", "/oauth/access_token", status=400, json={"error": {"message": "bad", "code": 100}})
    state = sign_payload({"typ": "meta_oauth_state", "tid": "default", "uid": None, "next": None}, 60)
    assert client.get("/auth/callback/meta", params={"code": "c", "state": state}).status_code == 502


# ----------------------------------------------------------------- scopes & webhooks
def test_login_requests_engagement_and_webhook_scopes(env, oauth_env):
    client, _, _ = env
    q = parse_qs(urlsplit(client.get("/auth/facebook/login").headers["location"]).query)
    scopes = q["scope"][0].split(",")
    assert {"pages_manage_engagement", "pages_manage_metadata", "pages_manage_posts", "pages_show_list"} <= set(scopes)


def test_login_scope_override_is_still_respected(env, oauth_env, monkeypatch):
    client, _, _ = env
    monkeypatch.setenv("META_OAUTH_SCOPES", "pages_show_list")
    q = parse_qs(urlsplit(client.get("/auth/facebook/login").headers["location"]).query)
    assert q["scope"] == ["pages_show_list"]


def test_callback_subscribes_every_connected_page_to_webhooks(env, oauth_env, graph_stub):
    client, store, _ = env
    _graph_happy_path(graph_stub, [
        {"id": "P1", "name": "Shop", "access_token": "PAGE_TOKEN_1"},
        {"id": "P2", "name": "Outlet", "access_token": "PAGE_TOKEN_2"},
        {"id": "P3", "name": "No token page"},
    ])
    graph_stub.add("POST", "/subscribed_apps", json={"success": True})
    state = sign_payload({"typ": "meta_oauth_state", "tid": "default", "uid": None, "next": None}, 60)
    res = client.get("/auth/facebook/callback", params={"code": "c", "state": state, "format": "json"})
    assert res.status_code == 200, res.text
    assert [(p["id"], p["webhook_subscribed"]) for p in res.json()["pages"]] == [("P1", True), ("P2", True)]

    subscriptions = {
        urlsplit(str(r.url)).path.split("/")[-2]: parse_qs(r.content.decode())
        for r in graph_stub.requests_to("/subscribed_apps")
    }
    assert subscriptions == {
        "P1": {"subscribed_fields": ["feed"], "access_token": ["PAGE_TOKEN_1"]},
        "P2": {"subscribed_fields": ["feed"], "access_token": ["PAGE_TOKEN_2"]},
    }
    assert "PAGE_TOKEN" not in res.text


def test_callback_keeps_the_connection_when_webhook_subscription_fails(env, oauth_env, graph_stub):
    client, store, hook_calls = env
    _graph_happy_path(graph_stub, [{"id": "P1", "name": "Shop", "access_token": "PAGE_TOKEN_1"}])
    graph_stub.add("POST", "/subscribed_apps", status=403, json={"error": {"message": "(#200) Permission denied", "code": 200}})
    state = sign_payload({"typ": "meta_oauth_state", "tid": "default", "uid": None, "next": None}, 60)
    res = client.get("/auth/facebook/callback", params={"code": "c", "state": state, "format": "json"})
    assert res.status_code == 200
    assert res.json()["pages"] == [{"id": "P1", "name": "Shop", "webhook_subscribed": False}]
    assert store.get_connected_account(None, "meta")["account_id"] == "P1"
    assert hook_calls == [("default", "P1")]
