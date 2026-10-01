"""End-to-end multi-tenant behaviour through the real FastAPI app."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from app.scheduler import SchedulerService
from app.services import resolve_task_credentials
from agents.publisher import PublisherAgent
import asyncio

client = TestClient(app)


@pytest.fixture(autouse=True)
def _no_env_file_writes(monkeypatch):
    monkeypatch.setattr("app.main._update_env_file", lambda updates: None)


def _register(company: str = "Acme"):
    email = f"user_{uuid4().hex[:8]}@example.com"
    res = client.post("/auth/register", json={"email": email, "password": "Passw0rd!", "full_name": "Test User", "company": company})
    assert res.status_code == 200, res.text
    body = res.json()
    return body["user"], {"Authorization": f"Bearer {body['token']}"}


def test_register_creates_a_tenant_and_a_signed_token():
    user, headers = _register("Globex")
    assert user["tenant_id"] != "default"
    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["id"] == user["id"]


def test_login_returns_token_for_the_users_tenant():
    user, _ = _register()
    # the tenant admin's email/password logs in globally
    email = user["email"]
    res = client.post("/auth/login", json={"email": email, "password": "Passw0rd!"})
    assert res.status_code == 200 and res.json()["user"]["tenant_id"] == user["tenant_id"]
    assert client.post("/auth/login", json={"email": email, "password": "nope"}).status_code == 401


def test_duplicate_registration_is_rejected():
    user, _ = _register()
    res = client.post("/auth/register", json={"email": user["email"], "password": "Passw0rd!", "full_name": "X"})
    assert res.status_code == 400


def test_stale_legacy_token_is_401_but_anonymous_still_works():
    assert client.get("/auth/me", headers={"Authorization": "Bearer omt_" + "a" * 32}).status_code == 401
    # Anonymous callers fall back to the default workspace; with no account there they must sign in.
    anon = client.get("/auth/me")
    assert anon.status_code == 200 or (anon.status_code == 401 and anon.json()["detail"] == "Not signed in.")


def test_campaigns_drafts_tasks_are_isolated_between_tenants():
    _, a = _register("Tenant A")
    _, b = _register("Tenant B")

    created = client.post("/campaign/create", json={"name": "Only for A"}, headers=a)
    assert created.status_code == 200
    campaign_id = created.json()["id"]

    assert [c["name"] for c in client.get("/campaigns", headers=a).json()] == ["Only for A"]
    names_b = [c["name"] for c in client.get("/campaigns", headers=b).json()]
    assert "Only for A" not in names_b
    # B asking for A's campaign id gets B's own (demo) campaign, never A's data
    assert client.get(f"/campaign/{campaign_id}", headers=b).json()["id"] != campaign_id
    assert client.get(f"/campaign/{campaign_id}", headers=a).json()["id"] == campaign_id

    draft = client.post("/content/generate", json={"campaign_id": campaign_id, "topic": "widgets", "platforms": ["meta"]}, headers=a).json()[0]
    assert draft["tenant_id"] == created.json()["tenant_id"]
    assert draft["id"] in {d["id"] for d in client.get("/drafts", headers=a).json()}
    assert draft["id"] not in {d["id"] for d in client.get("/drafts", headers=b).json()}

    task = client.post("/publish/schedule", json={"content_draft_ids": [draft["id"]], "publish_now": True}, headers=a).json()[0]
    assert task["tenant_id"] == created.json()["tenant_id"]
    assert task["id"] in {t["id"] for t in client.get("/publish/tasks", headers=a).json()}
    assert task["id"] not in {t["id"] for t in client.get("/publish/tasks", headers=b).json()}
    assert task["id"] not in {t["id"] for t in client.get("/publish/tasks").json()}  # nor the default workspace


def test_client_supplied_tenant_id_is_ignored():
    """CampaignCreate allows extra fields; a client must not be able to write into another tenant."""
    victim, _ = _register("Victim")
    _, attacker = _register("Attacker")
    res = client.post("/campaign/create", json={"name": "planted", "tenant_id": victim["tenant_id"]}, headers=attacker)
    assert res.status_code == 200
    assert res.json()["tenant_id"] != victim["tenant_id"]
    assert store.list_campaigns(tenant_id=victim["tenant_id"]) == []


def test_synthesized_demo_ids_do_not_collide_across_tenants():
    """Swagger-style ids ('string') are global primary keys; two tenants using them must both work."""
    _, a = _register("A")
    _, b = _register("B")
    for headers in (a, b, None):
        res = client.post("/publish/schedule", json={"content_draft_ids": ["string"], "publish_now": True}, headers=headers or {})
        assert res.status_code == 200, res.text
        assert res.json()[0]["status"] == "published"
    for headers in (a, b):
        res = client.post("/content/generate", json={"campaign_id": "string", "topic": "x", "platforms": ["meta"]}, headers=headers)
        assert res.status_code == 200, res.text


def test_assistant_creates_campaigns_in_the_callers_tenant():
    user, a = _register("Assist Co")
    res = client.post("/assistant/chat", json={"message": "create new campaign Autumn Push"}, headers=a)
    assert res.status_code == 200 and res.json()["type"] == "campaign"
    assert res.json()["data"]["tenant_id"] == user["tenant_id"]
    assert any("Autumn Push" in c.name for c in store.list_campaigns(tenant_id=user["tenant_id"]))


def test_integrations_are_tenant_scoped_and_never_touch_process_credentials(monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "")
    user, a = _register("Connect Co")
    res = client.post("/integrations/connect", json={
        "platform": "meta", "account_id": "PAGE-A", "account_name": "A's Page", "access_token": "TENANT_A_SECRET_TOKEN",
    }, headers=a)
    assert res.status_code == 200
    assert "access_token" not in res.json()["account"] and "TENANT_A_SECRET" not in res.text
    import os

    assert os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "") == ""  # process-wide credentials untouched

    assert client.get("/integrations/status", headers=a).json()["meta"]["status"] == "connected"
    _, b = _register("Other Co")
    other = client.get("/integrations/status", headers=b).json()["meta"]
    assert other["status"] == "not_connected" and other["account_id"] is None

    assert client.get("/tools/facebook/status", headers=a).json()["connected"] is True
    assert client.get("/tools/facebook/status", headers=b).json()["connected"] is False

    client.post("/integrations/disconnect", json={"platform": "meta"}, headers=a)
    assert client.get("/integrations/status", headers=a).json()["meta"]["status"] == "not_connected"


def test_platform_settings_are_default_workspace_only():
    _, a = _register("Not Admin")
    assert client.get("/settings/apis", headers=a).status_code == 403
    assert client.post("/settings/apis", json={"openai_api_key": "x" * 12}, headers=a).status_code == 403
    assert client.post("/settings/apis/test", headers=a).status_code == 403
    assert client.get("/settings/apis").status_code == 200  # default workspace (dashboard) unchanged


def test_settings_do_not_return_plaintext_secrets_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr("app.main.load_api_settings", lambda: {"openai_api_key": "sk-very-secret-value", "stitch_mcp_url": "mcp://x"})
    body = client.get("/settings/apis").json()
    assert body["raw"]["openai_api_key"] == "" and "very-secret" not in str(body)
    assert body["raw"]["stitch_mcp_url"] == "mcp://x"


def test_update_token_for_a_tenant_saves_only_to_that_tenant(graph_stub):
    graph_stub.add("GET", "/PAGE-T", json={"id": "PAGE-T", "name": "Tenant Page"})
    user, a = _register("Token Co")
    res = client.post("/tools/facebook/update-token", json={"access_token": "EAAB_TOKEN_FOR_TENANT", "page_id": "PAGE-T"}, headers=a)
    assert res.status_code == 200 and res.json()["page_name"] == "Tenant Page"
    saved = store.get_connected_account(None, "meta", tenant_id=user["tenant_id"])
    assert saved["account_id"] == "PAGE-T" and saved["access_token"] == "EAAB_TOKEN_FOR_TENANT"
    import os

    assert os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "") != "EAAB_TOKEN_FOR_TENANT"


def test_tenant_publish_uses_the_tenants_page_not_the_default_one(graph_stub, monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "DEFAULT_PAGE")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "DEFAULT_TOKEN_VALUE")
    graph_stub.add("POST", "/TENANT_PAGE/photos", json={"id": "1", "post_id": "TENANT_PAGE_1"})
    user, a = _register("Publisher Co")
    store.save_connected_account("u", "meta", "TENANT_PAGE", "TP", "TENANT_TOKEN_VALUE", tenant_id=user["tenant_id"])

    task = client.post("/publish/schedule", json={"content_draft_ids": ["x"], "publish_now": True, "channel": "meta"}, headers=a).json()[0]
    assert task["status"] == "published" and task["external_post_id"] == "TENANT_PAGE_1"
    url = str(graph_stub.calls[0].url)
    assert "TENANT_PAGE" in url and "TENANT_TOKEN_VALUE" in url and "DEFAULT_TOKEN_VALUE" not in url

    # a tenant with NO page must not fall back to the default workspace's page
    graph_stub.calls.clear()
    _, no_page = _register("No Page Co")
    task2 = client.post("/publish/schedule", json={"content_draft_ids": ["y"], "publish_now": True, "channel": "meta"}, headers=no_page).json()[0]
    assert graph_stub.calls == [] and task2["confirmation_badge"] == "Credentials Missing 🟡"


def test_scheduled_post_is_persisted_and_executed_by_the_scheduler(graph_stub, monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "DEFAULT_PAGE")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "DEFAULT_TOKEN_VALUE")
    graph_stub.add("POST", "/DEFAULT_PAGE/photos", json={"id": "1", "post_id": "DEFAULT_PAGE_42"})

    when = datetime.now(timezone.utc) + timedelta(minutes=10)
    res = client.post("/publish/schedule", json={
        "content_draft_ids": ["sched-draft"], "publish_now": False, "scheduled_at": when.isoformat(), "channel": "meta",
    })
    task = res.json()[0]
    assert res.status_code == 200 and task["status"] == "scheduled"
    assert store.get_task(task["id"]).status.value == "scheduled"  # persisted, not just an in-memory job

    service = SchedulerService(store, PublisherAgent(), credentials_resolver=lambda t: resolve_task_credentials(store, t),
                               now_fn=lambda: when + timedelta(seconds=1))
    assert asyncio.run(service.poll_once()) >= 1
    done = store.get_task(task["id"])
    assert done.status.value == "published" and done.external_post_id == "DEFAULT_PAGE_42"
    assert client.get("/publish/tasks").json()  # visible through the API


def test_health_reports_database_and_scheduler_mode():
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["database"] == "ok" and body["scheduler_mode"] == "off"


def test_health_returns_503_when_database_is_down(monkeypatch):
    def broken(_tenant):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_tenant", broken)
    res = client.get("/health")
    assert res.status_code == 503 and res.json()["database"] == "error"


def test_lifespan_starts_and_stops_the_embedded_scheduler(monkeypatch):
    monkeypatch.setenv("SCHEDULER_MODE", "embedded")
    monkeypatch.setenv("SCHEDULER_POLL_SECONDS", "1")
    with TestClient(app) as running:
        body = running.get("/health").json()
        assert body["scheduler_mode"] == "embedded" and body["scheduler_running"] is True
    monkeypatch.setenv("SCHEDULER_MODE", "off")
    assert client.get("/health").json()["scheduler_running"] is False


def test_oauth_url_endpoint_requires_configuration_then_returns_signed_state(monkeypatch):
    res = client.get("/auth/oauth/meta/url").json()
    assert res["configured"] is False and res["oauth_url"] is None
    monkeypatch.setenv("META_APP_ID", "app-1")
    monkeypatch.setenv("META_APP_SECRET", "sec")
    monkeypatch.setenv("META_REDIRECT_URI", "https://app.example.com/auth/facebook/callback")
    res = client.get("/auth/oauth/meta/url").json()
    assert res["configured"] is True and res["oauth_url"].startswith("https://www.facebook.com/") and "state=" in res["oauth_url"]


def test_oauth_router_is_mounted_on_the_main_app(monkeypatch):
    assert client.get("/auth/facebook/login", follow_redirects=False).status_code == 503  # not configured, but routed
    assert client.get("/auth/facebook/callback").status_code == 400  # missing state


def test_cors_is_wildcard_without_credentials_by_default():
    res = client.get("/health", headers={"Origin": "https://example.com"})
    assert res.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in res.headers


def test_new_workspace_starts_with_no_campaigns():
    _, headers = _register("Fresh Co")
    assert client.get("/campaigns", headers=headers).json() == []


def test_update_token_subscribes_the_page_to_webhooks(graph_stub):
    graph_stub.add("GET", "/PAGE-W", json={"id": "PAGE-W", "name": "Webhook Page"})
    graph_stub.add("POST", "/PAGE-W/subscribed_apps", json={"success": True})
    _, a = _register("Webhook Token Co")
    res = client.post("/tools/facebook/update-token", json={"access_token": "EAAB_TOKEN_W", "page_id": "PAGE-W"}, headers=a)
    assert res.status_code == 200 and res.json()["webhook_subscribed"] is True
    (request,) = graph_stub.requests_to("/PAGE-W/subscribed_apps")
    assert "subscribed_fields=feed" in request.content.decode()
    assert "access_token=EAAB_TOKEN_W" in request.content.decode()


def test_update_token_still_connects_when_webhook_subscription_fails(graph_stub):
    graph_stub.add("GET", "/PAGE-X", json={"id": "PAGE-X", "name": "No Webhook Page"})
    graph_stub.add("POST", "/PAGE-X/subscribed_apps", status=403, json={"error": {"message": "(#200) Permission denied", "code": 200}})
    user, a = _register("No Webhook Co")
    res = client.post("/tools/facebook/update-token", json={"access_token": "EAAB_TOKEN_X", "page_id": "PAGE-X"}, headers=a)
    assert res.status_code == 200
    assert res.json()["connected"] is True and res.json()["webhook_subscribed"] is False
    assert store.get_connected_account(None, "meta", tenant_id=user["tenant_id"])["account_id"] == "PAGE-X"
