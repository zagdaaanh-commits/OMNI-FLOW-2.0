"""Privacy: no secrets / Facebook ids in client-facing errors, and no user data without a session."""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from app.redaction import describe_exception, redact
from tools.meta_api import parse_graph_error

client = TestClient(app)

APP_ID = "2550662608771439"
USER_ID = "10223344556677889"


# ------------------------------------------------------------------------- redaction
@pytest.mark.parametrize(
    "raw,leak",
    [
        (f"Error validating access token: The user has not authorized application {APP_ID}.", APP_ID),
        (f"(#100) Object with ID '{USER_ID}' does not exist", USER_ID),
        ("Client error '400' for url 'https://graph.facebook.com/v23.0/me?access_token=EAABsecretvalue123&fields=id'", "EAABsecretvalue123"),
        ("token EAAGm0PX4ZCpsBAKZCZBa1b2c3d4e5f6g7h8i9j0 rejected", "EAAGm0PX4ZCpsBAKZCZBa1b2c3d4e5f6g7h8i9j0"),
        ("Authorization: Bearer abcdefghijklmnop.qrstu", "abcdefghijklmnop.qrstu"),
        ("Incorrect API key provided: sk-proj-abcdefghijklmnop", "sk-proj-abcdefghijklmnop"),
        ("bad key 0123456789abcdef0123456789abcdef.AbCdEfGhIjKlMnOp", "0123456789abcdef0123456789abcdef.AbCdEfGhIjKlMnOp"),
        ("proxy http://user:hunter2@10.0.0.1:7890 refused", "hunter2"),
        ("redirect to https://x.com/cb?code=AQDsecretcode&state=1", "AQDsecretcode"),
    ],
)
def test_redact_removes_secrets_and_facebook_ids(raw, leak):
    assert leak not in redact(raw)


def test_redact_removes_configured_secrets_verbatim(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "my-app-secret-value")
    assert "my-app-secret-value" not in redact("signature mismatch for my-app-secret-value")


def test_redact_keeps_ordinary_text():
    text = "(#200) Requires pages_manage_metadata permission. Retry in 30 seconds (code 190)."
    assert redact(text) == text
    assert redact(None) == ""


def test_describe_exception_is_redacted_and_capped():
    exc = RuntimeError(f"failed for user {USER_ID} " + "x" * 500)
    text = describe_exception(exc)
    assert text.startswith("RuntimeError: ") and USER_ID not in text and len(text) <= 300


def test_graph_errors_are_redacted_at_the_source():
    err = parse_graph_error({"error": {"message": f"The user has not authorized application {APP_ID}.", "code": 190}}, 400)
    assert APP_ID not in err["message"] and err["is_token_error"] is True


# ------------------------------------------------------ errors that reach a response
def test_facebook_status_never_shows_ids_from_graph_errors(graph_stub, monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "PAGE_X")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "EAABpagetokenvalue1234567890")
    graph_stub.add("GET", "/PAGE_X?", status=400, json={"error": {"message": f"(#100) Object with ID '{USER_ID}' does not exist", "code": 100}})
    body = client.get("/tools/facebook/status").text
    assert USER_ID not in body and "EAABpagetokenvalue" not in body


def test_expired_token_message_is_generic(graph_stub, monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "PAGE_X")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "EAABpagetokenvalue1234567890")
    graph_stub.add("GET", "/PAGE_X?", status=400, json={"error": {"message": f"The user has not authorized application {APP_ID}.", "code": 190}})
    data = client.get("/tools/facebook/status").json()
    assert data["connected"] is False and APP_ID not in str(data)
    assert "reconnect" in data["error"].lower()


def test_reply_errors_do_not_echo_facebook_ids(graph_stub):
    graph_stub.add("POST", "/111_222/comments", status=400, json={"error": {"message": f"Unsupported post request. Object with ID '{USER_ID}' does not exist", "code": 100}})
    res = client.post("/api/meta/reply-comment", json={"comment_id": "111_222", "message": "Hi", "page_token": "T"})
    assert res.status_code == 400 and USER_ID not in res.text


def test_token_verification_transport_errors_are_generic(graph_stub):
    def boom(request):
        raise httpx.ConnectError(f"connection failed for {request.url}", request=request)

    graph_stub.add("GET", "graph.facebook.com", handler=boom)
    res = client.post("/tools/facebook/update-token", json={"access_token": "EAABsecrettoken123456789", "page_id": "1234567890123"})
    assert res.status_code == 502
    assert res.json() == {"detail": "Could not reach Facebook. Please try again."}


def test_unhandled_errors_return_a_generic_500():
    path = f"/__test_crash_{uuid4().hex}"

    @app.get(path)
    def _crash():
        raise RuntimeError(f"database password=hunter2 for app {APP_ID}")

    res = TestClient(app, raise_server_exceptions=False).get(path)
    assert res.status_code == 500
    assert res.json() == {"detail": "Internal server error"}


# ------------------------------------------------------- user data needs a session
def _register():
    res = client.post("/auth/register", json={"email": f"p_{uuid4().hex[:8]}@example.com", "password": "Passw0rd!", "full_name": "Priv User"})
    assert res.status_code == 200, res.text
    body = res.json()
    return body["user"], {"Authorization": f"Bearer {body['token']}"}


def test_anonymous_callers_never_see_another_account():
    store.create_user(f"default_{uuid4().hex[:6]}@example.com", "Default Owner", "Passw0rd!", tenant_id="default")
    res = client.get("/auth/me")
    assert res.status_code == 401
    assert "Default Owner" not in res.text and "@example.com" not in res.text


def test_user_list_requires_a_session_and_is_tenant_scoped():
    assert client.get("/auth/users").status_code == 401
    user, headers = _register()
    other, _ = _register()
    listed = client.get("/auth/users", headers=headers).json()
    assert [u["id"] for u in listed] == [user["id"]]
    assert other["email"] not in str(listed)
