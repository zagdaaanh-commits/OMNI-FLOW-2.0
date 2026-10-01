"""Agency application intake and the Meta comment webhook / reply pipeline."""
from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

import app.routers.agency as agency
from app.main import app, store

client = TestClient(app)

VALID_CODE = "91330100799655058B"  # real Unified Social Credit Code format with a valid checksum


def _application(**overrides):
    body = {
        "company_name": "Shenzhen Example Trading Co., Ltd.",
        "credit_code": VALID_CODE,
        "store_url": "https://shop.example.com",
        "contact": "wechat: example_trading",
        "remarks": "Monthly budget about 5,000 USD",
    }
    body.update(overrides)
    return body


def _new_page_id() -> str:
    return str(10**14 + uuid4().int % 10**13)


def _register():
    email = f"comments_{uuid4().hex[:8]}@example.com"
    res = client.post("/auth/register", json={"email": email, "password": "Passw0rd!", "full_name": "Test User"})
    assert res.status_code == 200, res.text
    body = res.json()
    return body["user"]["tenant_id"], {"Authorization": f"Bearer {body['token']}"}


def _comment_event(page_id: str, comment_id: str, *, item: str = "comment", **value):
    change_value = {
        "item": item,
        "verb": "add",
        "comment_id": comment_id,
        "post_id": f"{page_id}_900",
        "parent_id": f"{page_id}_900",
        "from": {"id": "555", "name": "Buyer One"},
        "message": "Do you ship to Germany?",
        "created_time": 1790000000,
    }
    change_value.update(value)
    return {"object": "page", "entry": [{"id": page_id, "time": 1790000001, "changes": [{"field": "feed", "value": change_value}]}]}


# ------------------------------------------------------------------ agency intake
def test_agency_application_is_stored():
    res = client.post("/api/agency/apply", json=_application())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success" and body["message"] == "Application received" and body["id"]

    saved = next(a for a in store.list_agency_applications(tenant_id="default") if a["id"] == body["id"])
    assert saved["credit_code"] == VALID_CODE
    assert saved["store_url"] == "https://shop.example.com"
    assert saved["status"] == "received"


def test_agency_application_normalises_input():
    res = client.post(
        "/api/agency/apply",
        json=_application(credit_code=" 91330100799655058b ", store_url="shop.example.com", remarks="   "),
    )
    assert res.status_code == 200, res.text
    saved = next(a for a in store.list_agency_applications(tenant_id="default") if a["id"] == res.json()["id"])
    assert saved["credit_code"] == VALID_CODE
    assert saved["store_url"] == "https://shop.example.com"
    assert saved["remarks"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"credit_code": "12345"},
        {"credit_code": "91330100799655058A"},  # checksum mismatch
        {"credit_code": "91330100799655058I"},  # letter I is never used
        {"store_url": "not a url"},
        {"store_url": "ftp://shop.example.com"},
        {"company_name": "   "},
        {"contact": ""},
    ],
)
def test_invalid_agency_application_is_rejected(overrides):
    before = len(store.list_agency_applications(tenant_id="default"))
    res = client.post("/api/agency/apply", json=_application(**overrides))
    assert res.status_code == 422
    assert len(store.list_agency_applications(tenant_id="default")) == before


def test_agency_application_requires_all_fields():
    assert client.post("/api/agency/apply", json={"company_name": "Only a name"}).status_code == 422


def test_agency_application_is_scoped_to_the_callers_workspace():
    tenant_id, headers = _register()
    res = client.post("/api/agency/apply", json=_application(), headers=headers)
    assert res.status_code == 200
    assert [a["id"] for a in store.list_agency_applications(tenant_id=tenant_id)] == [res.json()["id"]]


def _mock_webhook_client(monkeypatch, handler):
    monkeypatch.setattr(
        agency,
        "build_async_httpx_client",
        lambda timeout=None, **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_lead_webhook_receives_the_application(monkeypatch):
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    monkeypatch.setenv("LEAD_NOTIFICATION_WEBHOOK", "https://hooks.example.com/leads")
    _mock_webhook_client(monkeypatch, handler)

    res = client.post("/api/agency/apply", json=_application())
    assert res.status_code == 200
    assert len(captured) == 1
    assert str(captured[0].url) == "https://hooks.example.com/leads"
    sent = json.loads(captured[0].content)
    assert sent["event"] == "agency_application.created"
    assert sent["application"]["id"] == res.json()["id"]
    assert sent["application"]["credit_code"] == VALID_CODE
    assert sent["application"]["contact"] == "wechat: example_trading"


def test_lead_webhook_failure_does_not_fail_the_submission(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    monkeypatch.setenv("LEAD_NOTIFICATION_WEBHOOK", "https://hooks.example.com/leads")
    _mock_webhook_client(monkeypatch, handler)
    res = client.post("/api/agency/apply", json=_application())
    assert res.status_code == 200 and res.json()["status"] == "success"


def test_no_lead_webhook_without_a_valid_url(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be called
        raise AssertionError("webhook must not be called")

    _mock_webhook_client(monkeypatch, handler)
    for value in ("", "not-a-url"):
        monkeypatch.setenv("LEAD_NOTIFICATION_WEBHOOK", value)
        assert client.post("/api/agency/apply", json=_application()).status_code == 200


# ----------------------------------------------------------- webhook verification
def _verify(token: str, mode: str = "subscribe", challenge: str = "1158201444"):
    return client.get("/api/meta/webhook", params={"hub.mode": mode, "hub.challenge": challenge, "hub.verify_token": token})


def test_webhook_verification_returns_the_challenge(monkeypatch):
    monkeypatch.setenv("META_VERIFY_TOKEN", "s3cret-verify")
    res = _verify("s3cret-verify")
    assert res.status_code == 200
    assert res.text == "1158201444"
    assert res.headers["content-type"].startswith("text/plain")


def test_webhook_verification_default_token_outside_production(monkeypatch):
    monkeypatch.delenv("META_VERIFY_TOKEN", raising=False)
    assert _verify("omniflow_verify_token").status_code == 200


@pytest.mark.parametrize("token,mode", [("wrong-token", "subscribe"), ("s3cret-verify", "unsubscribe"), ("", "subscribe")])
def test_webhook_verification_rejects_bad_requests(monkeypatch, token, mode):
    monkeypatch.setenv("META_VERIFY_TOKEN", "s3cret-verify")
    assert _verify(token, mode).status_code == 403


def test_production_requires_an_explicit_verify_token(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("META_VERIFY_TOKEN", raising=False)
    assert _verify("omniflow_verify_token").status_code == 403


# -------------------------------------------------------------- webhook ingestion
def test_webhook_stores_comment_for_the_workspace_that_owns_the_page():
    tenant_id = store.create_tenant("Webhook Co")["id"]
    page_id = _new_page_id()
    store.save_connected_account("global", "meta", page_id, "Webhook Page", "PAGE_TOKEN", tenant_id=tenant_id)

    res = client.post("/api/meta/webhook", json=_comment_event(page_id, f"{page_id}_900_1"))
    assert res.status_code == 200 and res.json() == {"status": "EVENT_RECEIVED"}

    saved = store.get_page_comment(f"{page_id}_900_1", tenant_id=tenant_id)
    assert saved is not None
    assert saved["page_id"] == page_id
    assert saved["post_id"] == f"{page_id}_900"
    assert saved["from_id"] == "555" and saved["from_name"] == "Buyer One"
    assert saved["message"] == "Do you ship to Germany?"
    assert saved["created_time"].startswith("2026-09-21T")  # 1790000000 in UTC
    assert store.get_page_comment(f"{page_id}_900_1", tenant_id="default") is None


def test_webhook_edit_and_remove_update_the_stored_comment():
    tenant_id = store.create_tenant("Edits Co")["id"]
    page_id = _new_page_id()
    comment_id = f"{page_id}_900_2"
    store.save_connected_account("global", "meta", page_id, "Edits Page", "PAGE_TOKEN", tenant_id=tenant_id)

    client.post("/api/meta/webhook", json=_comment_event(page_id, comment_id))
    client.post("/api/meta/webhook", json=_comment_event(page_id, comment_id, verb="edited", message="Do you ship to France?"))
    edited = store.get_page_comment(comment_id, tenant_id=tenant_id)
    assert edited["message"] == "Do you ship to France?" and edited["verb"] == "edited"

    removal = _comment_event(page_id, comment_id, verb="remove")
    del removal["entry"][0]["changes"][0]["value"]["message"]
    client.post("/api/meta/webhook", json=removal)
    removed = store.get_page_comment(comment_id, tenant_id=tenant_id)
    assert removed["verb"] == "remove" and removed["message"] == "Do you ship to France?"
    assert len(store.list_page_comments(tenant_id=tenant_id)) == 1


def test_default_workspace_owns_the_environment_page(monkeypatch):
    page_id = _new_page_id()
    monkeypatch.setenv("FACEBOOK_PAGE_ID", page_id)
    client.post("/api/meta/webhook", json=_comment_event(page_id, f"{page_id}_900_3"))
    assert store.get_page_comment(f"{page_id}_900_3", tenant_id="default") is not None


def test_webhook_ignores_unknown_pages_and_other_changes():
    tenant_id = store.create_tenant("Ignore Co")["id"]
    page_id = _new_page_id()
    store.save_connected_account("global", "meta", page_id, "Ignore Page", "PAGE_TOKEN", tenant_id=tenant_id)

    stranger = _new_page_id()
    assert client.post("/api/meta/webhook", json=_comment_event(stranger, f"{stranger}_1_1")).status_code == 200
    assert client.post("/api/meta/webhook", json=_comment_event(page_id, f"{page_id}_1_2", item="reaction")).status_code == 200
    assert client.post("/api/meta/webhook", json={"object": "instagram", "entry": []}).status_code == 200
    assert store.list_page_comments(tenant_id=tenant_id) == []
    assert store.get_page_comment(f"{stranger}_1_1", tenant_id="default") is None


def test_webhook_rejects_malformed_bodies():
    assert client.post("/api/meta/webhook", content=b"not json", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/api/meta/webhook", json=["a", "list"]).status_code == 400


def test_webhook_signature_is_enforced_when_app_secret_is_set(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "app-secret")
    tenant_id = store.create_tenant("Signed Co")["id"]
    page_id = _new_page_id()
    store.save_connected_account("global", "meta", page_id, "Signed Page", "PAGE_TOKEN", tenant_id=tenant_id)
    body = json.dumps(_comment_event(page_id, f"{page_id}_900_4")).encode()
    headers = {"content-type": "application/json"}

    assert client.post("/api/meta/webhook", content=body, headers=headers).status_code == 403
    bad = {**headers, "X-Hub-Signature-256": "sha256=" + "0" * 64}
    assert client.post("/api/meta/webhook", content=body, headers=bad).status_code == 403
    assert store.get_page_comment(f"{page_id}_900_4", tenant_id=tenant_id) is None

    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    good = {**headers, "X-Hub-Signature-256": signature}
    assert client.post("/api/meta/webhook", content=body, headers=good).status_code == 200
    assert store.get_page_comment(f"{page_id}_900_4", tenant_id=tenant_id) is not None


def test_production_rejects_unsigned_events_without_an_app_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    assert client.post("/api/meta/webhook", json=_comment_event(_new_page_id(), "1_2")).status_code == 403


# ------------------------------------------------------------------ comment reply
def _form(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def test_reply_with_an_explicit_page_token(graph_stub):
    graph_stub.add("POST", "/111_222/comments", json={"id": "111_333"})
    res = client.post("/api/meta/reply-comment", json={"comment_id": "111_222", "message": "Thanks!", "page_token": "PAGE_TOKEN"})
    assert res.status_code == 200, res.text
    assert res.json() == {"id": "111_333"}

    (request,) = graph_stub.requests_to("/111_222/comments")
    assert request.url.host == "graph.facebook.com"
    assert _form(request) == {"message": "Thanks!", "access_token": "PAGE_TOKEN"}


def test_reply_uses_the_token_of_the_page_that_received_the_comment(graph_stub):
    tenant_id, headers = _register()
    page_id = _new_page_id()
    comment_id = f"{page_id}_900_5"
    store.save_connected_account("global", "meta", page_id, "Tenant Page", "TENANT_PAGE_TOKEN", tenant_id=tenant_id)
    client.post("/api/meta/webhook", json=_comment_event(page_id, comment_id))

    graph_stub.add("POST", f"/{comment_id}/comments", json={"id": f"{page_id}_900_6"})
    res = client.post("/api/meta/reply-comment", json={"comment_id": comment_id, "message": "Yes, we do!"}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json() == {"id": f"{page_id}_900_6"}
    assert _form(graph_stub.requests_to(f"/{comment_id}/comments")[0])["access_token"] == "TENANT_PAGE_TOKEN"


def test_reply_without_any_page_token_is_rejected(graph_stub):
    res = client.post("/api/meta/reply-comment", json={"comment_id": "111_222", "message": "Hi"})
    assert res.status_code == 400
    assert "No Facebook Page is connected" in res.json()["detail"]
    assert graph_stub.calls == []


def test_reply_surfaces_graph_errors(graph_stub):
    graph_stub.add(
        "POST", "/111_222/comments", status=400,
        json={"error": {"message": "Invalid OAuth access token.", "type": "OAuthException", "code": 190}},
    )
    res = client.post("/api/meta/reply-comment", json={"comment_id": "111_222", "message": "Hi", "page_token": "EXPIRED"})
    assert res.status_code == 400
    assert "Invalid OAuth access token." in res.json()["detail"]


def test_reply_transport_failure_is_a_502(graph_stub):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    graph_stub.add("POST", "/111_222/comments", handler=boom)
    res = client.post("/api/meta/reply-comment", json={"comment_id": "111_222", "message": "Hi", "page_token": "PAGE_TOKEN"})
    assert res.status_code == 502


@pytest.mark.parametrize("comment_id", ["me/feed", "111_222/comments", "../111", "abc"])
def test_reply_rejects_non_graph_comment_ids(graph_stub, comment_id):
    res = client.post("/api/meta/reply-comment", json={"comment_id": comment_id, "message": "Hi", "page_token": "T"})
    assert res.status_code == 422
    assert graph_stub.calls == []


def test_reply_rejects_blank_messages(graph_stub):
    res = client.post("/api/meta/reply-comment", json={"comment_id": "111_222", "message": "   ", "page_token": "T"})
    assert res.status_code == 422
    assert graph_stub.calls == []
