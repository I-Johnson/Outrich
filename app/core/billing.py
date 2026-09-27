"""Stripe billing: one plan, $25/month, grandfathered legacy accounts.

Mirrors the ContractorBackend billing flow (same Stripe account): a Checkout
Session to subscribe, the Customer Portal to manage, and signed webhooks for
every state change. Subscription state lives on the ``app_users`` row.

Grandfathering: migration 202609270002 sets ``billing_exempt = 1`` on every
account that exists when billing launches; those accounts keep full access.
Accounts created after launch keep the column default 0 and need an active
subscription.

When the Stripe env vars are absent the deployment runs with billing off:
everyone has access and the billing page says billing is not configured. The
test suite runs in that mode.
"""
from __future__ import annotations

from datetime import datetime, timezone

import stripe

from app.config import settings as env

PLAN_MONTHLY_USD = 25
ACTIVE_STATUSES = {"trialing", "active"}


class BillingNotConfigured(RuntimeError):
    """Raised when a Stripe call is attempted without the required env keys."""


def configured() -> bool:
    return bool(env.STRIPE_SECRET_KEY and env.STRIPE_PRICE_ID)


def webhook_configured() -> bool:
    return bool(env.STRIPE_SECRET_KEY and env.STRIPE_WEBHOOK_SECRET)


def _period_end(user: dict) -> datetime | None:
    raw = user.get("stripe_current_period_end")
    if not raw:
        return None
    try:
        end = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return end if end.tzinfo else end.replace(tzinfo=timezone.utc)


def has_access(user: dict | None, *, admin: bool = False, now: datetime | None = None) -> bool:
    """Whether the account may use the app.

    - The verified env admin is always in.
    - Billing off (Stripe keys absent) lets everyone in.
    - Grandfathered accounts (billing_exempt) keep access forever.
    - trialing/active subscriptions are in; a canceled or past_due
      subscription keeps access until the end of the paid period (the account
      already paid for that time); everything else is out.
    """
    if not user:
        return False
    if admin or user.get("role") == "admin":
        return True
    if not configured():
        return True
    if int(user.get("billing_exempt") or 0):
        return True
    status = str(user.get("stripe_subscription_status") or "")
    if status in ACTIVE_STATUSES:
        return True
    if status in {"canceled", "past_due"}:
        end = _period_end(user)
        if end and end > (now or datetime.now(timezone.utc)):
            return True
    return False


def plan_state(user: dict | None, *, admin: bool = False) -> dict:
    """Template-facing view of the account's plan."""
    user = user or {}
    status = str(user.get("stripe_subscription_status") or "")
    end = _period_end(user)
    access = has_access(user, admin=admin)
    if admin or user.get("role") == "admin":
        label, tone = "Admin", "ok"
    elif int(user.get("billing_exempt") or 0):
        label, tone = "Early access - free forever", "ok"
    elif status in ACTIVE_STATUSES:
        label, tone = "Active", "ok"
    elif status == "canceled" and access:
        label, tone = f"Active until {end:%b %-d, %Y}" if end else "Canceled", "warn"
    elif status == "past_due":
        label = f"Payment failed - access through {end:%b %-d, %Y}" if access and end else "Payment failed"
        tone = "warn"
    elif status:
        label, tone = status.replace("_", " ").capitalize(), "warn"
    else:
        label, tone = "No subscription", "warn"
    return {
        "configured": configured(),
        "price": PLAN_MONTHLY_USD,
        "status": status,
        "label": label,
        "tone": tone,
        "has_access": access,
        "exempt": bool(int(user.get("billing_exempt") or 0)),
        "is_admin": bool(admin or user.get("role") == "admin"),
        "can_manage": bool(user.get("stripe_customer_id")),
        "renews_at": end.strftime("%b %-d, %Y") if end and status in ACTIVE_STATUSES else "",
        "access_until": end.strftime("%b %-d, %Y") if end and status in {"canceled", "past_due"} and access else "",
    }


def create_checkout_session(user: dict, *, success_url: str, cancel_url: str) -> str:
    """Create a Stripe Checkout Session for the $25/month plan; return its URL."""
    if not configured():
        raise BillingNotConfigured("Stripe is not configured")
    stripe.api_key = env.STRIPE_SECRET_KEY
    params = {
        "mode": "subscription",
        "line_items": [{"price": env.STRIPE_PRICE_ID, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(user["id"]),
        "metadata": {"user_id": str(user["id"]), "email": user.get("email") or ""},
        "subscription_data": {"metadata": {"user_id": str(user["id"])}},
    }
    if user.get("stripe_customer_id"):
        params["customer"] = user["stripe_customer_id"]
    else:
        params["customer_email"] = user.get("email") or ""
    session = stripe.checkout.Session.create(**params)
    return session.url


def create_portal_session(user: dict, *, return_url: str) -> str:
    """Create a Stripe Customer Portal session; return its URL."""
    if not configured():
        raise BillingNotConfigured("Stripe is not configured")
    if not user.get("stripe_customer_id"):
        raise ValueError("No subscription found. Please subscribe first.")
    stripe.api_key = env.STRIPE_SECRET_KEY
    portal = stripe.billing_portal.Session.create(customer=user["stripe_customer_id"], return_url=return_url)
    return portal.url


def construct_webhook_event(payload: bytes, sig_header: str | None):
    """Verify the Stripe signature and return the event. Raises ValueError."""
    if not webhook_configured():
        raise BillingNotConfigured("Stripe is not configured")
    if not sig_header:
        raise ValueError("Missing Stripe signature")
    try:
        return stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=env.STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError as exc:
        raise ValueError("Invalid signature") from exc


def apply_subscription(storage, user: dict, subscription: dict, customer_id: str | None = None) -> None:
    """Store subscription state from Stripe on the account row."""
    status = str(subscription.get("status") or "")
    period_end_ts = subscription.get("current_period_end")
    items = (subscription.get("items") or {}).get("data") or []
    values = {
        "stripe_subscription_id": subscription.get("id") or "",
        "stripe_subscription_status": status,
        "stripe_current_period_end": datetime.fromtimestamp(period_end_ts, tz=timezone.utc).isoformat() if period_end_ts else "",
    }
    if customer_id:
        values["stripe_customer_id"] = customer_id
    if items:
        price_id = ((items[0] or {}).get("price") or {}).get("id")
        if price_id:
            values["stripe_price_id"] = price_id
    storage.update("app_users", user["id"], values)


def _find_user(storage, *, user_id=None, customer_id=None, subscription_id=None) -> dict | None:
    if user_id:
        user = storage.get("app_users", str(user_id))
        if user:
            return user
    if customer_id:
        rows = storage.list("app_users", {"stripe_customer_id": customer_id}, order="", limit=1)
        if rows:
            return rows[0]
    if subscription_id:
        rows = storage.list("app_users", {"stripe_subscription_id": subscription_id}, order="", limit=1)
        if rows:
            return rows[0]
    return None


def handle_webhook_event(storage, event: dict) -> str:
    """Apply one verified Stripe event. Returns a short outcome for logging."""
    event_type = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    if event_type == "checkout.session.completed":
        user_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("user_id")
        user = _find_user(storage, user_id=user_id)
        if not user:
            return "checkout.session.completed: unknown user"
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        if subscription_id:
            stripe.api_key = env.STRIPE_SECRET_KEY
            subscription = stripe.Subscription.retrieve(subscription_id)
            apply_subscription(storage, user, subscription, customer_id)
        elif customer_id:
            storage.update("app_users", user["id"], {"stripe_customer_id": customer_id})
        return f"checkout.session.completed: user {user['id']} active"
    if event_type in {"customer.subscription.updated", "customer.subscription.deleted"}:
        subscription = obj
        user = _find_user(
            storage,
            user_id=(subscription.get("metadata") or {}).get("user_id"),
            customer_id=subscription.get("customer"),
            subscription_id=subscription.get("id"),
        )
        if not user:
            return f"{event_type}: unknown user"
        if event_type == "customer.subscription.deleted":
            period_end_ts = subscription.get("current_period_end")
            storage.update("app_users", user["id"], {
                "stripe_subscription_status": "canceled",
                "stripe_current_period_end": datetime.fromtimestamp(period_end_ts, tz=timezone.utc).isoformat() if period_end_ts else "",
            })
        else:
            apply_subscription(storage, user, subscription, subscription.get("customer"))
        return f"{event_type}: user {user['id']}"
    if event_type in {"invoice.paid", "invoice.payment_failed"}:
        subscription_id = obj.get("subscription")
        if not subscription_id:
            return f"{event_type}: not a subscription invoice"
        user = _find_user(storage, customer_id=obj.get("customer"), subscription_id=subscription_id)
        if not user:
            return f"{event_type}: unknown user"
        if event_type == "invoice.paid":
            stripe.api_key = env.STRIPE_SECRET_KEY
            subscription = stripe.Subscription.retrieve(subscription_id)
            apply_subscription(storage, user, subscription, obj.get("customer"))
        else:
            storage.update("app_users", user["id"], {"stripe_subscription_status": "past_due"})
        return f"{event_type}: user {user['id']}"
    return f"unhandled: {event_type}"
