"""Facebook / Meta OAuth 2.0 handshake.

``GET /auth/facebook/login``     -> redirect to the Facebook Login dialog (signed ``state``).
``GET /auth/facebook/callback``  -> exchange ``code`` -> short-lived -> long-lived user token,
                                    list the user's Pages and store each Page Access Token
                                    (non-expiring when derived from a long-lived user token)
                                    for the tenant that started the flow.

Browsers cannot attach an ``Authorization`` header to a navigation, so an SPA should call
``/auth/facebook/login?format=json`` with its bearer token and then navigate to the returned
``authorization_url``; the tenant is carried inside the signed ``state``.

Required env: ``META_APP_ID``, ``META_APP_SECRET``, ``META_REDIRECT_URI`` (must equal the
redirect URI registered in the Meta app, e.g. ``https://app.example.com/auth/facebook/callback``).
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import env_str
from app.security import InvalidTokenError, sign_payload, verify_payload
from app.tenancy import TenantContext, get_tenant_context
from tools.meta_api import DEFAULT_OAUTH_SCOPES, MetaAPIClient, MetaOAuthError

logger = logging.getLogger("omniflow.oauth.meta")

router = APIRouter(tags=["auth"])

STATE_TTL_SECONDS = 600
STATE_TYPE = "meta_oauth_state"


def _safe_next(value: Optional[str]) -> Optional[str]:
    """Only same-site relative paths are allowed (prevents open redirects)."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return None


def _config() -> Dict[str, str]:
    app_id, redirect_uri = env_str("META_APP_ID"), env_str("META_REDIRECT_URI")
    if not app_id or not redirect_uri or not env_str("META_APP_SECRET"):
        raise HTTPException(
            status_code=503,
            detail="Facebook OAuth is not configured. Set META_APP_ID, META_APP_SECRET and META_REDIRECT_URI.",
        )
    return {"app_id": app_id, "redirect_uri": redirect_uri}


def _client() -> MetaAPIClient:
    return MetaAPIClient()


@router.get("/auth/facebook/login")
def facebook_login(
    next: Optional[str] = Query(default=None, description="Relative path to return to after connecting"),
    format: Optional[str] = Query(default=None, description="'json' returns the URL instead of redirecting"),
    ctx: TenantContext = Depends(get_tenant_context),
):
    cfg = _config()
    state = sign_payload(
        {
            "typ": STATE_TYPE,
            "tid": ctx.tenant_id,
            "uid": ctx.user_id,
            "nonce": secrets.token_urlsafe(12),
            "next": _safe_next(next),
        },
        STATE_TTL_SECONDS,
    )
    url = _client().build_oauth_dialog_url(
        cfg["app_id"], cfg["redirect_uri"], state, env_str("META_OAUTH_SCOPES", default=DEFAULT_OAUTH_SCOPES)
    )
    if (format or "").lower() == "json":
        return {"authorization_url": url, "state": state, "expires_in": STATE_TTL_SECONDS}
    return RedirectResponse(url, status_code=307)


def _post_login_redirect(next_path: Optional[str], connected: int, error: Optional[str] = None) -> RedirectResponse:
    base = env_str("FRONTEND_URL", default="/").rstrip("/") or ""
    target = _safe_next(next_path) or ""
    query = {"meta_connected": str(connected)}
    if error:
        query["meta_error"] = error
    location = f"{base}{target}/?{urlencode(query)}" if not target else f"{base}{target}?{urlencode(query)}"
    return RedirectResponse(location, status_code=303)


@router.get("/auth/facebook/callback")
@router.get("/auth/callback/meta", include_in_schema=False)  # legacy redirect URI
def facebook_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_reason: Optional[str] = None,
    error_description: Optional[str] = None,
    format: Optional[str] = None,
):
    wants_json = (format or "").lower() == "json"

    if not state:
        raise HTTPException(status_code=400, detail="Missing OAuth state")
    try:
        claims = verify_payload(state)
        if claims.get("typ") != STATE_TYPE:
            raise InvalidTokenError("wrong state type")
    except InvalidTokenError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid or expired OAuth state: {exc}") from exc

    tenant_id = str(claims.get("tid") or "default")
    user_id = str(claims.get("uid") or "global")
    next_path = claims.get("next")

    if error:  # user denied the dialog, or Meta reported a problem
        logger.info("Facebook OAuth declined for tenant=%s: %s", tenant_id, error_reason or error)
        detail = error_description or error_reason or error
        if wants_json:
            return JSONResponse(status_code=400, content={"status": "denied", "error": detail})
        return _post_login_redirect(next_path, 0, error="access_denied")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    cfg = _config()
    client = _client()
    try:
        short = client.exchange_code_for_user_token(code, cfg["redirect_uri"])
        try:
            long_lived = client.exchange_for_long_lived_user_token(short["access_token"])
            user_token = long_lived["access_token"]
        except MetaOAuthError as exc:  # keep going with the short-lived token; page tokens are still issued
            logger.warning("Long-lived token exchange failed (%s); using short-lived token", exc)
            user_token = short["access_token"]
        pages: List[Dict[str, Any]] = client.list_managed_pages(user_token)
    except MetaOAuthError as exc:
        logger.warning("Facebook OAuth exchange failed for tenant=%s: %s", tenant_id, exc)
        raise HTTPException(status_code=502, detail=f"Facebook rejected the authorization: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - transport failure
        logger.warning("Facebook OAuth transport error: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Could not reach Facebook. Please try again.") from exc

    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Storage is not initialised")

    saved: List[Dict[str, str]] = []
    usable = [p for p in pages if p.get("id") and p.get("access_token")]
    # Save in reverse so the first page Meta lists ends up as the most recently updated (the default).
    for page in reversed(usable):
        store.save_connected_account(
            user_id=user_id,
            platform="meta",
            account_id=str(page["id"]),
            account_name=str(page.get("name") or page["id"]),
            access_token=page["access_token"],
            status="connected",
            permissions=list(page.get("tasks") or ["CREATE_CONTENT"]),
            tenant_id=tenant_id,
        )
    for page in usable:
        saved.append({"id": str(page["id"]), "name": str(page.get("name") or page["id"])})

    hook = getattr(request.app.state, "on_meta_connected", None)
    if hook and usable:
        try:
            hook(tenant_id, usable[0])
        except Exception:  # noqa: BLE001 - a hook failure must not undo a successful connect
            logger.exception("on_meta_connected hook failed")

    logger.info("Facebook OAuth connected %d page(s) for tenant=%s", len(saved), tenant_id)
    if wants_json:
        return {"status": "connected", "pages": saved, "tenant_id": tenant_id}
    return _post_login_redirect(next_path, len(saved))
