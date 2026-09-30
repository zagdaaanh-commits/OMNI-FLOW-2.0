"""MetaAPIClient against a mocked Graph API (no network)."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tools.http_client import get_http_client
from tools.meta_api import MetaAPIClient, MetaOAuthError, parse_graph_error

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PAGE = "101728504668130"


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", PAGE)
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "EAAB_page_token")


@pytest.fixture
def oauth_env(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "app-123")
    monkeypatch.setenv("META_APP_SECRET", "shhh-secret")


def _query(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


# ------------------------------------------------------------------- errors
def test_parse_graph_error_classification():
    expired = parse_graph_error({"error": {"message": "Error validating access token: Session has expired", "code": 190, "fbtrace_id": "T"}}, 400)
    assert expired["is_token_error"] and expired["fbtrace_id"] == "T"
    perm = parse_graph_error({"error": {"message": "(#200) Requires pages_manage_posts permission", "code": 200}}, 403)
    assert perm["is_permission_error"] and not perm["is_token_error"]
    other = parse_graph_error({}, 500, "boom")
    assert other["message"] == "boom" and not other["is_token_error"]


def test_default_graph_version_is_current(monkeypatch):
    monkeypatch.setenv("META_GRAPH_VERSION", "")
    assert MetaAPIClient().base_url == "https://graph.facebook.com/v23.0"


# --------------------------------------------------------------- feed / photo
def test_feed_publish_success(graph_stub, creds):
    graph_stub.add("POST", f"/{PAGE}/feed", json={"id": f"{PAGE}_555"})
    res = MetaAPIClient().publish_facebook_page_feed("Hello world")
    assert res["success"] is True
    assert res["id"] == res["post_id"] == res["external_post_id"] == f"{PAGE}_555"
    assert res["post_url"] == f"https://www.facebook.com/{PAGE}_555"
    body = graph_stub.body_text(graph_stub.calls[0])
    assert "message=Hello+world" in body and "access_token=EAAB_page_token" in body


def test_feed_publish_expired_token(graph_stub, creds):
    graph_stub.add("POST", "/feed", status=400, json={"error": {"message": "Error validating access token", "code": 190}})
    res = MetaAPIClient().publish_facebook_page_feed("x")
    assert res["success"] is False and res["mode"] == "token_expired"
    assert res["post_url"] is None and res["confirmation_badge"] == "Token Expired 🟡"
    assert "expired" in res["error"].lower()


def test_feed_publish_api_error_keeps_details(graph_stub, creds):
    graph_stub.add("POST", "/feed", status=400, json={"error": {"message": "Duplicate status", "code": 506, "fbtrace_id": "abc"}})
    res = MetaAPIClient().publish_facebook_page_feed("x")
    assert res["mode"] == "api_error" and res["graph_error"]["fbtrace_id"] == "abc"


def test_feed_publish_network_error(creds, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("no route to host")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    monkeypatch.setattr("tools.meta_api.get_http_client", lambda: client)
    res = MetaAPIClient().publish_facebook_page_feed("x")
    assert res["success"] is False and res["mode"] == "network_exception"
    assert res["post_url"] is None


def test_publish_requires_credentials():
    res = MetaAPIClient().publish_facebook_page_feed("x")
    assert res["success"] is False and res["mode"] == "missing_credentials"


def test_photo_publish_uses_post_id_for_url(graph_stub, creds):
    graph_stub.add("POST", f"/{PAGE}/photos", json={"id": "777", "post_id": f"{PAGE}_888"})
    res = MetaAPIClient().publish_facebook_page_photo(PNG, "caption")
    assert res["success"] and res["id"] == res["post_id"] == res["external_post_id"] == f"{PAGE}_888"
    assert res["photo_id"] == "777"
    assert res["post_url"] == f"https://www.facebook.com/{PAGE}_888"
    request = graph_stub.calls[0]
    assert "multipart/form-data" in request.headers["content-type"]
    assert b'name="source"' in request.content and b"image/png" in request.content
    assert _query(request)["access_token"] == "EAAB_page_token"


def test_photo_publish_without_post_id_falls_back_to_photo_id(graph_stub, creds):
    graph_stub.add("POST", "/photos", json={"id": f"{PAGE}_999"})
    res = MetaAPIClient().publish_facebook_page_photo(PNG, "c")
    assert res["post_id"] == f"{PAGE}_999"


def test_page_token_is_derived_from_user_token_and_rejection_is_reported(graph_stub, monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", PAGE)
    monkeypatch.setenv("FACEBOOK_USER_ACCESS_TOKEN", "USER_TOKEN")
    seen_tokens = []

    def photos(request):
        seen_tokens.append(_query(request)["access_token"])
        return httpx.Response(400, json={"error": {"message": "Invalid OAuth access token", "code": 190}})

    graph_stub.add("GET", "/me/accounts", json={"data": [{"id": PAGE, "access_token": "PAGE_TOKEN"}]})
    graph_stub.add("POST", "/photos", handler=photos)
    res = MetaAPIClient().publish_facebook_page_photo(PNG, "c")
    # The Page token was derived from the user token; Graph rejecting it (and no different token
    # being available to retry with) is reported honestly, with exactly one publish attempt.
    assert seen_tokens == ["PAGE_TOKEN"]
    assert res["success"] is False and res["mode"] == "token_expired"


def test_photo_retries_once_when_a_fresher_page_token_is_available(graph_stub, creds):
    seen_tokens = []

    def photos(request):
        seen_tokens.append(_query(request)["access_token"])
        if len(seen_tokens) == 1:
            MetaAPIClient._cached_page_tokens[PAGE] = "FRESH_TOKEN"  # e.g. refreshed by another request
            return httpx.Response(400, json={"error": {"message": "Session has expired", "code": 190}})
        return httpx.Response(200, json={"id": "1", "post_id": f"{PAGE}_2"})

    graph_stub.add("POST", "/photos", handler=photos)
    res = MetaAPIClient().publish_facebook_page_photo(PNG, "c")
    assert seen_tokens == ["EAAB_page_token", "FRESH_TOKEN"]
    assert res["success"] is True and res["post_id"] == f"{PAGE}_2"


def test_explicit_tenant_credentials_are_used_verbatim(graph_stub):
    graph_stub.add("POST", "/555/photos", json={"id": "1", "post_id": "555_1"})
    res = MetaAPIClient().publish_facebook_page_photo(PNG, "c", page_id="555", access_token="TENANT_TOKEN")
    assert res["success"]
    assert "/555/photos" in str(graph_stub.calls[0].url) and _query(graph_stub.calls[0])["access_token"] == "TENANT_TOKEN"


# --------------------------------------------------------- brand page creation
def test_create_brand_page_success(graph_stub):
    graph_stub.add("POST", "/me/accounts", json={"id": "424242", "access_token": "NEW_PAGE_TOKEN"})
    res = MetaAPIClient().create_brand_page("USER_TOKEN", "Acme Shop", about="We sell things")
    assert res["success"] is True
    assert res["page_id"] == "424242" and res["page_name"] == "Acme Shop"
    assert res["page_url"] == "https://www.facebook.com/424242"
    assert res["access_token"] == "NEW_PAGE_TOKEN"
    assert "access_token" not in (res["raw"] or {})
    body = graph_stub.body_text(graph_stub.calls[0])
    assert "name=Acme+Shop" in body and "category_enum=E_COMMERCE_WEBSITE" in body and "about=We+sell+things" in body
    assert MetaAPIClient._cached_page_tokens["424242"] == "NEW_PAGE_TOKEN"


def test_create_brand_page_permission_denied_is_explained(graph_stub):
    graph_stub.add("POST", "/me/accounts", status=403, json={"error": {"message": "(#200) Requires pages_manage_metadata permission", "code": 200}})
    res = MetaAPIClient().create_brand_page("USER_TOKEN", "Acme")
    assert res["success"] is False and res["mode"] == "permission_denied"
    assert "not approved" in res["error"] and "/auth/facebook/login" in res["error"]


def test_create_brand_page_expired_token(graph_stub):
    graph_stub.add("POST", "/me/accounts", status=400, json={"error": {"message": "Error validating access token", "code": 190}})
    assert MetaAPIClient().create_brand_page("USER_TOKEN", "Acme")["mode"] == "token_expired"


@pytest.mark.parametrize(
    "token,name,category",
    [("", "Acme", "E_COMMERCE_WEBSITE"), ("t", "  ", "E_COMMERCE_WEBSITE"), ("t", "x" * 76, "E_COMMERCE_WEBSITE"), ("t", "Acme", "bad category!")],
)
def test_create_brand_page_validates_input_without_network(graph_stub, token, name, category):
    res = MetaAPIClient().create_brand_page(token, name, category)
    assert res["success"] is False and res["mode"] == "invalid_input"
    assert graph_stub.calls == []


def test_create_brand_page_network_error(monkeypatch):
    def boom(request):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr("tools.meta_api.get_http_client", lambda: httpx.Client(transport=httpx.MockTransport(boom)))
    res = MetaAPIClient().create_brand_page("t", "Acme")
    assert res["success"] is False and res["mode"] == "network_exception"


# ------------------------------------------------------------------- OAuth
def test_oauth_dialog_url_is_encoded():
    url = MetaAPIClient().build_oauth_dialog_url("app-1", "https://x.com/cb?a=b&c=d", "st ate/+=", ["pages_show_list", "pages_manage_posts"])
    parts = urlsplit(url)
    assert parts.netloc == "www.facebook.com" and parts.path == "/v23.0/dialog/oauth"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {
        "client_id": "app-1", "redirect_uri": "https://x.com/cb?a=b&c=d", "state": "st ate/+=",
        "scope": "pages_show_list,pages_manage_posts", "response_type": "code",
    }


def test_code_exchange_and_long_lived_exchange(graph_stub, oauth_env):
    graph_stub.add("GET", "/oauth/access_token", json={"access_token": "SHORT", "token_type": "bearer", "expires_in": 3600})
    client = MetaAPIClient()
    assert client.exchange_code_for_user_token("the-code", "https://x.com/cb")["access_token"] == "SHORT"
    q = _query(graph_stub.calls[0])
    assert q["code"] == "the-code" and q["client_id"] == "app-123" and q["client_secret"] == "shhh-secret"
    assert q["redirect_uri"] == "https://x.com/cb"

    graph_stub.routes.clear()
    graph_stub.add("GET", "/oauth/access_token", json={"access_token": "LONG", "expires_in": 5184000})
    assert client.exchange_for_long_lived_user_token("SHORT")["access_token"] == "LONG"
    q = _query(graph_stub.calls[1])
    assert q["grant_type"] == "fb_exchange_token" and q["fb_exchange_token"] == "SHORT"


def test_code_exchange_errors(graph_stub, oauth_env, monkeypatch):
    graph_stub.add("GET", "/oauth/access_token", status=400, json={"error": {"message": "Invalid verification code format.", "code": 100}})
    with pytest.raises(MetaOAuthError, match="Invalid verification code") as info:
        MetaAPIClient().exchange_code_for_user_token("bad", "https://x.com/cb")
    assert info.value.status_code == 400
    monkeypatch.setenv("META_APP_SECRET", "")
    with pytest.raises(MetaOAuthError, match="must be configured"):
        MetaAPIClient().exchange_code_for_user_token("c", "https://x.com/cb")


def test_list_managed_pages_follows_paging(graph_stub):
    def me_accounts(request):
        if "after=CURSOR" in str(request.url):
            return httpx.Response(200, json={"data": [{"id": "2", "name": "B", "access_token": "tB"}]})
        return httpx.Response(200, json={
            "data": [{"id": "1", "name": "A", "access_token": "tA"}],
            "paging": {"next": "https://graph.facebook.com/v23.0/me/accounts?after=CURSOR&access_token=U"},
        })

    graph_stub.add("GET", "/me/accounts", handler=me_accounts)
    pages = MetaAPIClient().list_managed_pages("U")
    assert [p["id"] for p in pages] == ["1", "2"]
    assert len(graph_stub.calls) == 2


def test_list_managed_pages_never_follows_foreign_paging_hosts(graph_stub):
    graph_stub.add("GET", "/me/accounts", json={
        "data": [{"id": "1", "access_token": "t"}],
        "paging": {"next": "https://evil.example.com/steal?access_token=U"},
    })
    assert len(MetaAPIClient().list_managed_pages("U")) == 1
    assert len(graph_stub.calls) == 1


def test_verify_and_resolve_page_from_user_token(graph_stub):
    graph_stub.add("GET", f"/{PAGE}?", status=400, json={"error": {"message": "Unsupported get request", "code": 100}})
    graph_stub.add("GET", "/me?", json={"id": "9", "name": "Jane"})
    graph_stub.add("GET", "/me/accounts", json={"data": [{"id": PAGE, "name": "Shop", "access_token": "PAGE_T"}]})
    res = MetaAPIClient().verify_and_resolve_page("USER_T", PAGE)
    assert res == {"token": "PAGE_T", "page_id": PAGE, "page_name": "Shop"}


# ------------------------------------------------------------- proxy routing
def test_meta_traffic_goes_through_configured_proxy(monkeypatch, creds):
    """With OUTBOUND_PROXY_URL set, the shared client MetaAPIClient uses is built with that proxy."""
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "http://127.0.0.1:7890")
    client = get_http_client()
    assert [t for t in client._mounts.values() if t is not None], "expected proxy transports in proxy mode"
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "")
    assert not [t for t in get_http_client()._mounts.values() if t is not None]
