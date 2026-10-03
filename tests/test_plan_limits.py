"""Paid plans: Pro trial for new workspaces, the Pro AI quota (300) and channel cap (3), Agency VIP
without limits, 402 once a subscription ends, upgrade requests, and the operator's set_plan tool.

The PostgreSQL tests at the end (store contract, increment_ai_runs(), RLS) run only when
TEST_DATABASE_URL is set, as in CI.
"""
from __future__ import annotations

import importlib.util
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import plans
from app.main import app, store
from app.security import sign_payload
from app.tenancy import issue_access_token
from db.sqlite_store import SQLiteStore
from models.schemas import ContentDraft, Platform

client = TestClient(app)
ROOT = Path(__file__).resolve().parent.parent

AI_LIMIT = {"code": "AI_LIMIT_REACHED", "message": "Pro monthly AI quota (300/300) reached. Upgrade to VIP."}
CHANNEL_LIMIT = {"code": "CHANNEL_LIMIT_REACHED", "message": "Pro channel cap (3 max) reached. Upgrade to VIP."}
SUBSCRIPTION_REQUIRED = {"code": "SUBSCRIPTION_REQUIRED", "message": "Subscription required."}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _register():
    res = client.post(
        "/auth/register", json={"email": f"plan_{uuid4().hex[:8]}@example.com", "password": "Passw0rd!", "full_name": "Plan"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    return body["user"]["tenant_id"], {"Authorization": f"Bearer {body['token']}"}


def _set_plan(tenant_id, plan="pro", status="active", days=30):
    store.save_subscription(
        tenant_id=tenant_id, plan=plan, billing_cycle="monthly", status=status, current_period_end=_now() + timedelta(days=days)
    )


def _set_ai_runs(tenant_id, count, cycle_end=None):
    store.save_usage(
        tenant_id=tenant_id,
        ai_runs_count=count,
        cycle_start=_now() - timedelta(days=1),
        cycle_end=cycle_end or _now() + timedelta(days=29),
    )


def _ai_runs(tenant_id):
    return plans.current_ai_runs(store.get_usage(tenant_id=tenant_id))


@pytest.fixture
def model(monkeypatch):
    """The copywriter's model call, answering with model-written copy."""
    calls = []

    def generate_with_vision(prompt, image_base64=None, campaign=None):
        calls.append(prompt)
        draft = ContentDraft(campaign_id="c", platform=Platform.META, language="en", body="AI copy",
                             metadata={"status": "ai_generated"})
        return {"copy": "AI copy", "hashtags": "", "platforms": ["Meta"], "draft_id": draft.id, "image_base64": None,
                "drafts": [draft]}

    monkeypatch.setattr(main.copywriter, "generate_with_vision", generate_with_vision)
    return calls


def _generate(headers):
    return client.post("/content/generate", json={"prompt": "bamboo yoga mats", "campaign_id": "default-campaign"}, headers=headers)


def _connect(headers, platform, account_id):
    return client.post(
        "/integrations/connect",
        json={"platform": platform, "account_id": account_id, "account_name": account_id, "access_token": "TOKEN"},
        headers=headers,
    )


# ------------------------------------------------------------------------- the trial
def test_new_workspace_starts_a_7_day_pro_trial():
    _, headers = _register()
    sub = client.get("/api/billing/subscription", headers=headers).json()
    assert sub["plan"] == "pro" and sub["plan_name"] == "Pro Growth" and sub["status"] == "trial"
    assert sub["days_left"] == 7
    assert sub["limits"] == {"ai_runs": 300, "channels": 3, "storage_gb": 5}
    assert sub["usage"] == {"ai_runs": 0, "ai_cycle_end": None, "channels": 0}
    assert sub["pending_request"] is None


def test_trial_length_is_configurable(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_TRIAL_DAYS", "14")
    _, headers = _register()
    assert client.get("/api/billing/subscription", headers=headers).json()["days_left"] == 14


def test_workspaces_from_before_billing_get_a_trial_on_first_use():
    tenant = store.create_tenant("Legacy shop")
    user = store.create_user(f"legacy_{uuid4().hex[:6]}@example.com", "Legacy", "Passw0rd!", tenant_id=tenant["id"])
    headers = {"Authorization": f"Bearer {issue_access_token(user['id'], tenant['id'])}"}
    assert store.get_subscription(tenant_id=tenant["id"]) is None
    assert client.get("/api/billing/subscription", headers=headers).json()["status"] == "trial"


def test_default_workspace_is_the_operators_house_account():
    sub = client.get("/api/billing/subscription").json()  # anonymous = default workspace (REQUIRE_AUTH off)
    assert sub["plan"] == "agency" and sub["status"] == "active" and sub["current_period_end"] is None
    assert sub["limits"]["ai_runs"] is None and sub["limits"]["channels"] is None


def test_public_price_list():
    catalog = client.get("/api/billing/plans").json()
    assert catalog["currency"] == "CNY" and catalog["trial_days"] == 7
    prices = {p["id"]: p["prices"] for p in catalog["plans"]}
    assert prices == {"pro": {"monthly": 66, "annual": 666}, "agency": {"monthly": 166, "annual": 1666}}


# ---------------------------------------------------------------------- AI quota
def test_pro_ai_quota_blocks_the_301st_run(model):
    tenant_id, headers = _register()
    _set_ai_runs(tenant_id, 299)
    assert _generate(headers).status_code == 200
    assert _ai_runs(tenant_id) == 300

    res = _generate(headers)
    assert res.status_code == 403 and res.json()["detail"] == AI_LIMIT
    assert len(model) == 1  # the refused request never reached the model
    assert _ai_runs(tenant_id) == 300


def test_pro_ai_quota_counts_300_real_runs(model):
    tenant_id, headers = _register()
    for i in range(300):
        assert _generate(headers).status_code == 200, f"run {i + 1}"
    assert _ai_runs(tenant_id) == 300
    assert _generate(headers).json()["detail"] == AI_LIMIT
    assert client.get("/api/billing/subscription", headers=headers).json()["usage"]["ai_runs"] == 300


def test_assistant_chat_shares_the_quota(model):
    tenant_id, headers = _register()
    _set_ai_runs(tenant_id, 300)
    res = client.post("/assistant/chat", json={"message": "write an ad for bamboo mats"}, headers=headers)
    assert res.status_code == 403 and res.json()["detail"] == AI_LIMIT


def test_template_drafts_and_plain_answers_are_free():
    tenant_id, headers = _register()  # no LLM configured in tests: every generation is a template draft
    for _ in range(3):
        assert _generate(headers).status_code == 200
    assert client.post("/assistant/chat", json={"message": "what can you do?"}, headers=headers).status_code == 200
    assert _ai_runs(tenant_id) == 0


def test_a_failed_generation_gives_the_run_back(monkeypatch):
    tenant_id, headers = _register()

    def broken(*args, **kwargs):
        raise RuntimeError("model crashed")

    monkeypatch.setattr(main.copywriter, "generate_with_vision", broken)
    res = TestClient(app, raise_server_exceptions=False).post(
        "/content/generate", json={"prompt": "x", "campaign_id": "default-campaign"}, headers=headers
    )
    assert res.status_code == 500
    assert _ai_runs(tenant_id) == 0


def test_the_ai_cycle_restarts_after_30_days(model):
    tenant_id, headers = _register()
    _set_ai_runs(tenant_id, 300, cycle_end=_now() - timedelta(minutes=1))
    assert _generate(headers).status_code == 200
    usage = store.get_usage(tenant_id=tenant_id)
    assert usage["ai_runs_count"] == 1
    assert plans.parse_time(usage["cycle_end"]) > _now() + timedelta(days=29)


def test_concurrent_runs_cannot_overshoot_the_quota(tmp_path):
    local = SQLiteStore(str(tmp_path / "race.db"))
    local.save_usage(tenant_id="t", ai_runs_count=295, cycle_start=_now(), cycle_end=_now() + timedelta(days=30))
    results = []
    threads = [threading.Thread(target=lambda: results.append(local.increment_ai_runs(tenant_id="t", limit=300))) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r for r in results if r is not None) == [296, 297, 298, 299, 300]
    assert local.get_usage(tenant_id="t")["ai_runs_count"] == 300


# ------------------------------------------------------------------- channel cap
def test_pro_channel_cap_blocks_the_4th_channel():
    _, headers = _register()
    for platform in ("x", "tiktok", "wechat"):
        assert _connect(headers, platform, f"{platform}-1").status_code == 200
    res = _connect(headers, "xiaohongshu", "red-1")
    assert res.status_code == 403 and res.json()["detail"] == CHANNEL_LIMIT


def test_rebinding_a_connected_channel_is_allowed_at_the_cap():
    _, headers = _register()
    for page in ("P1", "P2", "P3"):
        assert _connect(headers, "meta", page).status_code == 200
    assert _connect(headers, "meta", "P2").status_code == 200  # token refresh for a connected Page
    assert _connect(headers, "meta", "P4").status_code == 403


def test_api_key_channels_count_once_and_webhooks_never():
    tenant_id, headers = _register()
    assert _connect(headers, "x", "API Key …AAAA").status_code == 200
    assert _connect(headers, "x", "API Key …BBBB").status_code == 200  # new keys replace the X connection
    store.save_connected_account("global", "webhook", "open.feishu.cn/…1234", "Feishu", "https://hook", tenant_id=tenant_id)
    assert client.get("/api/billing/subscription", headers=headers).json()["usage"]["channels"] == 1
    assert _connect(headers, "tiktok", "t").status_code == 200
    assert _connect(headers, "wechat", "w").status_code == 200


def test_disconnecting_frees_a_slot():
    _, headers = _register()
    for platform in ("x", "tiktok", "wechat"):
        _connect(headers, platform, platform)
    assert _connect(headers, "meta", "P1").status_code == 403
    client.post("/integrations/disconnect", json={"platform": "tiktok"}, headers=headers)
    assert _connect(headers, "meta", "P1").status_code == 200


def test_page_token_and_ad_account_binding_respect_the_cap(graph_stub):
    _, headers = _register()
    for platform in ("x", "tiktok", "wechat"):
        _connect(headers, platform, platform)
    res = client.post("/tools/facebook/update-token", json={"access_token": "PAGE_TOKEN", "page_id": "123456"}, headers=headers)
    assert res.status_code == 403 and res.json()["detail"] == CHANNEL_LIMIT
    res = client.post("/tools/meta/ad-account/connect", json={"ad_account_id": "act_42", "access_token": "TOKEN"}, headers=headers)
    assert res.status_code == 403 and res.json()["detail"] == CHANNEL_LIMIT
    assert graph_stub.calls == []  # refused before asking Meta


@pytest.fixture
def meta_oauth(monkeypatch, graph_stub):
    monkeypatch.setenv("META_APP_ID", "app-123")
    monkeypatch.setenv("META_APP_SECRET", "shhh-secret")
    monkeypatch.setenv("META_REDIRECT_URI", "https://app.example.com/auth/facebook/callback")
    graph_stub.add(
        "GET", "/oauth/access_token",
        handler=lambda r: httpx.Response(200, json={"access_token": "LONG" if "fb_exchange_token" in str(r.url) else "SHORT"}),
    )
    return graph_stub


def test_oauth_adds_new_pages_only_while_slots_are_left(meta_oauth):
    tenant_id, headers = _register()
    _connect(headers, "x", "x")
    _connect(headers, "meta", "P1")
    meta_oauth.add("GET", "/me/accounts", json={"data": [
        {"id": "P1", "name": "Shop", "access_token": "T1"},     # already connected: refreshed
        {"id": "P2", "name": "Outlet", "access_token": "T2"},   # takes the last slot
        {"id": "P3", "name": "Brand B", "access_token": "T3"},  # over the cap
    ]})
    state = sign_payload({"typ": "meta_oauth_state", "tid": tenant_id, "uid": "u", "next": None}, 60)
    res = client.get("/auth/facebook/callback", params={"code": "c", "state": state, "format": "json"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert [p["id"] for p in body["pages"]] == ["P1", "P2"] and body["skipped"] == 1
    assert body["limit"] == {"code": "CHANNEL_LIMIT_REACHED", "channels": 3}
    pages = {a["account_id"] for a in store.list_connected_accounts(None, tenant_id=tenant_id) if a["platform"] == "meta"}
    assert pages == {"P1", "P2"}

    redirect = TestClient(app, follow_redirects=False).get("/auth/facebook/callback", params={"code": "c", "state": state})
    assert "meta_error=channel_limit" in redirect.headers["location"]


def test_oauth_needs_an_active_subscription(meta_oauth):
    tenant_id, headers = _register()
    _set_plan(tenant_id, "pro", status="trial", days=-1)
    assert client.get("/auth/facebook/login", params={"format": "json"}, headers=headers).status_code == 402
    state = sign_payload({"typ": "meta_oauth_state", "tid": tenant_id, "uid": "u", "next": None}, 60)
    redirect = TestClient(app, follow_redirects=False).get("/auth/facebook/callback", params={"code": "c", "state": state})
    assert "meta_error=subscription_required" in redirect.headers["location"]
    assert meta_oauth.calls == []  # stopped before exchanging the code


# --------------------------------------------------------------------- Agency VIP
def test_agency_vip_has_no_limits(model):
    tenant_id, headers = _register()
    _set_plan(tenant_id, "agency")
    _set_ai_runs(tenant_id, 10_000)
    assert _generate(headers).status_code == 200
    assert _ai_runs(tenant_id) == 10_001  # still counted
    for i in range(6):
        assert _connect(headers, "meta", f"PAGE-{i}").status_code == 200
    sub = client.get("/api/billing/subscription", headers=headers).json()
    assert sub["plan_name"] == "Agency VIP" and sub["usage"]["channels"] == 6


# ------------------------------------------------------------ ended subscriptions
@pytest.mark.parametrize("status, days", [("trial", -1), ("active", -1), ("expired", 30)])
def test_ended_subscriptions_require_payment(model, status, days):
    tenant_id, headers = _register()
    _set_plan(tenant_id, "agency" if status == "active" else "pro", status=status, days=days)
    for res in (_generate(headers), _connect(headers, "x", "x"),
                client.post("/assistant/chat", json={"message": "hi"}, headers=headers),
                client.get("/auth/oauth/meta/url", headers=headers)):
        assert res.status_code == 402 and res.json()["detail"] == SUBSCRIPTION_REQUIRED
    assert model == []
    assert client.get("/api/billing/subscription", headers=headers).json()["status"] == "expired"


def test_reading_and_disconnecting_keep_working_after_expiry():
    tenant_id, headers = _register()
    _connect(headers, "x", "x")
    _set_plan(tenant_id, status="expired")
    assert client.get("/integrations/status", headers=headers).status_code == 200
    assert client.get("/campaigns", headers=headers).status_code == 200
    assert client.post("/integrations/disconnect", json={"platform": "x"}, headers=headers).status_code == 200


# --------------------------------------------------------------- upgrade requests
def test_upgrade_request_is_recorded_and_sent_to_the_operator(monkeypatch):
    import app.routers.billing as billing

    sent = []
    monkeypatch.setenv("LEAD_NOTIFICATION_WEBHOOK", "https://crm.example.com/leads")
    monkeypatch.setattr(
        billing, "build_async_httpx_client",
        lambda timeout=None, **kw: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(204))),
    )
    tenant_id, headers = _register()
    res = client.post("/api/billing/upgrade-request", json={"plan": "agency", "billing_cycle": "annual"}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "received" and body["duplicate"] is False
    assert (body["plan_name"], body["price"], body["currency"]) == ("Agency VIP", 1666, "CNY")
    assert body["checkout_url"] is None

    (request,) = sent
    payload = json.loads(request.content)
    assert payload["event"] == "upgrade_request.created"
    assert payload["upgrade_request"]["tenant_id"] == tenant_id and payload["upgrade_request"]["email"].startswith("plan_")

    again = client.post("/api/billing/upgrade-request", json={"plan": "agency", "billing_cycle": "annual"}, headers=headers).json()
    assert again["duplicate"] is True and again["id"] == body["id"] and len(sent) == 1

    sub = client.get("/api/billing/subscription", headers=headers).json()
    assert sub["plan"] == "pro" and sub["status"] == "trial"  # a request never upgrades by itself
    assert sub["pending_request"]["plan"] == "agency"


def test_upgrade_request_returns_the_configured_payment_link(monkeypatch):
    monkeypatch.setenv("BILLING_CHECKOUT_URL_PRO_MONTHLY", "https://pay.example.com/pro-monthly")
    monkeypatch.setenv("BILLING_CHECKOUT_URL_PRO_ANNUAL", "javascript:alert(1)")
    _, headers = _register()
    monthly = client.post("/api/billing/upgrade-request", json={"plan": "pro", "billing_cycle": "monthly"}, headers=headers).json()
    annual = client.post("/api/billing/upgrade-request", json={"plan": "pro", "billing_cycle": "annual"}, headers=headers).json()
    assert monthly["checkout_url"] == "https://pay.example.com/pro-monthly" and monthly["price"] == 66
    assert annual["checkout_url"] is None and annual["price"] == 666


def test_upgrade_request_needs_a_signed_in_user_and_a_real_plan():
    _, headers = _register()
    assert client.post("/api/billing/upgrade-request", json={"plan": "pro"}).status_code == 401
    assert client.post("/api/billing/upgrade-request", json={"plan": "free"}, headers=headers).status_code == 422
    assert client.post("/api/billing/upgrade-request", json={"plan": "pro", "billing_cycle": "weekly"}, headers=headers).status_code == 422


# ---------------------------------------------------------------- operator tool
def _set_plan_tool():
    spec = importlib.util.spec_from_file_location("set_plan", ROOT / "scripts" / "set_plan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operator_activates_extends_and_expires_a_plan(model):
    tool = _set_plan_tool()
    tenant_id, headers = _register()
    client.post("/api/billing/upgrade-request", json={"plan": "agency", "billing_cycle": "annual"}, headers=headers)

    first = tool.activate(store, tenant_id, "agency", "annual", 1)
    assert first["plan"] == "agency" and first["status"] == "active" and first["days_left"] == 365
    assert store.list_upgrade_requests(tenant_id=tenant_id, status="pending") == []

    renewed = tool.activate(store, tenant_id, "agency", "annual", 1)  # paid again before the end
    assert renewed["days_left"] == 730
    _set_ai_runs(tenant_id, 5000)
    assert _generate(headers).status_code == 200

    assert tool.expire(store, tenant_id)["status"] == "expired"
    assert _generate(headers).status_code == 402


def test_operator_tool_command_line(tmp_path, monkeypatch, capsys):
    tool = _set_plan_tool()
    db = tmp_path / "ops.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    ops = SQLiteStore(str(db))
    tenant = ops.create_tenant("Shop")
    ops.create_user("owner@shop.example", "Owner", "Passw0rd!", tenant_id=tenant["id"])
    ops.save_upgrade_request({"user_id": None, "plan": "pro", "billing_cycle": "monthly"}, tenant_id=tenant["id"])

    assert tool.main(["--list"]) == 0
    assert tenant["id"] in capsys.readouterr().out
    assert tool.main(["--email", "owner@shop.example", "--plan", "pro", "--cycle", "monthly"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["workspace"] == tenant["id"] and shown["plan"] == "pro" and shown["status"] == "active"


# --------------------------------------------------------------------- PostgreSQL
PG_URL = os.environ.get("TEST_DATABASE_URL", "")
pg_only = pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set (no PostgreSQL available)")
SQL_FILES = [ROOT / "scripts" / "init_supabase.sql", ROOT / "scripts" / "supabase_subscriptions.sql"]
APP_ROLE = "omniflow_billing_test"


@pytest.fixture
def pg_store():
    import psycopg

    from db.postgres_store import PostgresStore

    with psycopg.connect(PG_URL, autocommit=True) as conn:
        for sql in SQL_FILES:
            conn.execute(sql.read_text(encoding="utf-8"))
            conn.execute(sql.read_text(encoding="utf-8"))  # idempotent
        conn.execute("TRUNCATE subscriptions, usage_tracking, upgrade_requests")
    s = PostgresStore(PG_URL, min_size=1, max_size=12)
    yield s
    s.close()


@pg_only
def test_postgres_subscription_contract(pg_store):
    tenant = pg_store.create_tenant(f"Billing {uuid4().hex[:6]}")["id"]
    end = _now() + timedelta(days=7)
    created = pg_store.ensure_subscription(tenant_id=tenant, plan="pro", billing_cycle="monthly", status="trial", current_period_end=end)
    assert created["status"] == "trial" and plans.effective_status(created) == "trial"
    again = pg_store.ensure_subscription(tenant_id=tenant, plan="agency", billing_cycle="annual", status="active", current_period_end=None)
    assert again["plan"] == "pro"  # ensure never overwrites
    saved = pg_store.save_subscription(tenant_id=tenant, plan="agency", billing_cycle="annual", status="active", current_period_end=None)
    assert saved["plan"] == "agency" and saved["current_period_end"] is None

    assert [pg_store.increment_ai_runs(tenant_id=tenant, limit=2) for _ in range(3)] == [1, 2, None]
    pg_store.release_ai_run(tenant_id=tenant)
    assert pg_store.get_usage(tenant_id=tenant)["ai_runs_count"] == 1
    assert pg_store.increment_ai_runs(tenant_id=tenant) == 2  # no limit: Agency VIP

    pg_store.save_usage(tenant_id=tenant, ai_runs_count=300, cycle_start=_now() - timedelta(days=31), cycle_end=_now() - timedelta(days=1))
    assert pg_store.increment_ai_runs(tenant_id=tenant, limit=300) == 1  # ended cycle restarts
    assert plans.parse_time(pg_store.get_usage(tenant_id=tenant)["cycle_end"]) > _now() + timedelta(days=29)

    request = pg_store.save_upgrade_request({"user_id": "u", "plan": "pro", "billing_cycle": "annual"}, tenant_id=tenant)
    assert pg_store.list_upgrade_requests(tenant_id=tenant, status="pending")[0]["id"] == request["id"]
    assert any(r["id"] == request["id"] for r in pg_store.list_upgrade_requests_any_tenant())
    assert pg_store.set_upgrade_requests_status("fulfilled", tenant_id=tenant) == 1


@pg_only
def test_postgres_concurrent_runs_cannot_overshoot(pg_store):
    tenant = pg_store.create_tenant(f"Race {uuid4().hex[:6]}")["id"]
    pg_store.save_usage(tenant_id=tenant, ai_runs_count=290, cycle_start=_now(), cycle_end=_now() + timedelta(days=30))
    results = []
    threads = [threading.Thread(target=lambda: results.append(pg_store.increment_ai_runs(tenant_id=tenant, limit=300))) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r for r in results if r is not None) == list(range(291, 301))
    assert pg_store.get_usage(tenant_id=tenant)["ai_runs_count"] == 300


@pg_only
def test_postgres_default_house_account_is_seeded(pg_store):
    import psycopg

    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(SQL_FILES[1].read_text(encoding="utf-8"))
    house = pg_store.get_subscription(tenant_id="default")
    assert (house["plan"], house["status"], house["current_period_end"]) == ("agency", "active", None)


@pg_only
def test_postgres_rls_lets_only_the_server_change_a_plan(pg_store):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    tenant_a = pg_store.create_tenant("A")["id"]
    tenant_b = pg_store.create_tenant("B")["id"]
    for tenant in (tenant_a, tenant_b):
        pg_store.ensure_subscription(tenant_id=tenant, plan="pro", billing_cycle="monthly", status="trial", current_period_end=_now())

    with psycopg.connect(PG_URL, autocommit=True) as owner:
        owner.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")
        owner.execute(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'rlspw' NOSUPERUSER NOBYPASSRLS")
        owner.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
        owner.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON subscriptions, usage_tracking TO {APP_ROLE}")
        owner.execute(f"GRANT EXECUTE ON FUNCTION increment_ai_runs(text, integer) TO {APP_ROLE}")
    url = make_conninfo(**{**conninfo_to_dict(PG_URL), "user": APP_ROLE, "password": "rlspw"})
    try:
        with psycopg.connect(url) as conn:
            # The API server, scoped to workspace A.
            conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_a,))
            assert [r[0] for r in conn.execute("SELECT tenant_id FROM subscriptions")] == [tenant_a]
            assert conn.execute("UPDATE subscriptions SET plan = 'agency' WHERE tenant_id = %s", (tenant_b,)).rowcount == 0
            assert conn.execute("SELECT increment_ai_runs(%s, 300)", (tenant_a,)).fetchone()[0] == 1
            conn.commit()

            # Without the server's session setting (any other client): nothing is visible or writable.
            conn.execute("SELECT set_config('app.tenant_id', '', false)")
            assert conn.execute("SELECT count(*) FROM subscriptions").fetchone()[0] == 0
            assert conn.execute("UPDATE subscriptions SET plan = 'agency'").rowcount == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("INSERT INTO usage_tracking (tenant_id) VALUES (%s)", (tenant_b,))
            conn.rollback()
        assert pg_store.get_subscription(tenant_id=tenant_b)["plan"] == "pro"
    finally:
        with psycopg.connect(PG_URL, autocommit=True) as owner:
            owner.execute(f"REASSIGN OWNED BY {APP_ROLE} TO CURRENT_USER")
            owner.execute(f"DROP OWNED BY {APP_ROLE}")
            owner.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")
