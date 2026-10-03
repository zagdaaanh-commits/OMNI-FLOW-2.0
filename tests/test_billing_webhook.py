"""Stripe billing: Checkout Sessions for prepaid plan periods and the signed webhook that activates them.

Webhook events are signed here exactly as Stripe signs them (``t=<ts>,v1=HMAC-SHA256(secret,
"<ts>.<body>")``), so the real signature check runs. Stripe's API is replaced by a fake client
(``fake_stripe``), except in one test that sends the real SDK's request into a mock transport.
The PostgreSQL tests at the end run only when TEST_DATABASE_URL is set.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app import payments, plans
from app.main import app, store
from app.tenancy import issue_access_token

client = TestClient(app)
ROOT = Path(__file__).resolve().parent.parent

WEBHOOK_SECRET = "whsec_test_" + "a1b2c3d4e5f6" * 3
SECRET_KEY = "sk_test_" + "51TestKeyDoNotUse" * 2
PRICES = {("pro", "monthly"): 6600, ("pro", "annual"): 66600, ("agency", "monthly"): 16600, ("agency", "annual"): 166600}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _register():
    email = f"pay_{uuid4().hex[:8]}@example.com"
    res = client.post("/auth/register", json={"email": email, "password": "Passw0rd!", "full_name": "Payer"})
    assert res.status_code == 200, res.text
    body = res.json()
    return body["user"]["tenant_id"], {"Authorization": f"Bearer {body['token']}"}, email


def _set_plan(tenant_id, plan="pro", status="active", days=30.0, cycle="monthly"):
    store.save_subscription(
        tenant_id=tenant_id, plan=plan, billing_cycle=cycle, status=status, current_period_end=_now() + timedelta(days=days)
    )


def _subscription(headers):
    return client.get("/api/billing/subscription", headers=headers).json()


def _session(tenant_id, plan="pro", cycle="monthly", **overrides):
    """A Checkout Session as Stripe sends it in checkout.session.* events."""
    session = {
        "id": f"cs_test_{uuid4().hex}",
        "object": "checkout.session",
        "mode": "payment",
        "status": "complete",
        "payment_status": "paid",
        "amount_total": PRICES[(plan, cycle)],
        "currency": "cny",
        "client_reference_id": tenant_id,
        "metadata": {"workspace_id": tenant_id, "plan": plan, "cycle": cycle},
        "payment_intent": f"pi_{uuid4().hex[:24]}",
        "customer_details": {"email": "boss@shop.example"},
        "livemode": False,
    }
    session.update(overrides)
    return session


def _event(session, event_type="checkout.session.completed"):
    return {
        "id": f"evt_{uuid4().hex[:24]}",
        "object": "event",
        "type": event_type,
        "api_version": "2026-09-30.endive",
        "livemode": False,
        "data": {"object": session},
    }


def _signature(payload: bytes, secret=WEBHOOK_SECRET, timestamp=None) -> str:
    ts = int(time.time()) if timestamp is None else int(timestamp)
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def _post(event=None, *, payload=None, signature=None, secret=WEBHOOK_SECRET, timestamp=None, headers=None):
    body = payload if payload is not None else json.dumps(event).encode()
    sent_headers = {"Content-Type": "application/json"}
    if signature is not False:
        sent_headers["Stripe-Signature"] = signature or _signature(body, secret, timestamp)
    sent_headers.update(headers or {})
    return client.post("/api/billing/webhook", content=body, headers=sent_headers)


@pytest.fixture(autouse=True)
def stripe_env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.omniflow.test")
    payments.reset_client()
    yield
    payments.reset_client()


class _FakeSessions:
    def __init__(self):
        self.created = []
        self.sessions = {}
        self.error = None

    def create(self, params=None, options=None):
        if self.error:
            raise self.error
        session_id = f"cs_test_{uuid4().hex}"
        self.created.append(params)
        self.sessions[session_id] = {
            "id": session_id,
            "object": "checkout.session",
            "mode": params["mode"],
            "status": "open",
            "payment_status": "unpaid",
            "amount_total": params["line_items"][0]["price_data"]["unit_amount"],
            "currency": params["line_items"][0]["price_data"]["currency"],
            "client_reference_id": params["client_reference_id"],
            "metadata": dict(params["metadata"]),
            "payment_intent": None,
        }
        return SimpleNamespace(id=session_id, url=f"https://checkout.stripe.com/c/pay/{session_id}")

    def retrieve(self, session_id, params=None, options=None):
        if session_id not in self.sessions:
            import stripe

            raise stripe.InvalidRequestError(f"No such checkout.session: '{session_id}'", "session", http_status=404)
        return SimpleNamespace(to_dict=lambda: json.loads(json.dumps(self.sessions[session_id])))

    def pay(self, session_id):
        self.sessions[session_id].update(status="complete", payment_status="paid", payment_intent=f"pi_{uuid4().hex[:24]}")


@pytest.fixture
def fake_stripe(monkeypatch):
    sessions = _FakeSessions()
    fake = SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=sessions)))
    monkeypatch.setattr(payments, "get_client", lambda: fake)
    return sessions


# ================================================================= webhook: activation
def test_paid_checkout_activates_the_plan():
    tenant_id, headers, _ = _register()
    _set_plan(tenant_id, status="active", days=-1)  # ended: AI generation and channels answer 402
    assert _subscription(headers)["status"] == "expired"

    session = _session(tenant_id, "pro", "monthly")
    res = _post(_event(session))
    assert res.status_code == 200
    assert res.json() == {"received": True, "type": "checkout.session.completed", "result": "activated"}

    sub = _subscription(headers)
    assert (sub["plan"], sub["billing_cycle"], sub["status"], sub["days_left"]) == ("pro", "monthly", "active", 30)
    [payment] = store.list_payments(tenant_id=tenant_id)
    assert payment["id"] == session["id"] and payment["status"] == "paid" and payment["provider"] == "stripe"
    assert (payment["plan"], payment["billing_cycle"], payment["amount"], payment["currency"]) == ("pro", "monthly", 6600, "cny")
    assert payment["reference"] == session["payment_intent"]
    assert plans.parse_time(payment["period_end"]) == plans.parse_time(sub["current_period_end"])


def test_annual_agency_payment_gives_a_year_of_vip():
    tenant_id, headers, _ = _register()
    assert _post(_event(_session(tenant_id, "agency", "annual"))).json()["result"] == "activated"
    sub = _subscription(headers)
    assert (sub["plan"], sub["billing_cycle"], sub["status"]) == ("agency", "annual", "active")
    assert sub["limits"]["ai_runs"] is None and sub["limits"]["channels"] is None
    assert 365 <= sub["days_left"] <= 366  # starts now (a Pro trial does not carry over to VIP)


def test_retries_and_both_events_apply_a_payment_once():
    tenant_id, headers, _ = _register()
    _set_plan(tenant_id, status="active", days=-1)
    session = _session(tenant_id)
    event = _event(session)
    results = [_post(event).json()["result"] for _ in range(3)]
    results.append(_post(_event(session, "checkout.session.async_payment_succeeded")).json()["result"])
    assert results == ["activated", "duplicate", "duplicate", "duplicate"]
    assert _subscription(headers)["days_left"] == 30
    assert len(store.list_payments(tenant_id=tenant_id)) == 1


def test_concurrent_deliveries_apply_a_payment_once():
    tenant_id, headers, _ = _register()
    _set_plan(tenant_id, status="active", days=-1)
    event = _event(_session(tenant_id))
    results = []
    threads = [threading.Thread(target=lambda: results.append(_post(event).json()["result"])) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["activated"] + ["duplicate"] * 7
    assert _subscription(headers)["days_left"] == 30


def test_renewing_early_extends_from_the_current_end():
    tenant_id, headers, _ = _register()
    _set_plan(tenant_id, "pro", days=10)
    assert _post(_event(_session(tenant_id, "pro", "monthly"))).json()["result"] == "activated"
    assert _subscription(headers)["days_left"] == 40
    assert _post(_event(_session(tenant_id, "pro", "annual"))).json()["result"] == "activated"
    sub = _subscription(headers)
    assert sub["days_left"] == 405 and sub["billing_cycle"] == "annual"


def test_paying_during_the_trial_keeps_the_trial_days():
    tenant_id, headers, _ = _register()
    assert _subscription(headers)["status"] == "trial"
    assert _post(_event(_session(tenant_id, "pro", "monthly"))).json()["result"] == "activated"
    sub = _subscription(headers)
    assert sub["status"] == "active" and sub["days_left"] == 37  # 7 trial days + 30 paid


@pytest.mark.parametrize(
    "old_plan, new_plan, days_left, expected",
    [
        ("pro", "agency", 15, 36),    # 15 Pro days are worth ~5.96 VIP days: 30 + 5.96
        ("agency", "pro", 10, 56),    # 10 VIP days are worth ~25.2 Pro days: 30 + 25.2
    ],
)
def test_switching_plans_carries_the_unused_time_over(old_plan, new_plan, days_left, expected):
    tenant_id, headers, _ = _register()
    _set_plan(tenant_id, old_plan, days=days_left)
    assert _post(_event(_session(tenant_id, new_plan, "monthly"))).json()["result"] == "activated"
    sub = _subscription(headers)
    assert sub["plan"] == new_plan and sub["days_left"] == expected


def test_payment_fulfils_pending_upgrade_requests():
    tenant_id, headers, _ = _register()
    client.post("/api/billing/upgrade-request", json={"plan": "agency", "billing_cycle": "monthly"}, headers=headers)
    assert _subscription(headers)["pending_request"] is not None
    _post(_event(_session(tenant_id, "agency", "monthly")))
    assert _subscription(headers)["pending_request"] is None


def test_paid_plan_unlocks_ai_generation(monkeypatch):
    import app.main as main
    from models.schemas import ContentDraft, Platform

    def generate_with_vision(prompt, image_base64=None, campaign=None):
        draft = ContentDraft(campaign_id="c", platform=Platform.META, language="en", body="AI copy",
                             metadata={"status": "ai_generated"})
        return {"copy": "AI copy", "drafts": [draft], "platforms": ["Meta"]}

    monkeypatch.setattr(main.copywriter, "generate_with_vision", generate_with_vision)
    tenant_id, headers, _ = _register()
    _set_plan(tenant_id, status="active", days=-1)
    generate = lambda: client.post("/content/generate", json={"prompt": "yoga mats", "campaign_id": "default-campaign"}, headers=headers)  # noqa: E731
    assert generate().status_code == 402
    _post(_event(_session(tenant_id)))
    assert generate().status_code == 200


# ================================================================= webhook: payments that must not activate
def test_unpaid_session_waits_for_the_async_payment():
    tenant_id, headers, _ = _register()
    session = _session(tenant_id, payment_status="unpaid")
    assert _post(_event(session)).json()["result"] == "pending"
    assert _subscription(headers)["status"] == "trial" and store.list_payments(tenant_id=tenant_id) == []

    paid = {**session, "payment_status": "paid"}
    assert _post(_event(paid, "checkout.session.async_payment_succeeded")).json()["result"] == "activated"
    assert _subscription(headers)["status"] == "active"


def test_failed_async_payment_changes_nothing():
    tenant_id, headers, _ = _register()
    session = _session(tenant_id, payment_status="unpaid")
    res = _post(_event(session, "checkout.session.async_payment_failed"))
    assert res.status_code == 200 and res.json()["result"] == "failed"
    assert _subscription(headers)["status"] == "trial" and store.list_payments(tenant_id=tenant_id) == []


@pytest.mark.parametrize("overrides", [{"amount_total": 100}, {"currency": "usd"}, {"amount_total": PRICES[("pro", "annual")]}])
def test_wrong_amount_is_kept_for_review_not_applied(overrides):
    tenant_id, headers, _ = _register()
    session = _session(tenant_id, "pro", "monthly", **overrides)
    assert _post(_event(session)).json()["result"] == "review"
    assert _subscription(headers)["status"] == "trial"
    [payment] = store.list_payments(tenant_id=tenant_id)
    assert payment["status"] == "review" and payment["period_end"] is None
    assert _post(_event(session)).json()["result"] == "duplicate"  # still recorded only once


def test_house_account_payment_is_kept_for_review():
    session = _session("default", "agency", "annual")
    assert _post(_event(session)).json()["result"] == "review"
    house = store.get_subscription(tenant_id="default")
    assert house["plan"] == "agency" and house["status"] == "active" and house["current_period_end"] is None


def _unknown_workspace(session):
    other = str(uuid4())
    session["metadata"]["workspace_id"] = other
    session["client_reference_id"] = other


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.update(metadata={}),                         # another product on the same Stripe account
        lambda s: s.update(mode="subscription"),
        lambda s: s["metadata"].update(plan="enterprise"),
        lambda s: s["metadata"].update(cycle="weekly"),
        _unknown_workspace,
        lambda s: s.update(client_reference_id="someone-else"),  # metadata and reference disagree
        lambda s: s.update(id="pi_not_a_session"),
    ],
)
def test_events_that_are_not_our_plan_purchases_are_ignored(change):
    tenant_id, headers, _ = _register()
    session = _session(tenant_id)
    change(session)
    res = _post(_event(session))
    assert res.status_code == 200 and res.json()["result"] == "ignored"
    assert _subscription(headers)["status"] == "trial"
    assert store.list_payments(tenant_id=tenant_id) == []


def test_other_event_types_are_acknowledged_and_ignored():
    tenant_id, headers, _ = _register()
    for event_type in ("payment_intent.succeeded", "checkout.session.expired", "charge.refunded"):
        res = _post(_event(_session(tenant_id), event_type))
        assert res.status_code == 200 and res.json()["result"] == "ignored"
    assert _subscription(headers)["status"] == "trial"


# ================================================================= webhook: signature
def test_forged_or_replayed_events_are_rejected():
    tenant_id, headers, _ = _register()
    event = _event(_session(tenant_id, "agency", "annual"))
    body = json.dumps(event).encode()
    tampered = body.replace(b'"agency"', b'"pro"')

    attempts = {
        "wrong secret": _post(event, secret="whsec_attacker_guess_1234567890"),
        "missing header": _post(event, signature=False),
        "garbage header": _post(event, signature="not-a-signature"),
        "tampered body": _post(payload=tampered, signature=_signature(body)),
        "replayed (10 min old)": _post(event, timestamp=time.time() - 600),
    }
    for name, res in attempts.items():
        assert res.status_code == 400, name
        assert res.json()["detail"] == "Invalid Stripe signature or payload", name
    assert _subscription(headers)["status"] == "trial"
    assert store.list_payments(tenant_id=tenant_id) == []


def test_signed_but_malformed_payloads_are_rejected():
    for payload in (b"not json", b"[1, 2, 3]", b'{"no": "type"}', b"\xff\xfe"):
        assert _post(payload=payload).status_code == 400


def test_webhook_needs_its_secret(monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET")
    tenant_id, headers, _ = _register()
    res = _post(_event(_session(tenant_id)))
    assert res.status_code == 503
    assert _subscription(headers)["status"] == "trial"


def test_webhook_works_when_sign_in_is_required(monkeypatch):
    tenant_id, headers, _ = _register()
    monkeypatch.setenv("REQUIRE_AUTH", "true")
    assert _post(_event(_session(tenant_id))).json()["result"] == "activated"
    assert _subscription(headers)["status"] == "active"


def test_oversized_payloads_are_refused():
    assert _post(payload=b"{" + b" " * (600 * 1024) + b"}").status_code == 413


# ================================================================= create-checkout-session
@pytest.mark.parametrize("plan, cycle", sorted(PRICES))
def test_checkout_session_for_every_plan_and_cycle(fake_stripe, plan, cycle):
    tenant_id, headers, email = _register()
    res = client.post("/api/billing/create-checkout-session", json={"plan": plan, "cycle": cycle}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["url"].startswith("https://checkout.stripe.com/") and body["session_id"].startswith("cs_test_")
    assert (body["plan"], body["cycle"], body["currency"]) == (plan, cycle, "CNY")
    assert body["amount"] == plans.PLANS[plan]["prices"][cycle]

    [params] = fake_stripe.created
    assert params["mode"] == "payment"
    assert params["client_reference_id"] == tenant_id
    assert params["metadata"]["workspace_id"] == tenant_id
    assert (params["metadata"]["plan"], params["metadata"]["cycle"]) == (plan, cycle)
    assert params["payment_intent_data"]["metadata"]["workspace_id"] == tenant_id
    [item] = params["line_items"]
    assert item["quantity"] == 1
    assert item["price_data"]["currency"] == "cny" and item["price_data"]["unit_amount"] == PRICES[(plan, cycle)]
    assert params["allowed_payment_method_types"] == ["card", "alipay", "wechat_pay"]
    assert params["payment_method_options"] == {"wechat_pay": {"client": "web"}}
    assert params["success_url"] == "https://app.omniflow.test/?checkout=success&session_id={CHECKOUT_SESSION_ID}"
    assert params["cancel_url"] == "https://app.omniflow.test/?checkout=cancelled"
    assert params["customer_email"] == email

    assert _subscription(headers)["status"] == "trial"  # nothing changes until Stripe confirms payment


def test_checkout_accepts_billing_cycle_and_locale(fake_stripe):
    _, headers, _ = _register()
    res = client.post(
        "/api/billing/create-checkout-session", json={"plan": "agency", "billing_cycle": "annual", "locale": "zh"}, headers=headers
    )
    assert res.status_code == 200 and res.json()["cycle"] == "annual"
    assert fake_stripe.created[0]["locale"] == "zh"


def test_checkout_returns_to_this_server_without_public_base_url(fake_stripe, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL")
    _, headers, _ = _register()
    client.post("/api/billing/create-checkout-session", json={"plan": "pro"}, headers=headers)
    assert fake_stripe.created[0]["success_url"].startswith("http://testserver/?checkout=success")
    assert fake_stripe.created[0]["locale"] == "auto"


@pytest.mark.parametrize(
    "setting, types, wechat_option",
    [("card", ["card"], False), ("card, wechat_pay", ["card", "wechat_pay"], True), ("dashboard", None, True)],
)
def test_payment_methods_setting(fake_stripe, monkeypatch, setting, types, wechat_option):
    monkeypatch.setenv("STRIPE_PAYMENT_METHODS", setting)
    _, headers, _ = _register()
    client.post("/api/billing/create-checkout-session", json={"plan": "pro"}, headers=headers)
    params = fake_stripe.created[0]
    assert params.get("allowed_payment_method_types") == types
    assert ("payment_method_options" in params) == wechat_option
    catalog = client.get("/api/billing/plans").json()
    assert catalog["payments"] == {"provider": "stripe", "payment_methods": types}


def test_checkout_needs_a_signed_in_user_and_a_real_plan(fake_stripe):
    _, headers, _ = _register()
    assert client.post("/api/billing/create-checkout-session", json={"plan": "pro"}).status_code == 401
    for body in ({"plan": "free"}, {"plan": "pro", "cycle": "weekly"}, {}):
        assert client.post("/api/billing/create-checkout-session", json=body, headers=headers).status_code == 422
    assert fake_stripe.created == []


def test_house_account_cannot_be_bought(fake_stripe):
    headers = {"Authorization": f"Bearer {issue_access_token('operator', 'default')}"}
    res = client.post("/api/billing/create-checkout-session", json={"plan": "agency"}, headers=headers)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "PLAN_NOT_PURCHASABLE"
    assert fake_stripe.created == []


def test_without_stripe_the_manual_flow_stays(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    _, headers, _ = _register()
    assert client.get("/api/billing/plans").json()["payments"] is None
    res = client.post("/api/billing/create-checkout-session", json={"plan": "pro"}, headers=headers)
    assert res.status_code == 503 and res.json()["detail"]["code"] == "PAYMENTS_NOT_CONFIGURED"
    assert client.post("/api/billing/upgrade-request", json={"plan": "pro"}, headers=headers).status_code == 200


def test_stripe_errors_become_502_without_leaking_the_key(fake_stripe, caplog):
    import stripe

    fake_stripe.error = stripe.AuthenticationError(f"Invalid API Key provided: {SECRET_KEY}")
    _, headers, _ = _register()
    with caplog.at_level("ERROR"):
        res = client.post("/api/billing/create-checkout-session", json={"plan": "pro"}, headers=headers)
    assert res.status_code == 502
    assert res.json()["detail"] == {"code": "PAYMENT_PROVIDER_ERROR", "message": "Could not open the payment page. Please try again."}
    assert SECRET_KEY not in res.text and SECRET_KEY not in caplog.text
    assert "AuthenticationError" in caplog.text


def test_the_real_sdk_request_goes_through_the_outbound_client(monkeypatch):
    """stripe-python builds the HTTPS request; it must leave through tools.http_client (OUTBOUND_PROXY_URL)."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "cs_test_sdk1234567890", "object": "checkout.session",
                                         "url": "https://checkout.stripe.com/c/pay/cs_test_sdk1234567890"})

    built = []

    def build(timeout=None, **kwargs):
        built.append(timeout)
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(payments, "build_httpx_client", build)
    payments.reset_client()
    session = payments.create_checkout_session(
        tenant_id="t-1", plan="agency", cycle="monthly", base_url="https://app.omniflow.test", email="boss@shop.example"
    )
    assert session == {"id": "cs_test_sdk1234567890", "url": "https://checkout.stripe.com/c/pay/cs_test_sdk1234567890"}
    assert built == [20.0]
    [request] = seen
    assert request.method == "POST" and str(request.url) == "https://api.stripe.com/v1/checkout/sessions"
    assert request.headers["authorization"] == f"Bearer {SECRET_KEY}"
    form = dict(parse_qsl(request.content.decode()))
    assert form["mode"] == "payment" and form["line_items[0][price_data][unit_amount]"] == "16600"
    assert form["metadata[workspace_id]"] == "t-1" and form["client_reference_id"] == "t-1"
    assert form["payment_method_options[wechat_pay][client]"] == "web"
    assert [form[f"allowed_payment_method_types[{i}]"] for i in range(3)] == ["card", "alipay", "wechat_pay"]


# ================================================================= success page
def test_success_page_activates_at_once_and_the_webhook_is_then_a_duplicate(fake_stripe):
    tenant_id, headers, _ = _register()
    session_id = client.post("/api/billing/create-checkout-session", json={"plan": "agency"}, headers=headers).json()["session_id"]

    pending = client.get(f"/api/billing/checkout-session/{session_id}", headers=headers).json()
    assert (pending["status"], pending["paid"], pending["result"]) == ("open", False, "pending")
    assert pending["subscription"]["plan"] == "pro"

    fake_stripe.pay(session_id)
    paid = client.get(f"/api/billing/checkout-session/{session_id}", headers=headers).json()
    assert (paid["paid"], paid["result"]) == (True, "activated")
    assert (paid["subscription"]["plan"], paid["subscription"]["status"]) == ("agency", "active")

    session = fake_stripe.sessions[session_id]
    assert _post(_event(session)).json()["result"] == "duplicate"
    again = client.get(f"/api/billing/checkout-session/{session_id}", headers=headers).json()
    assert again["result"] == "duplicate" and again["paid"] is True
    assert len(store.list_payments(tenant_id=tenant_id)) == 1


def test_success_page_hides_other_workspaces_sessions(fake_stripe):
    _, headers_a, _ = _register()
    _, headers_b, _ = _register()
    session_id = client.post("/api/billing/create-checkout-session", json={"plan": "pro"}, headers=headers_a).json()["session_id"]
    fake_stripe.pay(session_id)
    assert client.get(f"/api/billing/checkout-session/{session_id}", headers=headers_b).status_code == 404
    assert _subscription(headers_a)["status"] == "trial"  # B's look did not apply A's payment either
    assert client.get(f"/api/billing/checkout-session/{session_id}").status_code == 401


def test_success_page_rejects_unknown_and_malformed_ids(fake_stripe):
    _, headers, _ = _register()
    for session_id in ("cs_test_doesnotexist123", "pi_123456789012", "cs_test_<script>", "cs_live_" + "x" * 300):
        assert client.get(f"/api/billing/checkout-session/{session_id}", headers=headers).status_code == 404


# ================================================================= PostgreSQL
PG_URL = os.environ.get("TEST_DATABASE_URL", "")
pg_only = pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set (no PostgreSQL available)")
SQL_FILES = [ROOT / "scripts" / "init_supabase.sql", ROOT / "scripts" / "supabase_subscriptions.sql"]
APP_ROLE = "omniflow_payments_test"


@pytest.fixture
def pg_store():
    import psycopg

    from db.postgres_store import PostgresStore

    with psycopg.connect(PG_URL, autocommit=True) as conn:
        for sql in SQL_FILES:
            conn.execute(sql.read_text(encoding="utf-8"))
            conn.execute(sql.read_text(encoding="utf-8"))  # idempotent
        conn.execute("TRUNCATE subscriptions, usage_tracking, upgrade_requests, billing_payments")
    s = PostgresStore(PG_URL, min_size=1, max_size=12)
    yield s
    s.close()


@pg_only
def test_postgres_payment_is_applied_once_under_concurrency(pg_store):
    tenant = pg_store.create_tenant(f"Pay {uuid4().hex[:6]}")["id"]
    plans.start_trial(pg_store, tenant)
    session = _session(tenant, "pro", "monthly")
    results = []

    def deliver():
        results.append(payments.fulfill_checkout_session(pg_store, session, source="test")["result"])

    threads = [threading.Thread(target=deliver) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["activated"] + ["duplicate"] * 9
    sub = plans.subscription_summary(pg_store, tenant)
    assert sub["status"] == "active" and sub["days_left"] == 37  # trial days kept, applied once
    [payment] = pg_store.list_payments(tenant_id=tenant)
    assert payment["status"] == "paid" and payment["amount"] == 6600 and payment["reference"] == session["payment_intent"]
    assert plans.parse_time(payment["period_end"]) == plans.parse_time(sub["current_period_end"])

    review = payments.fulfill_checkout_session(pg_store, _session(tenant, "pro", "monthly", amount_total=1), source="test")
    assert review["result"] == "review" and plans.subscription_summary(pg_store, tenant)["days_left"] == 37


@pg_only
def test_postgres_payments_are_private_to_the_workspace_and_read_only_for_clients(pg_store):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    tenant_a = pg_store.create_tenant("A")["id"]
    tenant_b = pg_store.create_tenant("B")["id"]
    for tenant in (tenant_a, tenant_b):
        plans.start_trial(pg_store, tenant)
        payments.fulfill_checkout_session(pg_store, _session(tenant), source="test")

    with psycopg.connect(PG_URL, autocommit=True) as owner:
        owner.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")
        owner.execute(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'rlspw' NOSUPERUSER NOBYPASSRLS")
        owner.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
        owner.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON billing_payments TO {APP_ROLE}")
    url = make_conninfo(**{**conninfo_to_dict(PG_URL), "user": APP_ROLE, "password": "rlspw"})
    try:
        with psycopg.connect(url) as conn:
            conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_a,))
            assert [r[0] for r in conn.execute("SELECT tenant_id FROM billing_payments")] == [tenant_a]
            assert conn.execute("UPDATE billing_payments SET amount = 1 WHERE tenant_id = %s", (tenant_b,)).rowcount == 0
            conn.commit()
            conn.execute("SELECT set_config('app.tenant_id', '', false)")
            assert conn.execute("SELECT count(*) FROM billing_payments").fetchone()[0] == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "INSERT INTO billing_payments (id, tenant_id, plan, billing_cycle, amount, currency) "
                    "VALUES ('cs_test_forged', %s, 'agency', 'annual', 0, 'cny')",
                    (tenant_b,),
                )
            conn.rollback()
    finally:
        with psycopg.connect(PG_URL, autocommit=True) as owner:
            owner.execute(f"REASSIGN OWNED BY {APP_ROLE} TO CURRENT_USER")
            owner.execute(f"DROP OWNED BY {APP_ROLE}")
            owner.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")
