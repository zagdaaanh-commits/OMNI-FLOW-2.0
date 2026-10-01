"""Storage contract shared by the SQLite (dev/test) and PostgreSQL/Supabase (production) stores.

Tenant scoping rules
--------------------
* Every tenant-scoped method takes a keyword-only ``tenant_id`` defaulting to
  :data:`DEFAULT_TENANT_ID`, so single-tenant call sites keep working unchanged.
* ``save_*`` methods use the model's own ``tenant_id`` and refuse to overwrite a row
  owned by another tenant (the write is skipped and ``CrossTenantWriteError`` raised).
* ``get_*`` methods never return another tenant's row.
* Cross-tenant access exists only for the scheduler worker, through the explicitly
  named ``*_any_tenant`` / ``list_due_tasks`` / ``claim_task`` / ``list_recoverable_tasks``.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from models.schemas import DEFAULT_TENANT_ID, Campaign, ContentDraft, PublishTask

__all__ = [
    "DEFAULT_TENANT_ID",
    "CrossTenantWriteError",
    "Store",
    "iso_utc",
    "mask_token",
    "scoped_id",
    "slugify",
    "utcnow",
]


class CrossTenantWriteError(PermissionError):
    """Attempt to overwrite a row that belongs to a different tenant."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    """Canonical, lexically sortable UTC timestamp string (naive values are treated as UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def mask_token(token: Optional[str]) -> str:
    token = token or ""
    return (token[:6] + "..." + token[-4:]) if len(token) > 10 else "***"


def scoped_id(item_id: str, tenant_id: str) -> str:
    """Namespace a caller-chosen/synthesized id (e.g. Swagger's "string") per tenant.

    Row ids are global primary keys; the default tenant keeps plain ids for backwards
    compatibility, every other tenant gets ``<tenant>:<id>`` so tenants can never collide.
    """
    if tenant_id == DEFAULT_TENANT_ID or item_id.startswith(f"{tenant_id}:"):
        return item_id
    return f"{tenant_id}:{item_id}"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:48] or "tenant"


@runtime_checkable
class Store(Protocol):
    # ---------------------------------------------------------------- tenants
    def create_tenant(self, name: str, slug: Optional[str] = None) -> Dict[str, Any]: ...
    def ensure_tenant(self, tenant_id: str, name: Optional[str] = None) -> Dict[str, Any]: ...
    def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]: ...

    # ------------------------------------------------------------------ users
    def create_user(
        self,
        email: str,
        full_name: str,
        password: str,
        role: str = "Brand Lead",
        company: str = "Global Brand HQ",
        avatar_url: str = "",
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Dict[str, Any]: ...
    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]: ...
    def get_user_by_id(self, user_id: str, *, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]: ...
    def list_users(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> List[Dict[str, Any]]: ...
    def authenticate_user(self, email: str, password: str) -> Optional[Dict[str, Any]]: ...

    # -------------------------------------------------------- social accounts
    def save_connected_account(
        self,
        user_id: str,
        platform: str,
        account_id: str,
        account_name: str,
        access_token: str,
        status: str = "connected",
        permissions: Optional[List[str]] = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Dict[str, Any]: ...
    def get_connected_account(
        self, user_id: Optional[str], platform: str, *, tenant_id: str = DEFAULT_TENANT_ID, account_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]: ...
    def list_connected_accounts(
        self, user_id: Optional[str] = "global", *, tenant_id: str = DEFAULT_TENANT_ID
    ) -> List[Dict[str, Any]]: ...
    def delete_connected_account(self, account_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> bool: ...
    def delete_connected_accounts_for_platform(self, platform: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> int: ...

    # ------------------------------------------------ campaigns/drafts/tasks
    def save_campaign(self, item: Campaign) -> Campaign: ...
    def save_draft(self, item: ContentDraft) -> ContentDraft: ...
    def save_task(self, item: PublishTask) -> PublishTask: ...
    def get_campaign(self, item_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Campaign]: ...
    def get_draft(self, item_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[ContentDraft]: ...
    def get_task(self, item_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[PublishTask]: ...
    def list_campaigns(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> List[Campaign]: ...
    def list_drafts(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> List[ContentDraft]: ...
    def list_tasks(self, *, tenant_id: str = DEFAULT_TENANT_ID, status: Optional[str] = None) -> List[PublishTask]: ...
    def counts(self, *, tenant_id: Optional[str] = None) -> Dict[str, int]: ...

    # ------------------------------------------ scheduler (cross-tenant; worker only)
    def get_task_any_tenant(self, item_id: str) -> Optional[PublishTask]: ...
    def get_draft_any_tenant(self, item_id: str) -> Optional[ContentDraft]: ...
    def list_due_tasks(self, now: datetime, limit: int = 50) -> List[PublishTask]: ...
    def claim_task(self, task_id: str, *, expected_status: str, new_status: str) -> bool: ...
    def list_recoverable_tasks(self, stale_before: datetime) -> List[PublishTask]: ...

    def close(self) -> None: ...
