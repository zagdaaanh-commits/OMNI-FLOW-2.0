"""Shared helpers used by both the web app and the scheduler worker."""
from __future__ import annotations

import logging
from typing import Any, Dict

from db.base import DEFAULT_TENANT_ID, Store
from models.schemas import PublishTask

logger = logging.getLogger("omniflow.services")


def resolve_task_credentials(store: Store, task: PublishTask) -> Dict[str, Any]:
    """Facebook page credentials to publish ``task`` with.

    * Non-default tenants publish ONLY with their own connected Page (never with the
      process-wide token, which belongs to the default workspace).  ``isolated=True`` tells
      the publisher to never fall back to it; a tenant without a page gets a simulated publish.
    * The default tenant returns ``{}`` so the publisher uses ``FACEBOOK_PAGE_*`` from the
      environment, preserving the original single-merchant behaviour.
    """
    if task.tenant_id == DEFAULT_TENANT_ID:
        return {}
    account = store.get_connected_account(None, "meta", tenant_id=task.tenant_id)
    if not account or account.get("status") != "connected":
        logger.info("Tenant %s has no connected Facebook page", task.tenant_id)
        return {"page_id": None, "access_token": None, "isolated": True}
    return {"page_id": account["account_id"], "access_token": account["access_token"], "isolated": True}
