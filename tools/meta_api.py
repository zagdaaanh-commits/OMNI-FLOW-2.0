from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

import httpx

from app.config import env_float, env_str, load_environment
from app.redaction import describe_exception, redact
from tools.http_client import get_http_client

logger = logging.getLogger("multi-agent-marketing.meta")

GRAPH_HOST = "graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v23.0"
# pages_manage_engagement: reply to comments as the Page.
# pages_manage_metadata: subscribe the Page to this app's webhooks (POST /{page_id}/subscribed_apps).
DEFAULT_OAUTH_SCOPES = (
    "pages_show_list,pages_manage_posts,pages_read_engagement,pages_manage_engagement,"
    "pages_manage_metadata,business_management"
)
# Page webhook fields subscribed when a Page is connected (comments arrive as "feed" changes).
PAGE_WEBHOOK_FIELDS = "feed"

# Graph error codes that mean "this token cannot be used" (expired / invalidated / malformed).
TOKEN_ERROR_CODES = {102, 190, 463, 467}
# Codes worth one retry after re-resolving a page token from a user token (190 family + missing permission).
TOKEN_RETRY_CODES = TOKEN_ERROR_CODES | {200}
_TOKEN_MESSAGE_HINTS = (
    "expired",
    "validating access token",
    "malformed",
    "invalid oauth",
    "session has been invalidated",
    "error validating",
)
_PERMISSION_CODES = {10, 200, 283, 299, 368}


def _clean_token(raw: str) -> str:
    return "" if (raw and "mock" in raw.lower()) else raw


def parse_graph_error(data: Any, status_code: int, text: str = "") -> Dict[str, Any]:
    """Normalise a Graph API error payload (never includes tokens)."""
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        err = {}
    # Graph messages can embed the app id, user ids or request URLs with tokens.
    message = redact(err.get("message") or text or f"HTTP {status_code}")
    code = err.get("code")
    lowered = message.lower()
    is_token = code in TOKEN_ERROR_CODES or any(h in lowered for h in _TOKEN_MESSAGE_HINTS)
    return {
        "message": message,
        "code": code,
        "subcode": err.get("error_subcode"),
        "type": err.get("type"),
        "fbtrace_id": err.get("fbtrace_id"),
        "is_token_error": bool(is_token),
        "is_permission_error": code in _PERMISSION_CODES or "permission" in lowered,
    }


def _json(resp: httpx.Response) -> Dict[str, Any]:
    if not resp.content:
        return {}
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _detect_image(image_bytes: bytes) -> tuple[str, str]:
    if image_bytes.startswith(b"\x89PNG"):
        return "upload.png", "image/png"
    if image_bytes.startswith(b"GIF8"):
        return "upload.gif", "image/gif"
    if image_bytes.startswith(b"RIFF") and len(image_bytes) > 12 and b"WEBP" in image_bytes[:16]:
        return "upload.webp", "image/webp"
    return "upload.jpg", "image/jpeg"


class MetaAPIClient:
    """Graph API adapter for Meta/Facebook/Instagram.

    * Credentials are read dynamically from the environment (``.env`` is loaded once by
      :func:`app.config.load_environment`; real environment variables win).
    * All traffic goes through :func:`tools.http_client.get_http_client`, so
      ``OUTBOUND_PROXY_URL`` switches between direct (Hong Kong cloud) and proxy
      (mainland development) routing without code changes.
    """

    _cached_page_tokens: Dict[str, str] = {}

    def __init__(self) -> None:
        self._refresh_credentials()

    # ------------------------------------------------------------ credentials
    def _refresh_credentials(self) -> None:
        load_environment()
        self.page_id = env_str("FACEBOOK_PAGE_ID", "META_PAGE_ID")
        self.access_token = _clean_token(env_str("FACEBOOK_PAGE_ACCESS_TOKEN", "META_ACCESS_TOKEN"))
        self.user_token = _clean_token(env_str("FACEBOOK_USER_ACCESS_TOKEN"))
        self.graph_version = env_str("META_GRAPH_VERSION", default=DEFAULT_GRAPH_VERSION)
        self.base_url = f"https://{GRAPH_HOST}/{self.graph_version}"
        self.timeout = env_float("HTTP_TIMEOUT_SECONDS", 10.0)

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def has_facebook_credentials(self) -> bool:
        self._refresh_credentials()
        return bool(self.page_id and (self.access_token or self.user_token))

    # ---------------------------------------------------------------- HTTP core
    def _get(self, url: str, *, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> httpx.Response:
        """GET with one retry on transport errors (idempotent)."""
        client = get_http_client()
        last: Optional[Exception] = None
        for _ in range(2):
            try:
                return client.get(url, params=params, timeout=timeout or self.timeout)
            except httpx.TransportError as exc:
                last = exc
                logger.debug("Graph GET transport error, retrying: %s", type(exc).__name__)
        assert last is not None
        raise last

    def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        """POST without automatic retry (publishing is not idempotent)."""
        kwargs.setdefault("timeout", self.timeout)
        return get_http_client().post(url, **kwargs)

    # ------------------------------------------------------------ page tokens
    def resolve_page_token(self, page_id: Optional[str] = None) -> Optional[str]:
        """Resolve a Page Access Token: cache, configured page token, or /me/accounts via a user token."""
        self._refresh_credentials()
        target_page = str(page_id or self.page_id)
        if target_page in self._cached_page_tokens:
            return self._cached_page_tokens[target_page]

        if self.access_token:
            self._cached_page_tokens[target_page] = self.access_token
            return self.access_token

        if not self.user_token:
            return None

        try:
            resp = self._get(self._url("me/accounts"), params={"access_token": self.user_token})
            if resp.status_code == 200:
                data = _json(resp).get("data", [])
                for acc in data:
                    acc_id = str(acc.get("id"))
                    acc_token = acc.get("access_token")
                    if acc_token:
                        self._cached_page_tokens[acc_id] = acc_token
                    if acc_id == target_page and acc_token:
                        return acc_token
                if data and data[0].get("access_token") and not target_page:
                    self._cached_page_tokens[str(data[0]["id"])] = data[0]["access_token"]
                    return data[0]["access_token"]
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to query /me/accounts: %s", type(exc).__name__)

        return self.access_token

    # -------------------------------------------------------- connection test
    def test_connection(self) -> Dict[str, Any]:
        """Tests Facebook Graph API credentials and returns live page connection details."""
        self._refresh_credentials()
        if not self.has_facebook_credentials():
            return {
                "connected": False,
                "error": "FACEBOOK_PAGE_ID or FACEBOOK_PAGE_ACCESS_TOKEN not configured in .env",
                "page_id": self.page_id,
            }

        resolved_token = self.resolve_page_token(self.page_id) or self.access_token
        if not resolved_token:
            return {
                "connected": False,
                "error": "Facebook Page Access Token not configured. Please paste your token.",
                "page_id": self.page_id,
                "token_expired": True,
            }

        try:
            resp = self._get(
                self._url(str(self.page_id)),
                params={"fields": "id,name,link", "access_token": resolved_token},
            )
            data = _json(resp)
            if resp.status_code == 200 and "id" in data:
                return {
                    "connected": True,
                    "page_id": data.get("id"),
                    "page_name": data.get("name"),
                    "page_link": data.get("link", f"https://www.facebook.com/{data.get('id')}"),
                    "status_code": 200,
                    "message": "Facebook Page verified & active 🟢",
                }
            err = parse_graph_error(data, resp.status_code, resp.text)
            is_expired = err["is_token_error"] or "app id" in err["message"].lower()
            return {
                "connected": False,
                "error": "Facebook authorization expired. Please reconnect the Page in Integrations." if is_expired else err["message"],
                "status_code": resp.status_code,
                "page_id": self.page_id,
                "token_expired": is_expired,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "connected": False,
                "error": describe_exception(exc),
                "page_id": self.page_id,
                "token_expired": False,
            }

    # ------------------------------------------------------------- publishing
    @staticmethod
    def _failure(resp: httpx.Response, data: Dict[str, Any]) -> Dict[str, Any]:
        err = parse_graph_error(data, resp.status_code, resp.text)
        logger.warning(
            "Meta Graph API error: status=%s code=%s type=%s trace=%s msg=%s",
            resp.status_code, err["code"], err["type"], err["fbtrace_id"], err["message"],
        )
        expired = err["is_token_error"]
        return {
            "success": False,
            "status_code": resp.status_code,
            "error": (
                "Facebook Access Token expired. Please refresh your Page token."
                if expired
                else f"Meta Graph API ({resp.status_code}): {err['message']}"
            ),
            "graph_error": err,
            "post_url": None,
            "post_id": None,
            "external_post_id": None,
            "raw": data,
            "mode": "token_expired" if expired else "api_error",
            "confirmation_badge": "Token Expired 🟡" if expired else "Gateway Fallback 🟡",
        }

    @staticmethod
    def _network_failure(exc: Exception) -> Dict[str, Any]:
        logger.warning("Meta Graph API connection exception: %s", type(exc).__name__)
        return {
            "success": False,
            "error": "Connection exception: " + describe_exception(exc),
            "post_url": None,
            "post_id": None,
            "external_post_id": None,
            "mode": "network_exception",
            "confirmation_badge": "Gateway Fallback 🟡",
        }

    def _resolve_target(self, page_id: Optional[str], access_token: Optional[str]) -> tuple[str, str]:
        self._refresh_credentials()
        target_page = page_id or self.page_id
        target_token = access_token or self.resolve_page_token(target_page) or self.access_token
        return target_page, target_token

    def publish_facebook_page_feed(
        self,
        message: str,
        page_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST ``/{page_id}/feed`` with ``message``. Success includes ``id``, ``post_id``, ``post_url``."""
        target_page, target_token = self._resolve_target(page_id, access_token)
        if not target_page or not target_token:
            return {
                "success": False,
                "error": "FACEBOOK_PAGE_ID or FACEBOOK_PAGE_ACCESS_TOKEN missing in .env",
                "mode": "missing_credentials",
                "post_url": None,
                "confirmation_badge": "Credentials Missing 🟡",
            }

        url = self._url(f"{target_page}/feed")
        try:
            resp = self._post(url, data={"message": message, "access_token": target_token})
            data = _json(resp)

            if resp.status_code != 200 and (data.get("error") or {}).get("code") in TOKEN_RETRY_CODES and not access_token:
                resolved = self.resolve_page_token(target_page)
                if resolved and resolved != target_token:
                    resp = self._post(url, data={"message": message, "access_token": resolved})
                    data = _json(resp)

            if resp.status_code == 200 and "id" in data:
                fb_id = str(data["id"])
                post_url = f"https://www.facebook.com/{fb_id}"
                return {
                    "success": True,
                    "id": fb_id,
                    "post_id": fb_id,
                    "external_post_id": fb_id,
                    "post_url": post_url,
                    "confirmation_badge": "Published to Facebook 🟢",
                    "status_code": 200,
                    "message": "Published to Facebook 🟢",
                    "raw": data,
                }
            return self._failure(resp, data)
        except Exception as exc:  # noqa: BLE001
            return self._network_failure(exc)

    def publish_facebook_page_photo(
        self,
        image_bytes: bytes,
        caption: str,
        page_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST ``/{page_id}/photos`` (multipart ``source``).

        Graph returns ``{"id": <photo_id>, "post_id": "<page>_<post>"}``.  The result's
        ``id``/``post_id``/``external_post_id`` carry the *post* id and ``post_url`` links to it.
        """
        target_page, target_token = self._resolve_target(page_id, access_token)
        if not target_page or not target_token:
            return {
                "success": False,
                "error": "FACEBOOK_PAGE_ID or FACEBOOK_PAGE_ACCESS_TOKEN missing in .env",
                "mode": "missing_credentials",
                "post_url": None,
                "confirmation_badge": "Credentials Missing 🟡",
            }

        url = self._url(f"{target_page}/photos")
        filename, mime_type = _detect_image(image_bytes)
        upload_timeout = max(self.timeout, 30.0)

        def _upload(token: str) -> httpx.Response:
            return self._post(
                url,
                params={"access_token": token},
                data={"caption": caption},
                files={"source": (filename, image_bytes, mime_type)},
                timeout=upload_timeout,
            )

        try:
            resp = _upload(target_token)
            data = _json(resp)

            if resp.status_code != 200 and (data.get("error") or {}).get("code") in TOKEN_RETRY_CODES and not access_token:
                resolved = self.resolve_page_token(target_page)
                if resolved and resolved != target_token:
                    resp = _upload(resolved)
                    data = _json(resp)

            if resp.status_code == 200 and ("id" in data or "post_id" in data):
                photo_id = data.get("id")
                post_id = str(data.get("post_id") or photo_id)
                post_url = f"https://www.facebook.com/{post_id}"
                logger.info("Published photo to Facebook page %s: %s", target_page, post_url)
                return {
                    "success": True,
                    "id": post_id,
                    "post_id": post_id,
                    "external_post_id": post_id,
                    "photo_id": str(photo_id) if photo_id is not None else None,
                    "post_url": post_url,
                    "confirmation_badge": "Published to Facebook 🟢",
                    "status_code": 200,
                    "message": "Published to Facebook 🟢",
                    "raw": data,
                }
            return self._failure(resp, data)
        except Exception as exc:  # noqa: BLE001
            return self._network_failure(exc)

    # --------------------------------------------------------------- comments
    def reply_to_comment(self, comment_id: str, message: str, access_token: str) -> Dict[str, Any]:
        """POST ``/{comment_id}/comments`` as the Page. Transport errors propagate to the caller.

        Returns ``{"success": True, "id": ...}`` or ``{"success": False, "status_code", "error"}``
        where ``error`` is :func:`parse_graph_error` output.
        """
        self._refresh_credentials()
        resp = self._post(self._url(f"{comment_id}/comments"), data={"message": message, "access_token": access_token})
        data = _json(resp)
        if resp.status_code == 200 and data.get("id"):
            return {"success": True, "id": str(data["id"])}
        return {"success": False, "status_code": resp.status_code, "error": parse_graph_error(data, resp.status_code, resp.text)}

    # ------------------------------------------------------------ ad accounts
    AD_ACCOUNT_FIELDS = "id,account_id,name,account_status,currency,timezone_name"

    def get_ad_account(self, ad_account_id: str, access_token: str) -> Dict[str, Any]:
        """GET ``/act_<id>`` with a (System User) token. Transport errors propagate to the caller.

        Returns ``{"success": True, "account": {...}}`` or ``{"success": False, "status_code", "error"}``.
        """
        self._refresh_credentials()
        resp = self._get(self._url(ad_account_id), params={"fields": self.AD_ACCOUNT_FIELDS, "access_token": access_token})
        data = _json(resp)
        if resp.status_code == 200 and data.get("id"):
            return {"success": True, "account": data}
        return {"success": False, "status_code": resp.status_code, "error": parse_graph_error(data, resp.status_code, resp.text)}

    def subscribe_page_webhooks(self, page_id: str, page_token: str, fields: str = PAGE_WEBHOOK_FIELDS) -> Dict[str, Any]:
        """POST ``/{page_id}/subscribed_apps`` so Meta delivers this Page's webhook events to the app.

        Needs a Page token carrying ``pages_manage_metadata``. Never raises: returns
        ``{"success": True}`` or ``{"success": False, "error": <message>, "code": <graph code>}``.
        """
        self._refresh_credentials()
        try:
            resp = self._post(
                self._url(f"{page_id}/subscribed_apps"),
                data={"subscribed_fields": fields, "access_token": page_token},
            )
        except Exception as exc:  # noqa: BLE001 - transport failure
            return {"success": False, "error": f"network error: {type(exc).__name__}", "code": None}
        data = _json(resp)
        if resp.status_code == 200 and data.get("success") is True:
            return {"success": True}
        err = parse_graph_error(data, resp.status_code, resp.text)
        return {"success": False, "error": err["message"], "code": err["code"]}

    # ------------------------------------------------ brand page provisioning
    def create_brand_page(
        self,
        user_access_token: str,
        page_name: str,
        category: str = "E_COMMERCE_WEBSITE",
        about: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Facebook Page for an onboarded merchant via ``POST /me/accounts``.

        IMPORTANT: Meta restricts programmatic Page creation to apps that have been granted
        the required access (business verification plus advanced access / partner approval).
        For ordinary apps Graph answers with a permissions error; callers MUST handle
        ``success=False`` (this method never raises for API errors) and fall back to guiding
        the merchant to create the Page manually and connect it through OAuth.

        Returns ``{success, page_id, page_name, page_url, access_token, error, status_code, raw, ...}``.
        """
        name = (page_name or "").strip()
        token = (user_access_token or "").strip()
        base_fail: Dict[str, Any] = {
            "success": False, "page_id": None, "page_name": name or None, "page_url": None,
            "access_token": None, "raw": None,
        }
        if not token:
            return {**base_fail, "error": "user_access_token is required", "status_code": 400, "mode": "invalid_input"}
        if not name:
            return {**base_fail, "error": "page_name is required", "status_code": 400, "mode": "invalid_input"}
        if len(name) > 75:
            return {**base_fail, "error": "page_name must be 75 characters or fewer", "status_code": 400, "mode": "invalid_input"}
        category_enum = (category or "E_COMMERCE_WEBSITE").strip().upper()
        if not re.fullmatch(r"[A-Z0-9_]{2,64}", category_enum):
            return {**base_fail, "error": "category must be a Graph category enum such as E_COMMERCE_WEBSITE",
                    "status_code": 400, "mode": "invalid_input"}

        self._refresh_credentials()
        payload: Dict[str, Any] = {"name": name, "category_enum": category_enum, "access_token": token}
        if about:
            payload["about"] = about.strip()[:255]

        try:
            resp = self._post(self._url("me/accounts"), data=payload)
            data = _json(resp)
        except Exception as exc:  # noqa: BLE001
            return {**base_fail, "error": "Connection exception: " + describe_exception(exc),
                    "status_code": None, "mode": "network_exception"}

        if resp.status_code == 200 and data.get("id"):
            page_id = str(data["id"])
            page_token = data.get("access_token")
            if page_token:
                self._cached_page_tokens[page_id] = page_token
            logger.info("Created Facebook brand page %s (%s)", page_id, name)
            return {
                "success": True,
                "page_id": page_id,
                "page_name": name,
                "page_url": f"https://www.facebook.com/{page_id}",
                "access_token": page_token,
                "error": None,
                "status_code": 200,
                "raw": {k: v for k, v in data.items() if k != "access_token"},
            }

        err = parse_graph_error(data, resp.status_code, resp.text)
        if err["is_token_error"]:
            message, mode = "The user access token is invalid or expired. Ask the merchant to re-authorize.", "token_expired"
        elif err["is_permission_error"]:
            message = (
                "Meta rejected Page creation: this app is not approved to create Pages via the API "
                f"(Graph: {err['message']}). Create the Page manually and connect it via /auth/facebook/login."
            )
            mode = "permission_denied"
        else:
            message, mode = f"Meta Graph API ({resp.status_code}): {err['message']}", "api_error"
        return {**base_fail, "error": message, "status_code": resp.status_code, "graph_error": err,
                "mode": mode, "raw": {k: v for k, v in data.items() if k != "access_token"}}

    # ------------------------------------------------------ OAuth 2.0 helpers
    def build_oauth_dialog_url(
        self,
        app_id: str,
        redirect_uri: str,
        state: str,
        scopes: Optional[str | List[str]] = None,
    ) -> str:
        """Facebook Login dialog URL (``https://www.facebook.com/<ver>/dialog/oauth``)."""
        self._refresh_credentials()
        if isinstance(scopes, (list, tuple)):
            scope_str = ",".join(scopes)
        else:
            scope_str = scopes or env_str("META_OAUTH_SCOPES", default=DEFAULT_OAUTH_SCOPES)
        query = urlencode(
            {
                "client_id": app_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": scope_str,
                "response_type": "code",
            }
        )
        return f"https://www.facebook.com/{self.graph_version}/dialog/oauth?{query}"

    def _oauth_call(self, params: Dict[str, str]) -> Dict[str, Any]:
        resp = self._get(self._url("oauth/access_token"), params=params)
        data = _json(resp)
        if resp.status_code != 200 or "access_token" not in data:
            err = parse_graph_error(data, resp.status_code, resp.text)
            raise MetaOAuthError(err["message"], status_code=resp.status_code, graph_error=err)
        return data

    def exchange_code_for_user_token(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        """Authorization ``code`` -> short-lived user token ``{access_token, token_type, expires_in}``."""
        app_id, app_secret = env_str("META_APP_ID"), env_str("META_APP_SECRET")
        if not app_id or not app_secret:
            raise MetaOAuthError("META_APP_ID and META_APP_SECRET must be configured", status_code=503)
        return self._oauth_call(
            {"client_id": app_id, "client_secret": app_secret, "redirect_uri": redirect_uri, "code": code}
        )

    def exchange_for_long_lived_user_token(self, short_lived_token: str) -> Dict[str, Any]:
        """Short-lived -> long-lived (~60 day) user token.

        Page Access Tokens derived from a long-lived user token via ``/me/accounts`` do not expire.
        """
        app_id, app_secret = env_str("META_APP_ID"), env_str("META_APP_SECRET")
        if not app_id or not app_secret:
            raise MetaOAuthError("META_APP_ID and META_APP_SECRET must be configured", status_code=503)
        return self._oauth_call(
            {
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_lived_token,
            }
        )

    def list_managed_pages(self, user_access_token: str, max_pages: int = 10) -> List[Dict[str, Any]]:
        """Pages the user manages, each with its (non-expiring) Page Access Token; follows paging."""
        pages: List[Dict[str, Any]] = []
        url: Optional[str] = self._url("me/accounts")
        params: Optional[Dict[str, Any]] = {
            "fields": "id,name,access_token,category,tasks",
            "limit": 100,
            "access_token": user_access_token,
        }
        for _ in range(max_pages):
            if not url:
                break
            resp = self._get(url, params=params)
            data = _json(resp)
            if resp.status_code != 200:
                err = parse_graph_error(data, resp.status_code, resp.text)
                raise MetaOAuthError(err["message"], status_code=resp.status_code, graph_error=err)
            pages.extend(p for p in data.get("data", []) if isinstance(p, dict))
            next_url = (data.get("paging") or {}).get("next")
            # Only follow paging links that stay on Graph (the URL embeds the access token).
            if next_url and urlsplit(next_url).hostname == GRAPH_HOST:
                url, params = next_url, None
            else:
                url = None
        return pages

    def verify_and_resolve_page(self, token: str, target_page: str) -> Dict[str, Any]:
        """Validate a pasted token and resolve the Page Access Token to store.

        Accepts a Page token (verified against the page) or a User token (verified via ``/me``,
        then the matching Page token is taken from ``/me/accounts``).
        Returns ``{token, page_id, page_name}``; raises :class:`MetaOAuthError` (status 400) if rejected.
        """
        self._refresh_credentials()
        page_name = "Facebook Page"
        resp = self._get(
            self._url(str(target_page)), params={"fields": "id,name,link", "access_token": token}
        )
        data = _json(resp)
        if resp.status_code == 200 and "id" in data:
            return {"token": token, "page_id": str(data.get("id", target_page)), "page_name": data.get("name", page_name)}

        user_resp = self._get(self._url("me"), params={"fields": "id,name", "access_token": token})
        user_data = _json(user_resp)
        if user_resp.status_code != 200:
            err = (
                parse_graph_error(data, resp.status_code, resp.text)["message"]
                if data.get("error")
                else parse_graph_error(user_data, user_resp.status_code, user_resp.text)["message"]
            )
            raise MetaOAuthError(f"Meta Graph API token verification rejected: {err}", status_code=400)

        acc_resp = self._get(self._url("me/accounts"), params={"access_token": token})
        accounts = _json(acc_resp).get("data", []) if acc_resp.status_code == 200 else []
        matched = next((a for a in accounts if str(a.get("id")) == str(target_page)), None)
        if matched and matched.get("access_token"):
            return {"token": matched["access_token"], "page_id": str(matched["id"]), "page_name": matched.get("name", page_name)}
        if accounts and accounts[0].get("access_token"):
            first = accounts[0]
            return {"token": first["access_token"], "page_id": str(first["id"]), "page_name": first.get("name", page_name)}
        return {"token": token, "page_id": str(target_page), "page_name": user_data.get("name", "User Account")}

    # ---------------------------------------------- generic Graph (IG etc.)
    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        self._refresh_credentials()
        params = dict(kwargs.pop("params", {}) or {})
        if "access_token" not in params and self.access_token:
            params["access_token"] = self.access_token
        response = get_http_client().request(
            method, self._url(path), params=params, timeout=self.timeout, **kwargs
        )
        response.raise_for_status()
        return response.json()

    def publish_page_feed(self, page_id: str, message: str, link: Optional[str] = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {"message": message}
        if link:
            data["link"] = link
        return self._request("POST", f"{page_id}/feed", data=data)

    def create_instagram_media_container(self, ig_user_id: str, image_url: str, caption: str) -> Dict[str, Any]:
        return self._request("POST", f"{ig_user_id}/media", data={"image_url": image_url, "caption": caption})

    def publish_instagram_media(self, ig_user_id: str, creation_id: str) -> Dict[str, Any]:
        return self._request("POST", f"{ig_user_id}/media_publish", data={"creation_id": creation_id})

    def get_page_insights(self, page_id: str, metric: str, period: str = "day") -> Dict[str, Any]:
        return self._request("GET", f"{page_id}/insights", params={"metric": metric, "period": period})

    def get_object(self, object_id: str, fields: str) -> Dict[str, Any]:
        return self._request("GET", object_id, params={"fields": fields})


class MetaOAuthError(RuntimeError):
    """OAuth / token-exchange failure with Graph error details attached."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, graph_error: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.graph_error = graph_error or {}
