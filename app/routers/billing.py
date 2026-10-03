"""Plans, the workspace's subscription, and upgrade requests.

* ``GET  /api/billing/plans`` - public price list (Pro Growth, Agency VIP).
* ``GET  /api/billing/subscription`` - the caller's plan, status, limits and current usage.
* ``POST /api/billing/upgrade-request`` - "Upgrade" from the pricing modal. Records the request,
  notifies the operator (``LEAD_NOTIFICATION_WEBHOOK``) and returns the optional payment link
  (``BILLING_CHECKOUT_URL_<PLAN>_<CYCLE>``). It never changes the plan: the operator activates
  it after payment with ``scripts/set_plan.py``.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel

from app import plans
from app.config import env_float
from app.routers.agency import lead_webhook_url
from app.tenancy import TenantContext, get_tenant_context, require_authenticated
from tools.http_client import build_async_httpx_client

logger = logging.getLogger("omniflow.billing")

router = APIRouter(prefix="/api/billing", tags=["billing"])

UPGRADE_EVENT = "upgrade_request.created"
DUPLICATE_WINDOW = timedelta(hours=24)


class UpgradeRequestBody(BaseModel):
    plan: Literal["pro", "agency"]
    billing_cycle: Literal["monthly", "annual"] = "monthly"


def _store(request: Request) -> Any:
    return request.app.state.store


async def notify_operator(url: str, payload: Dict[str, Any]) -> None:
    """POST the upgrade request to the operator's webhook; never raises."""
    try:
        async with build_async_httpx_client(timeout=env_float("LEAD_WEBHOOK_TIMEOUT_SECONDS", 10.0)) as client:
            resp = await client.post(url, json={"event": UPGRADE_EVENT, "upgrade_request": payload})
        if resp.status_code >= 400:
            logger.warning("Upgrade webhook returned HTTP %s for request %s", resp.status_code, payload.get("id"))
    except Exception as exc:  # noqa: BLE001 - notification is best effort
        logger.warning("Upgrade webhook delivery failed for request %s: %s", payload.get("id"), type(exc).__name__)


def _request_view(record: Dict[str, Any]) -> Dict[str, Any]:
    plan = plans.PLANS[record["plan"]]
    return {
        "id": record["id"],
        "plan": record["plan"],
        "plan_name": plan["name"],
        "billing_cycle": record["billing_cycle"],
        "price": plan["prices"][record["billing_cycle"]],
        "currency": plans.CURRENCY,
        "status": record["status"],
        "created_at": str(record.get("created_at")),
    }


@router.get("/plans")
def list_plans() -> Dict[str, Any]:
    return plans.public_catalog()


@router.get("/subscription")
def get_subscription(request: Request, ctx: TenantContext = Depends(get_tenant_context)) -> Dict[str, Any]:
    store = _store(request)
    summary = plans.subscription_summary(store, ctx.tenant_id)
    pending = store.list_upgrade_requests(tenant_id=ctx.tenant_id, status="pending")
    summary["pending_request"] = _request_view(pending[0]) if pending else None
    return summary


@router.post("/upgrade-request")
def request_upgrade(
    body: UpgradeRequestBody,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: TenantContext = Depends(require_authenticated),
) -> Dict[str, Any]:
    store = _store(request)
    checkout = plans.checkout_url(body.plan, body.billing_cycle)

    # Clicking again within a day returns the same request instead of notifying the operator twice.
    now = plans.utcnow()
    for existing in store.list_upgrade_requests(tenant_id=ctx.tenant_id, status="pending"):
        created = plans.parse_time(existing.get("created_at"))
        same = existing["plan"] == body.plan and existing["billing_cycle"] == body.billing_cycle
        if same and created and now - created < DUPLICATE_WINDOW:
            return {**_request_view(existing), "status": "received", "duplicate": True, "checkout_url": checkout}

    record = store.save_upgrade_request(
        {"user_id": ctx.user_id, "plan": body.plan, "billing_cycle": body.billing_cycle}, tenant_id=ctx.tenant_id
    )
    view = _request_view(record)
    logger.info("Upgrade request %s: tenant=%s -> %s/%s", record["id"], ctx.tenant_id, body.plan, body.billing_cycle)

    url = lead_webhook_url()
    if url:
        user: Optional[Dict[str, Any]] = store.get_user_by_id(ctx.user_id, tenant_id=ctx.tenant_id) if ctx.user_id else None
        current = plans.subscription_summary(store, ctx.tenant_id)
        background_tasks.add_task(notify_operator, url, {
            **view,
            "tenant_id": ctx.tenant_id,
            "email": (user or {}).get("email"),
            "name": (user or {}).get("full_name"),
            "current_plan": current["plan"],
            "current_status": current["status"],
        })
    return {**view, "status": "received", "duplicate": False, "checkout_url": checkout}
