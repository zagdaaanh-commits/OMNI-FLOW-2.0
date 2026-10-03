"""Plans, the workspace's subscription, Stripe Checkout and upgrade requests.

* ``GET  /api/billing/plans`` - public price list (Pro Growth, Agency VIP) and whether online
  payment is available (``payments``).
* ``GET  /api/billing/subscription`` - the caller's plan, status, limits and current usage.
* ``POST /api/billing/create-checkout-session`` - Stripe Checkout for one prepaid period of a
  plan (card, Alipay, WeChat Pay); returns the Checkout ``url`` to redirect to. The workspace is
  carried in ``client_reference_id`` and ``metadata.workspace_id``.
* ``GET  /api/billing/checkout-session/{id}`` - the success page's check: asks Stripe for the
  session and applies it if paid (idempotent with the webhook, ``app/routers/webhook.py``).
* ``POST /api/billing/upgrade-request`` - the manual path when Stripe is not configured. Records
  the request, notifies the operator (``LEAD_NOTIFICATION_WEBHOOK``) and returns the optional
  payment link (``BILLING_CHECKOUT_URL_<PLAN>_<CYCLE>``). It never changes the plan: the
  operator activates it after payment with ``scripts/set_plan.py``.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import AliasChoices, BaseModel, Field

from app import payments, plans
from app.config import env_float, env_str
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


class CheckoutSessionBody(BaseModel):
    plan: Literal["pro", "agency"]
    cycle: Literal["monthly", "annual"] = Field(
        default="monthly", validation_alias=AliasChoices("cycle", "billing_cycle")
    )
    locale: Optional[Literal["zh", "en"]] = None  # language of the Stripe page; default: the browser's


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


def _public_base_url(request: Request) -> str:
    """Where Stripe sends the merchant back: PUBLIC_BASE_URL, else this request's own origin."""
    configured = env_str("PUBLIC_BASE_URL")
    if configured.lower().startswith(("https://", "http://")):
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _payment_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


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
    return {**plans.public_catalog(), "payments": payments.public_info()}


@router.get("/subscription")
def get_subscription(request: Request, ctx: TenantContext = Depends(get_tenant_context)) -> Dict[str, Any]:
    store = _store(request)
    summary = plans.subscription_summary(store, ctx.tenant_id)
    pending = store.list_upgrade_requests(tenant_id=ctx.tenant_id, status="pending")
    summary["pending_request"] = _request_view(pending[0]) if pending else None
    return summary


@router.post("/create-checkout-session")
def create_checkout_session(
    body: CheckoutSessionBody,
    request: Request,
    ctx: TenantContext = Depends(require_authenticated),
) -> Dict[str, Any]:
    """Stripe Checkout for one prepaid period; the plan changes only once Stripe reports it paid."""
    if not payments.is_configured():
        raise _payment_error(503, "PAYMENTS_NOT_CONFIGURED", "Online payment is not available.")
    store = _store(request)
    if plans.has_no_end_date(plans.get_or_create_subscription(store, ctx.tenant_id)):
        raise _payment_error(409, "PLAN_NOT_PURCHASABLE", "This workspace's plan has no end date.")
    user: Optional[Dict[str, Any]] = store.get_user_by_id(ctx.user_id, tenant_id=ctx.tenant_id) if ctx.user_id else None
    try:
        session = payments.create_checkout_session(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            email=(user or {}).get("email"),
            plan=body.plan,
            cycle=body.cycle,
            base_url=_public_base_url(request),
            locale=body.locale,
        )
    except payments.PaymentsUnavailable as exc:
        raise _payment_error(503, "PAYMENTS_NOT_CONFIGURED", "Online payment is not available.") from exc
    except payments.PaymentProviderError as exc:
        logger.error("Stripe Checkout for tenant=%s (%s/%s) failed: %s", ctx.tenant_id, body.plan, body.cycle, exc)
        raise _payment_error(502, "PAYMENT_PROVIDER_ERROR", "Could not open the payment page. Please try again.") from exc
    logger.info("Checkout session %s: tenant=%s -> %s/%s", session["id"], ctx.tenant_id, body.plan, body.cycle)
    return {
        "url": session["url"],
        "session_id": session["id"],
        "plan": body.plan,
        "cycle": body.cycle,
        "amount": plans.PLANS[body.plan]["prices"][body.cycle],
        "currency": plans.CURRENCY,
    }


@router.get("/checkout-session/{session_id}")
def checkout_session_status(
    session_id: str,
    request: Request,
    ctx: TenantContext = Depends(require_authenticated),
) -> Dict[str, Any]:
    """The success page's check: applies the session if Stripe says it is paid (once, like the webhook)."""
    if not payments.SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=404, detail="Checkout session not found")
    if not payments.is_configured():
        raise _payment_error(503, "PAYMENTS_NOT_CONFIGURED", "Online payment is not available.")
    try:
        session = payments.retrieve_checkout_session(session_id)
    except payments.CheckoutSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="Checkout session not found") from exc
    except payments.PaymentProviderError as exc:
        logger.error("Could not retrieve checkout session %s: %s", session_id, exc)
        raise _payment_error(502, "PAYMENT_PROVIDER_ERROR", "Could not reach the payment provider.") from exc
    if (session.get("metadata") or {}).get("workspace_id") != ctx.tenant_id:
        raise HTTPException(status_code=404, detail="Checkout session not found")  # another workspace's session

    store = _store(request)
    outcome = payments.fulfill_checkout_session(store, session, source="success_page")
    return {
        "session_id": session_id,
        "status": session.get("status"),
        "payment_status": session.get("payment_status"),
        "paid": session.get("payment_status") == "paid",
        "result": outcome["result"],
        "subscription": plans.subscription_summary(store, ctx.tenant_id),
    }


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
