"""Per-workspace notification webhook (Feishu / Lark custom bot, or any HTTPS endpoint).

``POST /integrations/webhook`` sends a real test message and saves the URL only if it is
accepted. The URL is stored like other channel credentials (``platform = "webhook"``); clients
only ever see a masked form. :func:`notify_workspace` delivers events (e.g. new agency
applications) to the workspace's webhook and never raises.

Server-side request forgery: merchants choose the URL, so every send re-validates it and refuses
hosts that resolve to private, loopback, link-local or otherwise non-public addresses. Production
also requires ``https``. Redirects are not followed.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import env_float, is_production
from app.redaction import describe_exception, redact
from app.tenancy import TenantContext, get_tenant_context
from models.schemas import WebhookSaveRequest
from tools.http_client import build_async_httpx_client

logger = logging.getLogger("omniflow.notifications")

router = APIRouter(tags=["integrations"])

WEBHOOK_PLATFORM = "webhook"
FEISHU_HOSTS = ("open.feishu.cn", "open.larksuite.com")
PROVIDER_LABELS = {"feishu": "Feishu / Lark", "custom": "Custom webhook"}


class WebhookURLError(ValueError):
    """The URL is not an acceptable public webhook endpoint."""


def resolve_host(host: str) -> List[str]:
    """IP addresses for ``host`` (separate function so tests can stub DNS)."""
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def provider_for(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return "feishu" if any(host == h or host.endswith("." + h) for h in FEISHU_HOSTS) else "custom"


def mask_webhook(url: str) -> str:
    """``host/…last4`` — enough to recognise the hook without revealing its secret path."""
    parts = urlsplit(url)
    tail = (parts.path + ("?" + parts.query if parts.query else "")).rstrip("/")
    return f"{parts.hostname}/…{tail[-4:]}" if tail else str(parts.hostname)


def validate_public_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    allowed = ("https",) if is_production() else ("https", "http")
    if parts.scheme not in allowed:
        raise WebhookURLError("Webhook URL must start with https://")
    if not parts.hostname:
        raise WebhookURLError("Webhook URL needs a host name")
    if parts.username or parts.password:
        raise WebhookURLError("Webhook URL must not contain a username or password")
    try:
        addresses = resolve_host(parts.hostname)
    except (socket.gaierror, UnicodeError) as exc:
        raise WebhookURLError("Webhook host could not be resolved") from exc
    if not addresses:
        raise WebhookURLError("Webhook host could not be resolved")
    for address in addresses:
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            raise WebhookURLError("Webhook host must be a public internet address")
    return url


def build_message(url: str, event: str, title: str, lines: List[str], data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if provider_for(url) == "feishu":
        return {"msg_type": "text", "content": {"text": "\n".join([title, *lines])}}
    return {"event": event, "title": title, "lines": lines, "data": data or {}}


async def send_webhook(
    url: str, event: str, title: str, lines: List[str], data: Optional[Dict[str, Any]] = None
) -> Tuple[bool, Optional[str]]:
    """POST one message. Returns ``(ok, error)``; never raises."""
    try:
        url = validate_public_url(url)
    except WebhookURLError as exc:
        return False, str(exc)
    try:
        async with build_async_httpx_client(timeout=env_float("WEBHOOK_TIMEOUT_SECONDS", 10.0)) as client:
            resp = await client.post(url, json=build_message(url, event, title, lines, data), follow_redirects=False)
    except Exception as exc:  # noqa: BLE001 - delivery is best effort
        logger.warning("Webhook delivery failed: %s", describe_exception(exc))
        return False, "the webhook endpoint could not be reached"
    if not 200 <= resp.status_code < 300:
        return False, f"the endpoint answered HTTP {resp.status_code}"
    if provider_for(url) == "feishu":
        try:
            body = resp.json()
        except ValueError:
            body = {}
        code = body.get("code", body.get("StatusCode", 0)) if isinstance(body, dict) else 0
        if code not in (0, None):
            return False, redact(f"Feishu rejected the message: {body.get('msg') or body.get('StatusMessage') or code}")
    return True, None


async def notify_workspace(
    store: Any, tenant_id: str, event: str, title: str, lines: List[str], data: Optional[Dict[str, Any]] = None
) -> None:
    """Deliver an event to the workspace's webhook, if one is configured. Never raises."""
    try:
        account = store.get_connected_account(None, WEBHOOK_PLATFORM, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load workspace webhook: %s", describe_exception(exc))
        return
    if not account:
        return
    ok, error = await send_webhook(account["access_token"], event, title, lines, data)
    if not ok:
        logger.warning("Workspace webhook for tenant=%s not delivered: %s", tenant_id, error)


@router.post("/integrations/webhook")
async def save_webhook(
    payload: WebhookSaveRequest, request: Request, ctx: TenantContext = Depends(get_tenant_context)
) -> Dict[str, str]:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Storage is not initialised")
    try:
        url = validate_public_url(payload.url)
    except WebhookURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ok, error = await send_webhook(
        url,
        "omniflow.test",
        "OmniFlow ✅ 测试消息 / Test message",
        ["此 Webhook 已连接到 OmniFlow 工作区，之后的通知会发送到这里。"],
    )
    if not ok:
        raise HTTPException(status_code=400, detail=f"Test message failed: {error}")

    provider = provider_for(url)
    masked = mask_webhook(url)
    store.save_connected_account(
        user_id=ctx.user_id or "global",
        platform=WEBHOOK_PLATFORM,
        account_id=masked,
        account_name=PROVIDER_LABELS[provider],
        access_token=url,
        status="connected",
        permissions=["notifications"],
        tenant_id=ctx.tenant_id,
    )
    # One webhook per workspace: drop any previously saved one.
    for account in store.list_connected_accounts(None, tenant_id=ctx.tenant_id):
        if account["platform"] == WEBHOOK_PLATFORM and account["account_id"] != masked:
            store.delete_connected_account(account["id"], tenant_id=ctx.tenant_id)
    return {"status": "connected", "provider": provider, "masked_url": masked}
