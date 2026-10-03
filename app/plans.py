"""Paid plans for OmniFlow workspaces. There is no free tier.

* **Pro Growth** - ¥66/month or ¥666/year: 3 connected channels, 300 AI runs per 30-day cycle, 5 GB storage.
* **Agency VIP** - ¥166/month or ¥1666/year: unlimited channels and AI runs, 50 GB storage, priority routing.

A new workspace starts on a Pro trial (``SUBSCRIPTION_TRIAL_DAYS``, default 7 days). When the trial
or a paid period ends, AI generation and channel binding answer 402 until the operator activates a
plan (``scripts/set_plan.py``) after payment. The shared ``default`` workspace is the operator's
house account: Agency VIP without an end date.

Limits of ``None`` mean unlimited. Storage sizes and priority routing are shown on the pricing
page; uploads are not metered yet.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Set, Tuple

from app.config import env_int, env_str
from app.tenancy import DEFAULT_TENANT_ID

PRO = "pro"
AGENCY = "agency"
PLANS: Dict[str, Dict[str, Any]] = {
    PRO: {
        "id": PRO,
        "name": "Pro Growth",
        "prices": {"monthly": 66, "annual": 666},
        "ai_runs": 300,
        "channels": 3,
        "storage_gb": 5,
        "priority_routing": False,
    },
    AGENCY: {
        "id": AGENCY,
        "name": "Agency VIP",
        "prices": {"monthly": 166, "annual": 1666},
        "ai_runs": None,
        "channels": None,
        "storage_gb": 50,
        "priority_routing": True,
    },
}
CURRENCY = "CNY"
BILLING_CYCLES = ("monthly", "annual")
PERIOD_DAYS = {"monthly": 30, "annual": 365}
AI_USAGE_CYCLE_DAYS = 30  # AI runs reset every 30 days, also on annual plans
ACTIVE_STATUSES = ("active", "trial")

# Pages, Instagram accounts and ad accounts count one channel each; API-key channels
# (X, TikTok, 小红书, WeChat) hold one set of credentials per workspace and count once.
MULTI_ACCOUNT_PLATFORMS = {"meta", "instagram", "meta_ads"}
NOT_A_CHANNEL = {"webhook"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def trial_days() -> int:
    return max(0, env_int("SUBSCRIPTION_TRIAL_DAYS", 7))


def parse_time(value: Any) -> Optional[datetime]:
    """Timestamps come back as datetimes (PostgreSQL) or ISO strings (SQLite)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def effective_status(subscription: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """'active' / 'trial' while the period lasts, otherwise 'expired' (no cron job flips the row)."""
    status = subscription.get("status")
    if status not in ACTIVE_STATUSES:
        return "expired"
    end = parse_time(subscription.get("current_period_end"))
    if end is not None and end <= (now or utcnow()):
        return "expired"
    return status


def start_trial(store: Any, tenant_id: str) -> Dict[str, Any]:
    """Pro trial for a new workspace (no-op when the workspace already has a subscription)."""
    return store.ensure_subscription(
        tenant_id=tenant_id,
        plan=PRO,
        billing_cycle="monthly",
        status="trial",
        current_period_end=utcnow() + timedelta(days=trial_days()),
    )


def get_or_create_subscription(store: Any, tenant_id: str) -> Dict[str, Any]:
    """The workspace's subscription; workspaces created before billing existed start a trial now."""
    subscription = store.get_subscription(tenant_id=tenant_id)
    if subscription:
        return subscription
    if tenant_id == DEFAULT_TENANT_ID:
        return store.ensure_subscription(
            tenant_id=tenant_id, plan=AGENCY, billing_cycle="annual", status="active", current_period_end=None
        )
    return start_trial(store, tenant_id)


def channel_key(platform: str, account_id: str) -> Tuple[str, ...]:
    platform = (platform or "").lower()
    return (platform, str(account_id)) if platform in MULTI_ACCOUNT_PLATFORMS else (platform,)


def connected_channel_keys(accounts: Iterable[Dict[str, Any]]) -> Set[Tuple[str, ...]]:
    keys: Set[Tuple[str, ...]] = set()
    for account in accounts:
        platform = (account.get("platform") or "").lower()
        if platform in NOT_A_CHANNEL or account.get("status", "connected") != "connected":
            continue
        keys.add(channel_key(platform, account.get("account_id") or ""))
    return keys


def current_ai_runs(usage: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> int:
    """Runs used in the current cycle (0 once the cycle has ended; the next run starts a new one)."""
    if not usage:
        return 0
    end = parse_time(usage.get("cycle_end"))
    if end is not None and (now or utcnow()) > end:
        return 0
    return int(usage.get("ai_runs_count") or 0)


def checkout_url(plan: str, billing_cycle: str) -> Optional[str]:
    """Optional payment link per plan and cycle, e.g. BILLING_CHECKOUT_URL_PRO_MONTHLY."""
    url = env_str(f"BILLING_CHECKOUT_URL_{plan.upper()}_{billing_cycle.upper()}")
    return url if url.lower().startswith(("https://", "http://")) else None


def public_catalog() -> Dict[str, Any]:
    return {
        "currency": CURRENCY,
        "billing_cycles": list(BILLING_CYCLES),
        "trial_days": trial_days(),
        "plans": [dict(PLANS[PRO]), dict(PLANS[AGENCY])],
    }


def subscription_summary(store: Any, tenant_id: str) -> Dict[str, Any]:
    subscription = get_or_create_subscription(store, tenant_id)
    plan = PLANS.get(subscription.get("plan"), PLANS[PRO])
    now = utcnow()
    status = effective_status(subscription, now)
    end = parse_time(subscription.get("current_period_end"))
    usage = store.get_usage(tenant_id=tenant_id)
    usage_end = parse_time(usage.get("cycle_end")) if usage else None
    channels = connected_channel_keys(store.list_connected_accounts(None, tenant_id=tenant_id))
    return {
        "plan": plan["id"],
        "plan_name": plan["name"],
        "billing_cycle": subscription.get("billing_cycle"),
        "status": status,
        "current_period_end": end.isoformat() if end else None,
        "days_left": max(0, math.ceil((end - now).total_seconds() / 86400)) if end and status != "expired" else None,
        "limits": {"ai_runs": plan["ai_runs"], "channels": plan["channels"], "storage_gb": plan["storage_gb"]},
        "usage": {
            "ai_runs": current_ai_runs(usage, now),
            "ai_cycle_end": usage_end.isoformat() if usage_end and usage_end > now else None,
            "channels": len(channels),
        },
    }
