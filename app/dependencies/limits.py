"""Plan enforcement: ``Depends(verify_plan_limit("ai_generation" | "channels"))``.

* Every gated feature needs an active or trial subscription, otherwise
  402 ``SUBSCRIPTION_REQUIRED``.
* ``ai_generation``: Pro allows 300 AI runs per 30-day cycle. The dependency refuses the request
  with 403 ``AI_LIMIT_REACHED`` before any model call once the quota is used up. The endpoint then
  wraps the work in ``with gate.ai_run() as run:``, which takes one run atomically
  (``increment_ai_runs``, so concurrent requests cannot overshoot) and gives it back when the
  request fails or only a template draft could be written (``run.keep_if_ai(result)``).
* ``channels``: Pro allows 3 connected channels. Which account is being bound is only known
  inside the endpoint, so it calls ``gate.require_channel_slot(platform, account_id)``:
  re-binding or refreshing a channel that is already connected is always allowed, a new one
  beyond the cap is 403 ``CHANNEL_LIMIT_REACHED``.

Agency VIP has no limits; its AI runs are still counted.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Optional

from fastapi import Depends, HTTPException, Request

from app import plans
from app.tenancy import TenantContext, get_tenant_context

SUBSCRIPTION_REQUIRED = "SUBSCRIPTION_REQUIRED"
AI_LIMIT_REACHED = "AI_LIMIT_REACHED"
CHANNEL_LIMIT_REACHED = "CHANNEL_LIMIT_REACHED"
FEATURES = ("ai_generation", "channels")


def subscription_required() -> HTTPException:
    return HTTPException(status_code=402, detail={"code": SUBSCRIPTION_REQUIRED, "message": "Subscription required."})


def ai_limit_reached(limit: int) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={"code": AI_LIMIT_REACHED, "message": f"Pro monthly AI quota ({limit}/{limit}) reached. Upgrade to VIP."},
    )


def channel_limit_reached(limit: int) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={"code": CHANNEL_LIMIT_REACHED, "message": f"Pro channel cap ({limit} max) reached. Upgrade to VIP."},
    )


def produced_ai_output(result: Any) -> bool:
    """True when at least one draft was written by the model (not the template fallback)."""
    drafts = result.get("drafts") if isinstance(result, dict) else result
    for draft in drafts or []:
        meta = draft.get("metadata") if isinstance(draft, dict) else getattr(draft, "metadata", None)
        if (meta or {}).get("status") == "ai_generated":
            return True
    return False


@dataclass
class AIRun:
    """One AI run taken from the workspace's quota; ``refund()`` gives it back."""

    gate: "PlanGate"
    refunded: bool = False

    def refund(self) -> None:
        if not self.refunded:
            self.gate.store.release_ai_run(tenant_id=self.gate.tenant_id)
            self.refunded = True

    def keep_if_ai(self, result: Any) -> None:
        """Only model-written output is charged; template drafts are free."""
        if not produced_ai_output(result):
            self.refund()


@dataclass
class PlanGate:
    store: Any
    tenant_id: str
    subscription: Dict[str, Any]
    status: str
    _channels: Optional[set] = field(default=None, repr=False)

    @property
    def plan(self) -> Dict[str, Any]:
        return plans.PLANS.get(self.subscription.get("plan"), plans.PLANS[plans.PRO])

    @property
    def active(self) -> bool:
        return self.status in plans.ACTIVE_STATUSES

    def require_active(self) -> None:
        if not self.active:
            raise subscription_required()

    # ------------------------------------------------------------------ AI runs
    def check_ai_quota(self) -> None:
        limit = self.plan["ai_runs"]
        if limit is not None and plans.current_ai_runs(self.store.get_usage(tenant_id=self.tenant_id)) >= limit:
            raise ai_limit_reached(limit)

    @contextmanager
    def ai_run(self) -> Iterator[AIRun]:
        limit = self.plan["ai_runs"]
        count = self.store.increment_ai_runs(tenant_id=self.tenant_id, limit=limit)
        if count is None:  # another request took the last run
            raise ai_limit_reached(limit)
        run = AIRun(self)
        try:
            yield run
        except BaseException:
            run.refund()
            raise

    # ----------------------------------------------------------------- channels
    def channel_keys(self) -> set:
        if self._channels is None:
            self._channels = plans.connected_channel_keys(self.store.list_connected_accounts(None, tenant_id=self.tenant_id))
        return self._channels

    def channel_slots_left(self) -> Optional[int]:
        limit = self.plan["channels"]
        return None if limit is None else max(0, limit - len(self.channel_keys()))

    def is_connected(self, platform: str, account_id: str) -> bool:
        return plans.channel_key(platform, account_id) in self.channel_keys()

    def require_channel_slot(self, platform: str, account_id: str) -> None:
        """403 when binding this account would exceed the plan's channel cap."""
        limit = self.plan["channels"]
        if limit is None or self.is_connected(platform, account_id):
            return
        if len(self.channel_keys()) >= limit:
            raise channel_limit_reached(limit)
        self.channel_keys().add(plans.channel_key(platform, account_id))


def load_plan_gate(store: Any, tenant_id: str) -> PlanGate:
    subscription = plans.get_or_create_subscription(store, tenant_id)
    return PlanGate(store=store, tenant_id=tenant_id, subscription=subscription, status=plans.effective_status(subscription))


def verify_plan_limit(feature: str) -> Callable[..., PlanGate]:
    """Dependency factory: checks the caller's subscription for ``feature`` and returns a :class:`PlanGate`."""
    if feature not in FEATURES:
        raise ValueError(f"Unknown plan feature: {feature!r}")

    def dependency(request: Request, ctx: TenantContext = Depends(get_tenant_context)) -> PlanGate:
        gate = load_plan_gate(request.app.state.store, ctx.tenant_id)
        gate.require_active()
        if feature == "ai_generation":
            gate.check_ai_quota()
        return gate

    dependency.__name__ = f"verify_plan_limit_{feature}"
    return dependency
