"""Facebook Page comments: Meta webhook (verify + ingest) and replying as the Page.

``GET  /api/meta/webhook``       -> subscription handshake (``hub.mode`` / ``hub.verify_token`` / ``hub.challenge``)
``POST /api/meta/webhook``       -> store ``feed`` changes whose ``item`` is ``comment`` in ``page_comments``
``POST /api/meta/reply-comment`` -> reply to a comment through the Graph API with a Page Access Token

Security
* The verify token is ``META_VERIFY_TOKEN``. Outside production it falls back to
  ``omniflow_verify_token`` for local testing; production refuses the handshake without it.
* Events are authenticated with ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body keyed with
  ``META_APP_SECRET``). With a secret configured, unsigned or mismatched events get 403. Without
  one, production rejects every event and development accepts them with a warning.
* An event is stored for every workspace that connected the Page (looked up by Page id); the
  default workspace also owns ``FACEBOOK_PAGE_ID``. Events for unknown Pages are dropped.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from app.config import env_str, is_production
from app.tenancy import DEFAULT_TENANT_ID, TenantContext, get_tenant_context
from models.schemas import CommentReplyRequest
from tools.meta_api import MetaAPIClient

logger = logging.getLogger("omniflow.meta.comments")

router = APIRouter(prefix="/api/meta", tags=["meta"])

DEV_VERIFY_TOKEN = "omniflow_verify_token"


def _store(request: Request) -> Any:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Storage is not initialised")
    return store


# --------------------------------------------------------------- verification
def expected_verify_token() -> str:
    token = env_str("META_VERIFY_TOKEN")
    if token:
        return token
    return "" if is_production() else DEV_VERIFY_TOKEN


@router.get("/webhook", response_class=PlainTextResponse)
def verify_webhook(
    mode: Optional[str] = Query(default=None, alias="hub.mode"),
    challenge: Optional[str] = Query(default=None, alias="hub.challenge"),
    verify_token: Optional[str] = Query(default=None, alias="hub.verify_token"),
) -> PlainTextResponse:
    expected = expected_verify_token()
    if (
        mode == "subscribe"
        and challenge is not None
        and expected
        and verify_token is not None
        and hmac.compare_digest(verify_token.encode(), expected.encode())
    ):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Webhook verification failed")


# ----------------------------------------------------------------- ingestion
def check_signature(body: bytes, header: Optional[str]) -> None:
    secret = env_str("META_APP_SECRET")
    if not secret:
        if is_production():
            raise HTTPException(status_code=403, detail="Webhook signature cannot be verified: META_APP_SECRET is not set")
        logger.warning("Accepting an unsigned Meta webhook event (META_APP_SECRET not set; development only)")
        return
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not header or not hmac.compare_digest(header.strip().encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")


def _text(value: Any) -> Optional[str]:
    return str(value) if value not in (None, "") else None


def _created_time(value: Any) -> Optional[datetime]:
    """Meta sends unix seconds for feed changes; accept ISO-8601 strings too."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str) and value:
        if value.isdigit():
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("+0000", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def iter_comment_events(payload: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield ``(page_id, comment)`` for every ``entry[].changes[].value`` with ``item == "comment"``."""
    if payload.get("object") not in (None, "page"):
        return
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        page_id = _text(entry.get("id"))
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") not in (None, "feed"):
                continue
            value = change.get("value")
            if not isinstance(value, dict) or value.get("item") != "comment":
                continue
            comment_id = _text(value.get("comment_id"))
            if not page_id or not comment_id:
                continue
            sender = value.get("from") if isinstance(value.get("from"), dict) else {}
            message = value.get("message")
            yield page_id, {
                "page_id": page_id,
                "comment_id": comment_id,
                "post_id": _text(value.get("post_id")),
                "parent_id": _text(value.get("parent_id")),
                "from_id": _text(sender.get("id")),
                "from_name": _text(sender.get("name")),
                "message": message if isinstance(message, str) else None,
                "verb": str(value.get("verb") or "add")[:20],
                "created_time": _created_time(value.get("created_time")),
            }


def tenants_for_page(store: Any, page_id: str) -> List[str]:
    tenants = {
        acc["tenant_id"]
        for acc in store.list_connected_accounts_any_tenant("meta", page_id)
        if acc.get("status", "connected") == "connected"
    }
    if page_id == env_str("FACEBOOK_PAGE_ID", "META_PAGE_ID"):
        tenants.add(DEFAULT_TENANT_ID)
    return sorted(tenants)


def _persist(store: Any, events: List[Tuple[str, Dict[str, Any]]]) -> int:
    stored = 0
    for page_id, comment in events:
        tenants = tenants_for_page(store, page_id)
        if not tenants:
            logger.info("Dropping comment %s for unconnected page %s", comment["comment_id"], page_id)
            continue
        for tenant_id in tenants:
            store.save_page_comment(comment, tenant_id=tenant_id)
            stored += 1
    return stored


@router.post("/webhook")
async def receive_webhook(request: Request) -> Dict[str, str]:
    body = await request.body()
    check_signature(body, request.headers.get("x-hub-signature-256"))
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Webhook body must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook body must be a JSON object")

    events = list(iter_comment_events(payload))
    if events:
        stored = await run_in_threadpool(_persist, _store(request), events)
        logger.info("Meta webhook: %d comment event(s), %d row(s) stored", len(events), stored)
    return {"status": "EVENT_RECEIVED"}


# -------------------------------------------------------------------- replies
@router.post("/reply-comment")
def reply_to_comment(
    payload: CommentReplyRequest,
    request: Request,
    ctx: TenantContext = Depends(get_tenant_context),
) -> Dict[str, str]:
    token = payload.page_token
    if not token:
        # The page that received the comment decides which Page token replies; fall back to the
        # workspace's connected page when the comment has not been seen through the webhook.
        stored = _store(request).get_page_comment(payload.comment_id, tenant_id=ctx.tenant_id)
        resolve = getattr(request.app.state, "facebook_credentials", None)
        if resolve is not None:
            _, token = resolve(ctx.tenant_id, stored["page_id"] if stored else None)
    if not token:
        raise HTTPException(
            status_code=400,
            detail="No Facebook Page is connected for this workspace. Connect a Page or pass page_token.",
        )

    try:
        result = MetaAPIClient().reply_to_comment(payload.comment_id, payload.message, token)
    except Exception as exc:  # noqa: BLE001 - transport failure
        logger.warning("Comment reply transport error: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Could not reach Facebook. Please try again.") from exc

    if not result["success"]:
        error = result["error"]
        status = result["status_code"]
        logger.info("Graph rejected comment reply (HTTP %s, code %s)", status, error.get("code"))
        raise HTTPException(
            status_code=400 if 400 <= status < 500 else 502,
            detail=f"Facebook rejected the reply: {error['message']}",
        )
    return {"id": result["id"]}
