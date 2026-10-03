#!/usr/bin/env python
"""Operator tool for subscriptions (there is no in-app payment yet).

Typical flow: a merchant clicks "Upgrade" -> you receive the request (LEAD_NOTIFICATION_WEBHOOK or
``--list``) -> the merchant pays -> you activate the plan here.

    python scripts/set_plan.py --list                                   # pending upgrade requests
    python scripts/set_plan.py --email boss@shop.com --show             # current plan and usage
    python scripts/set_plan.py --email boss@shop.com --plan pro --cycle monthly
    python scripts/set_plan.py --workspace <tenant_id> --plan agency --cycle annual --periods 2
    python scripts/set_plan.py --workspace <tenant_id> --expire          # end access now

Activating extends an active paid period of the same plan from its current end date; otherwise
the period starts now. Pending upgrade requests of that workspace are marked fulfilled.
Uses DATABASE_URL (PostgreSQL) or DATABASE_PATH (SQLite), like the app.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import plans  # noqa: E402
from app.config import load_environment  # noqa: E402
from db import create_store  # noqa: E402


def _resolve_workspace(store, args) -> str:
    if args.workspace:
        return args.workspace
    user = store.get_user_by_email(args.email)
    if not user:
        raise SystemExit(f"No account with email {args.email}")
    return user["tenant_id"]


def activate(store, tenant_id: str, plan: str, cycle: str, periods: int) -> dict:
    current = plans.get_or_create_subscription(store, tenant_id)
    now = plans.utcnow()
    start = now
    end = plans.parse_time(current.get("current_period_end"))
    if current.get("status") == "active" and current.get("plan") == plan and end and end > now:
        start = end  # paying again before the period ends extends it
    period_end = start + timedelta(days=plans.PERIOD_DAYS[cycle] * periods)
    store.save_subscription(
        tenant_id=tenant_id, plan=plan, billing_cycle=cycle, status="active", current_period_end=period_end
    )
    store.set_upgrade_requests_status("fulfilled", tenant_id=tenant_id)
    return plans.subscription_summary(store, tenant_id)


def expire(store, tenant_id: str) -> dict:
    current = plans.get_or_create_subscription(store, tenant_id)
    store.save_subscription(
        tenant_id=tenant_id,
        plan=current["plan"],
        billing_cycle=current["billing_cycle"],
        status="expired",
        current_period_end=plans.utcnow(),
    )
    return plans.subscription_summary(store, tenant_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--workspace", help="workspace (tenant) id")
    target.add_argument("--email", help="email of any user in the workspace")
    parser.add_argument("--list", action="store_true", help="list pending upgrade requests")
    parser.add_argument("--show", action="store_true", help="show the workspace's plan and usage")
    parser.add_argument("--plan", choices=sorted(plans.PLANS), help="activate this plan")
    parser.add_argument("--cycle", choices=plans.BILLING_CYCLES, default="monthly")
    parser.add_argument("--periods", type=int, default=1, help="number of billing periods paid (default 1)")
    parser.add_argument("--expire", action="store_true", help="end the workspace's access now")
    args = parser.parse_args(argv)

    load_environment()
    store = create_store()
    try:
        if args.list:
            for item in store.list_upgrade_requests_any_tenant(status="pending"):
                user = store.get_user_by_id(item["user_id"], tenant_id=item["tenant_id"]) if item.get("user_id") else None
                print(f"{item['created_at']}  {item['tenant_id']}  {(user or {}).get('email', '-'):<32} {item['plan']}/{item['billing_cycle']}")
            return 0
        if not (args.workspace or args.email):
            parser.error("--workspace or --email is required")
        tenant_id = _resolve_workspace(store, args)
        if args.plan and args.expire:
            parser.error("use either --plan or --expire")
        if args.periods < 1:
            parser.error("--periods must be at least 1")
        if args.plan:
            result = activate(store, tenant_id, args.plan, args.cycle, args.periods)
        elif args.expire:
            result = expire(store, tenant_id)
        else:
            result = plans.subscription_summary(store, tenant_id)
        print(json.dumps({"workspace": tenant_id, **result}, ensure_ascii=False, indent=2))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
