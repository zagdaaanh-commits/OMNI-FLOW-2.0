"""Stripe webhook: ``POST /api/billing/webhook``.

Stripe calls this endpoint (no user session; REQUIRE_AUTH does not apply) with a signed event.
The signature is checked against ``STRIPE_WEBHOOK_SECRET`` on the raw body before anything is
parsed, and events older than 5 minutes are refused, so a captured request cannot be replayed.

Handled events (select these when adding the endpoint in the Stripe Dashboard):

* ``checkout.session.completed`` - paid card and wallet payments: the workspace in
  ``metadata.workspace_id`` gets ``metadata.plan`` for one more period (see ``app/payments.py``).
* ``checkout.session.async_payment_succeeded`` - the same, for methods that confirm later.
* ``checkout.session.async_payment_failed`` - logged; the plan is unchanged.

Every payment is applied once (keyed by the Checkout Session id), so Stripe's retries and the
success page racing the webhook are harmless. Answers 2xx for anything that should not be retried
(including events for other products on the same Stripe account); a database failure answers 500
and Stripe retries it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app import payments

logger = logging.getLogger("omniflow.billing.webhook")

router = APIRouter(prefix="/api/billing", tags=["billing"])

FULFIL_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
MAX_PAYLOAD_BYTES = 512 * 1024


def handle_event(store: Any, event: Dict[str, Any]) -> Dict[str, Any]:
    event_type = event["type"]
    session = (event.get("data") or {}).get("object") or {}
    if event_type in FULFIL_EVENTS:
        return payments.fulfill_checkout_session(store, session, source=event_type)
    if event_type == "checkout.session.async_payment_failed":
        logger.warning(
            "Checkout session %s: asynchronous payment failed (workspace %s); plan unchanged",
            session.get("id"), (session.get("metadata") or {}).get("workspace_id"),
        )
        return {"result": "failed"}
    return {"result": "ignored"}


@router.post("/webhook")
async def stripe_webhook(request: Request) -> Dict[str, Any]:
    payload = await request.body()
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        event = payments.parse_webhook(payload, request.headers.get("stripe-signature"))
    except payments.PaymentsUnavailable as exc:
        logger.error("Stripe webhook received but %s", exc)
        raise HTTPException(status_code=503, detail="Stripe webhook is not configured") from exc
    except payments.InvalidWebhook as exc:
        logger.warning("Rejected Stripe webhook: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid Stripe signature or payload") from exc

    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Storage is not initialised")
    outcome = await run_in_threadpool(handle_event, store, event)
    return {"received": True, "type": event["type"], "result": outcome["result"]}
