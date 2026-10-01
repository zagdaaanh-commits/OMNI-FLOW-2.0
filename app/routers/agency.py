"""Meta agency ad-account application intake: ``POST /api/agency/apply``.

Merchants submit their business details from the 快速开户 modal. Each application is stored in
``agency_applications`` for the caller's workspace. When ``LEAD_NOTIFICATION_WEBHOOK`` is set
(an http(s) URL), the lead is also POSTed there after the response is sent; delivery failures
are logged and never affect the merchant's submission.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from app.config import env_float, env_str
from app.tenancy import TenantContext, get_tenant_context
from models.schemas import AgencyApplicationRequest
from tools.http_client import build_async_httpx_client

logger = logging.getLogger("omniflow.agency")

router = APIRouter(prefix="/api/agency", tags=["agency"])

LEAD_EVENT = "agency_application.created"


def lead_webhook_url() -> str:
    url = env_str("LEAD_NOTIFICATION_WEBHOOK")
    return url if url.lower().startswith(("https://", "http://")) else ""


async def notify_lead_webhook(url: str, application: Dict[str, Any]) -> None:
    """POST the lead to the notification webhook; never raises."""
    payload = {"event": LEAD_EVENT, "application": application}
    try:
        async with build_async_httpx_client(timeout=env_float("LEAD_WEBHOOK_TIMEOUT_SECONDS", 10.0)) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            logger.warning("Lead webhook returned HTTP %s for application %s", resp.status_code, application.get("id"))
    except Exception as exc:  # noqa: BLE001 - notification is best effort
        logger.warning("Lead webhook delivery failed for application %s: %s", application.get("id"), type(exc).__name__)


@router.post("/apply")
def submit_agency_application(
    payload: AgencyApplicationRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: TenantContext = Depends(get_tenant_context),
) -> Dict[str, str]:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Storage is not initialised")

    record = store.save_agency_application({**payload.model_dump(), "user_id": ctx.user_id}, tenant_id=ctx.tenant_id)
    logger.info("Agency application %s received for tenant=%s", record["id"], ctx.tenant_id)

    url = lead_webhook_url()
    if url:
        background_tasks.add_task(notify_lead_webhook, url, record)

    return {"status": "success", "message": "Application received", "id": record["id"]}
