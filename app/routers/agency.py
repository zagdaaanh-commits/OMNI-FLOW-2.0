"""Meta agency ad-account application intake: ``POST /api/agency/apply``.

Merchants submit their business details from the 快速开户 modal. Each application is stored in
``agency_applications`` for the caller's workspace, optionally with ``business_license_path``: a
file the same workspace uploaded through ``POST /api/upload/document``. When ``LEAD_NOTIFICATION_WEBHOOK`` is set
(an http(s) URL), the lead is also POSTed there after the response is sent; delivery failures
are logged and never affect the merchant's submission.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from app.config import env_float, env_str
from app.object_storage import parse_object_path
from app.routers.notifications import notify_workspace
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

    license_path = payload.business_license_path
    if license_path is not None:
        # Only a document this workspace uploaded (POST /api/upload/document) can be attached.
        parsed = parse_object_path(license_path)
        if parsed is None or parsed[0] != ctx.tenant_id or parsed[1] not in ("pdf", "png", "jpg"):
            raise HTTPException(
                status_code=422,
                detail=[{
                    "loc": ["body", "business_license_path"],
                    "msg": "must be a business license uploaded by this workspace",
                    "type": "value_error",
                }],
            )

    record = store.save_agency_application({**payload.model_dump(), "user_id": ctx.user_id}, tenant_id=ctx.tenant_id)
    logger.info("Agency application %s received for tenant=%s", record["id"], ctx.tenant_id)

    url = lead_webhook_url()
    if url:  # the platform operator's lead intake (server environment)
        background_tasks.add_task(notify_lead_webhook, url, record)
    # The merchant's own notification webhook, if their workspace configured one.
    background_tasks.add_task(
        notify_workspace,
        store,
        ctx.tenant_id,
        "agency_application.created",
        "📝 新的开户申请 / New ad account application",
        [
            f"公司 Company: {record['company_name']}",
            f"店铺 Store: {record['store_url']}",
            f"联系方式 Contact: {record['contact']}",
            f"营业执照 Business license: {'已上传 attached' if record.get('business_license_path') else '未上传 not attached'}",
            f"申请编号 ID: {record['id']}",
        ],
        {"id": record["id"], "company_name": record["company_name"], "store_url": record["store_url"]},
    )

    return {"status": "success", "message": "Application received", "id": record["id"]}
