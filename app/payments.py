"""Stripe Checkout for plan purchases: prepaid periods, no automatic renewal.

Each successful Checkout payment buys one period of a plan, 30 days (monthly) or 365 days
(annual), at the CNY prices in :mod:`app.plans`. Sessions are one-time payments
(``mode=payment``) so card, Alipay and WeChat Pay all work; Stripe cannot renew WeChat Pay or
Alipay automatically. Paying again before the end extends the current period
(:func:`app.plans.paid_period_end`).

Fulfilment (:func:`fulfill_checkout_session`) is idempotent per Checkout Session id (the
``billing_payments`` table) and runs from two places:

* the webhook ``POST /api/billing/webhook`` (``checkout.session.completed`` and
  ``checkout.session.async_payment_succeeded``), which is authoritative;
* the success page (``GET /api/billing/checkout-session/{id}``), which asks Stripe directly so the
  plan is active the moment the merchant returns, even if the webhook is still on its way.

Configuration: ``STRIPE_SECRET_KEY`` (``sk_...`` or a restricted ``rk_...`` key),
``STRIPE_WEBHOOK_SECRET`` (``whsec_...``), ``STRIPE_PAYMENT_METHODS`` (default
``card,alipay,wechat_pay``; ``dashboard`` = everything enabled in the Dashboard) and
``PUBLIC_BASE_URL`` for the return URLs. The list is sent as ``allowed_payment_method_types``,
which filters the methods enabled under Dashboard -> Settings -> Payment methods, so Alipay and
WeChat Pay must be switched on there too. Calls to Stripe honour ``OUTBOUND_PROXY_URL``.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from app import plans
from app.config import env_float, env_str
from app.redaction import describe_exception
from tools.http_client import build_httpx_client, get_outbound_proxy

logger = logging.getLogger("omniflow.payments")

PROVIDER = "stripe"
STRIPE_CURRENCY = "cny"
DEFAULT_PAYMENT_METHODS = ("card", "alipay", "wechat_pay")
WEBHOOK_TOLERANCE_SECONDS = 300
SESSION_ID_RE = re.compile(r"^cs_(?:test|live)_[A-Za-z0-9]{8,250}$")
_METHOD_RE = re.compile(r"^[a-z_]{2,40}$")
CYCLE_LABELS = {"monthly": "月付 Monthly", "annual": "年付 Annual"}


class PaymentsUnavailable(RuntimeError):
    """Stripe is not configured."""


class PaymentProviderError(RuntimeError):
    """Stripe rejected the call or could not be reached (message is redacted)."""


class CheckoutSessionNotFound(LookupError):
    """Stripe has no Checkout Session with that id."""


class InvalidWebhook(ValueError):
    """Missing or wrong Stripe-Signature, or not a Stripe event."""


# ------------------------------------------------------------------ configuration
def secret_key() -> str:
    return env_str("STRIPE_SECRET_KEY")


def is_configured() -> bool:
    return secret_key().startswith(("sk_", "rk_"))


def webhook_secret() -> str:
    return env_str("STRIPE_WEBHOOK_SECRET")


def payment_methods() -> Optional[List[str]]:
    """Payment method types offered in Checkout; ``None`` = Stripe picks them (Dashboard settings)."""
    raw = env_str("STRIPE_PAYMENT_METHODS", default=",".join(DEFAULT_PAYMENT_METHODS))
    if raw.strip().lower() == "dashboard":
        return None
    methods = [m.strip().lower() for m in raw.split(",")]
    return [m for m in methods if _METHOD_RE.match(m)] or list(DEFAULT_PAYMENT_METHODS)


def public_info() -> Optional[Dict[str, Any]]:
    """What the pricing page needs to know (no secrets); ``None`` when online payment is off."""
    if not is_configured():
        return None
    return {"provider": PROVIDER, "payment_methods": payment_methods()}


# ------------------------------------------------------------------ Stripe client
def _outbound_http_client(timeout: float) -> Any:
    """Stripe's httpx transport running on the app's outbound client (proxy mode, no env proxies)."""
    import stripe

    http_client = stripe.HTTPXClient(timeout=timeout, allow_sync_methods=True)
    http_client._client.close()  # replaced by a client that honours OUTBOUND_PROXY_URL
    http_client._client = build_httpx_client(timeout=timeout)
    return http_client


_lock = threading.Lock()
_cached: Optional[Tuple[Tuple[str, Optional[str], float], Any]] = None


def get_client() -> Any:
    """The shared ``stripe.StripeClient``; raises :class:`PaymentsUnavailable` when not configured."""
    global _cached
    if not is_configured():
        raise PaymentsUnavailable("STRIPE_SECRET_KEY is not set")
    signature = (secret_key(), get_outbound_proxy(), env_float("STRIPE_TIMEOUT_SECONDS", 20.0))
    with _lock:
        if _cached is None or _cached[0] != signature:
            import stripe

            client = stripe.StripeClient(
                signature[0], http_client=_outbound_http_client(signature[2]), max_network_retries=2
            )
            _cached = (signature, client)
        return _cached[1]


def reset_client() -> None:
    global _cached
    with _lock:
        _cached = None


# ------------------------------------------------------------------ Checkout
def checkout_params(
    *,
    tenant_id: str,
    plan: str,
    cycle: str,
    base_url: str,
    user_id: Optional[str] = None,
    email: Optional[str] = None,
    locale: Optional[str] = None,
) -> Dict[str, Any]:
    """Parameters for ``POST /v1/checkout/sessions``: one prepaid period of ``plan``."""
    info = plans.PLANS[plan]
    days = plans.PERIOD_DAYS[cycle]
    metadata = {"workspace_id": tenant_id, "plan": plan, "cycle": cycle}
    if user_id:
        metadata["user_id"] = user_id
    base_url = base_url.rstrip("/")
    params: Dict[str, Any] = {
        "mode": "payment",
        "client_reference_id": tenant_id,
        "line_items": [{
            "quantity": 1,
            "price_data": {
                "currency": STRIPE_CURRENCY,
                "unit_amount": plans.price_minor_units(plan, cycle),
                "product_data": {
                    "name": f"OmniFlow {info['name']} · {CYCLE_LABELS[cycle]}",
                    "description": f"{days} 天使用期，不自动续费 / {days}-day plan period, does not renew automatically",
                },
            },
        }],
        "metadata": metadata,
        "payment_intent_data": {"metadata": metadata, "description": f"OmniFlow {info['name']} ({cycle}, {days} days)"},
        "payment_method_options": {"wechat_pay": {"client": "web"}},  # required whenever WeChat Pay may appear
        "success_url": f"{base_url}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base_url}/?checkout=cancelled",
        "locale": locale if locale in ("zh", "en") else "auto",
        "submit_type": "pay",
    }
    methods = payment_methods()
    if methods is not None:
        # API 2026-09-30: a filter on the methods enabled in the Dashboard (payment_method_types is gone).
        params["allowed_payment_method_types"] = methods
        if "wechat_pay" not in methods:
            del params["payment_method_options"]
    if email and "@" in email:
        params["customer_email"] = email
    return params


def create_checkout_session(**kwargs: Any) -> Dict[str, str]:
    """Create a Checkout Session (see :func:`checkout_params`); returns its ``id`` and ``url``."""
    params = checkout_params(**kwargs)
    client = get_client()
    try:
        session = client.v1.checkout.sessions.create(params=params)
    except Exception as exc:  # noqa: BLE001 - stripe.StripeError or a transport failure
        raise PaymentProviderError(describe_exception(exc)) from exc
    return {"id": session.id, "url": session.url}


def retrieve_checkout_session(session_id: str) -> Dict[str, Any]:
    client = get_client()
    try:
        session = client.v1.checkout.sessions.retrieve(session_id)
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "http_status", None) == 404:
            raise CheckoutSessionNotFound(session_id) from exc
        raise PaymentProviderError(describe_exception(exc)) from exc
    return session.to_dict()


# ------------------------------------------------------------------ webhook
def parse_webhook(payload: bytes, signature: Optional[str]) -> Dict[str, Any]:
    """Verify ``Stripe-Signature`` against ``STRIPE_WEBHOOK_SECRET`` and return the event."""
    secret = webhook_secret()
    if not secret:
        raise PaymentsUnavailable("STRIPE_WEBHOOK_SECRET is not set")
    if not signature:
        raise InvalidWebhook("missing Stripe-Signature header")
    import stripe

    try:
        stripe.WebhookSignature.verify_header(payload, signature, secret, WEBHOOK_TOLERANCE_SECONDS)
        event = json.loads(payload)
    except stripe.SignatureVerificationError as exc:
        raise InvalidWebhook("signature does not match") from exc
    except ValueError as exc:  # also UnicodeDecodeError / JSONDecodeError
        raise InvalidWebhook("payload is not JSON") from exc
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise InvalidWebhook("not a Stripe event")
    return event


# ------------------------------------------------------------------ fulfilment
def _payment_intent_id(session: Dict[str, Any]) -> Optional[str]:
    intent = session.get("payment_intent")
    if isinstance(intent, dict):
        intent = intent.get("id")
    return intent if isinstance(intent, str) else None


def fulfill_checkout_session(store: Any, session: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    """Apply a paid Checkout Session to its workspace's subscription, at most once.

    ``result`` is ``activated`` (plan extended now), ``duplicate`` (applied before), ``pending``
    (not paid yet, e.g. an asynchronous method), ``review`` (paid but not applied: wrong amount,
    or the house account) or ``ignored`` (not an OmniFlow plan purchase).
    """
    session_id = session.get("id")
    metadata = session.get("metadata") or {}
    tenant_id = metadata.get("workspace_id")
    plan, cycle = metadata.get("plan"), metadata.get("cycle")
    outcome: Dict[str, Any] = {"session_id": session_id, "tenant_id": tenant_id, "result": "ignored"}

    if (
        not isinstance(session_id, str)
        or not session_id.startswith("cs_")
        or session.get("mode") != "payment"
        or not isinstance(tenant_id, str)
        or plan not in plans.PLANS
        or cycle not in plans.PERIOD_DAYS
    ):
        logger.info("Checkout session %s (%s) is not an OmniFlow plan purchase; ignored", session_id, source)
        return outcome
    if session.get("client_reference_id") not in (None, tenant_id):
        logger.error("Checkout session %s: client_reference_id does not match its workspace; ignored", session_id)
        return outcome
    if session.get("payment_status") != "paid":
        logger.info("Checkout session %s (%s) is %s; waiting for payment", session_id, source, session.get("payment_status"))
        return {**outcome, "result": "pending"}
    if store.get_tenant(tenant_id) is None:
        logger.error("Paid checkout session %s names unknown workspace %s; not applied", session_id, tenant_id)
        return outcome

    amount = session.get("amount_total")
    currency = str(session.get("currency") or "").lower()
    expected = plans.price_minor_units(plan, cycle)
    plans.get_or_create_subscription(store, tenant_id)  # the row must exist so it can be locked

    def activate(current: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if amount != expected or currency != STRIPE_CURRENCY:
            logger.error(
                "Checkout session %s paid %s %s, expected %s %s for %s/%s; kept for review",
                session_id, amount, currency, expected, STRIPE_CURRENCY, plan, cycle,
            )
            return None
        if plans.has_no_end_date(current):
            logger.warning("Checkout session %s paid for the house account %s; kept for review", session_id, tenant_id)
            return None
        return {
            "plan": plan,
            "billing_cycle": cycle,
            "status": "active",
            "current_period_end": plans.paid_period_end(current, plan, cycle),
        }

    payment = store.apply_payment(
        {
            "id": session_id,
            "provider": PROVIDER,
            "plan": plan,
            "billing_cycle": cycle,
            "amount": int(amount) if isinstance(amount, int) else 0,
            "currency": currency or STRIPE_CURRENCY,
            "reference": _payment_intent_id(session),
        },
        tenant_id=tenant_id,
        activate=activate,
    )
    if payment is None:
        return {**outcome, "result": "duplicate"}
    if payment.get("status") != "paid":
        return {**outcome, "result": "review", "payment": payment}
    store.set_upgrade_requests_status("fulfilled", tenant_id=tenant_id)
    logger.info(
        "Stripe payment %s (%s): workspace %s is on %s/%s until %s",
        session_id, source, tenant_id, plan, cycle, payment.get("period_end"),
    )
    return {**outcome, "result": "activated", "payment": payment}
