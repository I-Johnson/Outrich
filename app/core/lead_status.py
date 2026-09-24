"""Consistent lead status, suppression, and pending-email transitions."""
from __future__ import annotations

from app.db import new_id, now_iso, store


VALID_CLIENT_STATUSES = {
    "new", "queued", "contacted", "replied", "demo_booked",
    "do_not_contact", "bounced",
}
NO_FURTHER_EMAIL_STATUSES = {"replied", "demo_booked", "do_not_contact", "bounced"}


def _cancel_pending(lead_id: str, reason: str, storage) -> int:
    pending = storage.list(
        "email_log",
        {"client_id": lead_id, "status": ("in", ["queued", "sending"])},
        order="",
        limit=1000,
    )
    for log in pending:
        storage.update(
            "email_log",
            log["id"],
            {"status": "skipped", "next_attempt_at": None, "error": reason},
        )
    return len(pending)


def _suppress_lead(lead: dict, storage) -> bool:
    email = str(lead.get("email") or "").strip().lower()
    domain = str(lead.get("domain") or "").strip().lower()
    existing = storage.list("suppression", order="", limit=10000)
    if email and any(str(row.get("email") or "").strip().lower() == email for row in existing):
        return False
    if not email and domain and any(str(row.get("domain") or "").strip().lower() == domain for row in existing):
        return False
    if not email and not domain:
        return False
    storage.insert(
        "suppression",
        {
            "id": new_id(),
            "email": email or None,
            "domain": None if email else domain,
            "reason": "manual",
            "created_at": now_iso(),
        },
    )
    return True


def _mark_latest_delivery_replied(lead_id: str, storage) -> bool:
    logs = storage.list("email_log", {"client_id": lead_id}, order="sent_at desc", limit=1000)
    delivered = [log for log in logs if log.get("sent_at")]
    if not delivered:
        return False
    latest = max(delivered, key=lambda log: str(log.get("sent_at") or ""))
    storage.update("email_log", latest["id"], {"status": "replied"})
    return True


def set_client_status(lead_id: str, status: str, *, storage=None) -> dict:
    storage = storage or store
    if status not in VALID_CLIENT_STATUSES:
        raise ValueError(f"Unsupported lead status: {status}")
    lead = storage.get("clients", lead_id)
    if not lead:
        raise ValueError("Lead not found")
    storage.update("clients", lead_id, {"status": status, "updated_at": now_iso()})
    cancelled = 0
    suppressed = False
    reply_recorded = False
    if status in NO_FURTHER_EMAIL_STATUSES:
        cancelled = _cancel_pending(lead_id, f"Lead marked {status.replace('_', ' ')}.", storage)
    if status == "do_not_contact":
        suppressed = _suppress_lead(lead, storage)
    if status in {"replied", "demo_booked"}:
        reply_recorded = _mark_latest_delivery_replied(lead_id, storage)
    return {
        "status": status,
        "cancelled": cancelled,
        "suppressed": suppressed,
        "reply_recorded": reply_recorded,
    }


def delete_unused_client(lead_id: str, *, storage=None) -> bool:
    """Delete a lead only when doing so cannot cascade away delivery history."""
    storage = storage or store
    lead = storage.get("clients", lead_id)
    if not lead:
        return False
    if storage.list("email_log", {"client_id": lead_id}, order="", limit=1):
        raise ValueError("Leads with campaign email history cannot be deleted")
    return bool(storage.delete("clients", {"id": lead_id}))
