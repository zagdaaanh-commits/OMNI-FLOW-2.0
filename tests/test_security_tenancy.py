"""Signed tokens and tenant resolution."""
from __future__ import annotations

import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import security
from app.security import InvalidTokenError, sign_payload, verify_payload
from app.tenancy import (
    DEFAULT_TENANT_ID,
    TenantContext,
    get_tenant_context,
    issue_access_token,
    require_authenticated,
)


# ------------------------------------------------------------------ security
def test_sign_and_verify_roundtrip():
    token = sign_payload({"tid": "t1", "sub": "u1"}, 60)
    claims = verify_payload(token)
    assert claims["tid"] == "t1" and claims["sub"] == "u1"
    assert claims["exp"] > claims["iat"]


def test_tampered_payload_or_signature_rejected():
    token = sign_payload({"tid": "t1"}, 60)
    body, sig = token.split(".")
    forged_body = sign_payload({"tid": "victim"}, 60).split(".")[0]
    for bad in (f"{forged_body}.{sig}", f"{body}.{sig[:-2]}xx", "garbage", "", "a.b.c"):
        with pytest.raises(InvalidTokenError):
            verify_payload(bad)


def test_expired_token_rejected(monkeypatch):
    token = sign_payload({"tid": "t1"}, 1)
    real_time = time.time
    monkeypatch.setattr("app.security.time.time", lambda: real_time() + 5)
    with pytest.raises(InvalidTokenError, match="expired"):
        verify_payload(token)


def test_different_secret_invalidates_tokens(monkeypatch):
    token = sign_payload({"tid": "t1"}, 60)
    monkeypatch.setenv("APP_SECRET_KEY", "another-secret-key-for-this-test-0000000000")
    with pytest.raises(InvalidTokenError):
        verify_payload(token)


def test_production_requires_strong_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_SECRET_KEY", "")
    with pytest.raises(RuntimeError, match="required"):
        security.get_secret_key()
    monkeypatch.setenv("APP_SECRET_KEY", "short")
    with pytest.raises(RuntimeError, match="at least 32"):
        security.get_secret_key()


def test_dev_without_secret_uses_ephemeral_key(monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "")
    monkeypatch.setattr(security, "_ephemeral_key", None)
    key = security.get_secret_key()
    assert len(key) == 32 and security.get_secret_key() == key


def test_ttl_must_be_positive():
    with pytest.raises(ValueError):
        sign_payload({}, 0)


# ------------------------------------------------------------------- tenancy
def _app() -> TestClient:
    app = FastAPI()

    @app.get("/whoami")
    def whoami(ctx: TenantContext = Depends(get_tenant_context)):
        return {"tenant_id": ctx.tenant_id, "user_id": ctx.user_id, "authenticated": ctx.authenticated}

    @app.get("/private")
    def private(ctx: TenantContext = Depends(require_authenticated)):
        return {"tenant_id": ctx.tenant_id}

    return TestClient(app)


def test_anonymous_requests_use_default_tenant():
    body = _app().get("/whoami").json()
    assert body == {"tenant_id": DEFAULT_TENANT_ID, "user_id": None, "authenticated": False}


def test_bearer_token_selects_tenant():
    token = issue_access_token("user-1", "acme")
    body = _app().get("/whoami", headers={"Authorization": f"Bearer {token}"}).json()
    assert body == {"tenant_id": "acme", "user_id": "user-1", "authenticated": True}


def test_invalid_bearer_is_401_not_silent_downgrade():
    client = _app()
    assert client.get("/whoami", headers={"Authorization": "Bearer omt_stale"}).status_code == 401
    expired = issue_access_token("u", "acme", ttl_seconds=1)
    time.sleep(1.1)
    assert client.get("/whoami", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_non_access_tokens_are_rejected():
    oauth_state = sign_payload({"typ": "meta_oauth_state", "tid": "acme"}, 60)
    assert _app().get("/whoami", headers={"Authorization": f"Bearer {oauth_state}"}).status_code == 401


def test_require_auth_env_blocks_anonymous(monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "true")
    client = _app()
    assert client.get("/whoami").status_code == 401
    token = issue_access_token("u", "acme")
    assert client.get("/whoami", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_tenant_header_only_when_explicitly_enabled(monkeypatch):
    client = _app()
    assert client.get("/whoami", headers={"X-Tenant-ID": "acme"}).json()["tenant_id"] == DEFAULT_TENANT_ID
    monkeypatch.setenv("ALLOW_TENANT_HEADER", "true")
    assert client.get("/whoami", headers={"X-Tenant-ID": "acme"}).json()["tenant_id"] == "acme"
    assert client.get("/whoami", headers={"X-Tenant-ID": "../evil"}).status_code == 400
    monkeypatch.setenv("APP_ENV", "production")
    assert client.get("/whoami", headers={"X-Tenant-ID": "acme"}).json()["tenant_id"] == DEFAULT_TENANT_ID


def test_require_authenticated_dependency():
    client = _app()
    assert client.get("/private").status_code == 401
    token = issue_access_token("u", "acme")
    assert client.get("/private", headers={"Authorization": f"Bearer {token}"}).json() == {"tenant_id": "acme"}
