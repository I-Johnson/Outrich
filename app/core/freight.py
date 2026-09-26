"""Freight missions, load economics, one-to-one mail, and guarded reply handling."""
from __future__ import annotations

import base64
import email
import imaplib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup

from app.config import settings
from app.adapters import get_provider
from app.core.freight_agent import compose_counter_reply, interpret_broker_reply
from app.core.gmail_senders import get_gmail_sender, list_gmail_senders, sender_context, sender_password
from app.core.leads import valid_email
from app.core.template_engine import render_template
from app.db import new_id, now_iso, store

logger = logging.getLogger(__name__)

PROTECTED_PATTERNS = {
    "call_requested": re.compile(r"\b(call me|give me a call|call us|phone me|ring me|(?:reach|call|phone|text) me at\s*\+?\d+)\b", re.I),
    "sensitive_driver_info": re.compile(
        r"\b(full (?:drivers?|drv) info(?:rmation)?|drivers?(?:'s)? info(?:rmation)?|drv info(?:rmation)?|"
        r"driver(?:'s)? (?:details|information|info|data)|driver details|driver license|cdl|date of birth|dob|social security|ssn|"
        r"(?:send|share|give|provide|forward)(?:\s+(?:me|us|over))?\s+their\s+details?)\b",
        re.I,
    ),
    "rate_confirmation": re.compile(r"\b(rate[\s_-]*con(?:firmation)?|confirmation attached|sign(?:ed)? confirmation)\b", re.I),
    "price_accepted": re.compile(r"\b(we accept|accepted|that works|rate works|book it|you got it|agreed|deal|confirmed at)\b", re.I),
    # These terms change the job, payment risk, or truck obligations. A rate
    # counter alone is not an answer to them, even if the model misses them.
    "operational_terms": re.compile(
        r"\b(?:dedicated\s+(?:carrier|lane)|\d+\s+loads?\s+a\s+day|"
        r"hook\s+and\s+(?:drop|live)|factoring\s+(?:company|denied|won't|will\s+not)|"
        r"credit\s+rating|\bTWIC\b|oversize\s+permits?|escorts?|"
        r"self[ -]?load|\bPPE\b|\b(?:two|three|multiple|[2-9])\s+(?:pickups?|drops?)|"
        r"\d+\s*°?\s*F\b.{0,25}\bcontinuous\b|"
        r"(?:authority|MC)\s+for\s+at\s+least|home\s+time|"
        r"cannot\s+legally\s+(?:take|haul)|hazmat\s+endorsement|"
        r"send\s+(?:photos?|BOL|POD)\b)\b", re.I),
    "rate_refused": re.compile(r"\b(?:can(?:not|'t)|won't|unable\s+to)\s+(?:get|go|come|do)\s+(?:up\s+)?to\s+\$?\s*\d", re.I),
}
TEAM_QUESTION = re.compile(r"\b(true team|team truck|team drivers?|solo or team|is (?:it|this) a team)\b", re.I)
EQUIPMENT_QUESTION = re.compile(
    r"\b(?:what|which)\s+(?:kind\s+of\s+)?(?:equipment|trailer)\b"
    r"|\b(?:equipment|trailer)\s+type\s*\??"
    r"|\b(?:do\s+you\s+have|have\s+you\s+got|are\s+you\s+running|can\s+you\s+provide|is\s+(?:it|this)(?:\s+a)?)\b[^?.\n]{0,65}\b(?:dry\s*van|reefer|flatbed|53\s*(?:ft|foot))\b"
    r"|\b(?:dry\s*van|reefer|flatbed|53\s*(?:ft|foot))\s*\?",
    re.I,
)
MC_QUESTION = re.compile(r"\b(mc(?:\s*number|\s*#)?|dot(?:\s*number|\s*#)?)\b", re.I)
NUMBER_CANDIDATE = re.compile(r"(?<![\w])(?P<currency>\$)?\s*(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?![\w])")
PHONE_NUMBER = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?!\d)|(?<!\d)\d{3}[\s.-]\d{4}(?!\d)")
DATE_NUMBER = re.compile(r"(?<!\d)(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)(?!\d)")
AUTHORITY_ID = re.compile(r"\b(?:MC(?:\s*(?:number|#))?|USDOT|DOT(?:\s*(?:number|#))?)\s*[:#-]?\s*\d{4,10}\b", re.I)
SENSITIVE_OUTBOUND = {
    "authority ID (MC/USDOT)": AUTHORITY_ID,
    "phone number": PHONE_NUMBER,
    "driver identity information": re.compile(r"\b(?:cdl|commercial driver's license|driver(?:'s)? license|date of birth|\bdob\b|social security|\bssn\b|driver details|driver information|driver info)\b", re.I),
    "insurance or financial document": re.compile(r"\b(?:insurance certificate|certificate of insurance|\bcoi\b|w-?9|bank(?:ing)? details?|routing number|account number|factoring|void(?:ed)? check)\b", re.I),
    "vehicle identifier": re.compile(r"\b(?:vin|vehicle identification number|license plate|plate number|tractor number|trailer number)\b", re.I),
}
# Driver and vehicle identity lives on the truck profile for booking paperwork.
# These values are never passed to the model, never shareable, and a draft that
# contains a stored value cannot be sent at all - even with a dispatcher override.
NEVER_SEND_PROFILE_FIELDS = {
    "truck_vin": "truck VIN",
    "driver_name": "driver name",
    "driver_cdl_number": "driver CDL number",
    "driver_cdl_state": "driver CDL state",
    "driver_phone": "driver phone",
}
# Only these profile facts can ever be marked shareable with brokers.
SHAREABLE_FIELD_ALLOWLIST = {"equipment_type", "team_status", "mc_number", "dot_number", "trailer_length_ft"}


SHAREABLE_FIELD_LABELS = {
    "equipment_type": "Equipment type",
    "team_status": "Team driver status",
    "mc_number": "MC number",
    "dot_number": "USDOT number",
}


def shareable_field_labels(fields: list[str]) -> list[str]:
    return [SHAREABLE_FIELD_LABELS.get(field, field) for field in fields or []]


def filter_shareable_fields(fields: list[str]) -> list[str]:
    """Server-side guard: crafted form posts cannot widen the shareable set."""
    return [field for field in fields if field in SHAREABLE_FIELD_ALLOWLIST]


def _sensitive_profile_values_in_text(profile: dict, text: str) -> list[str]:
    """Stored driver/vehicle values found in an outbound text. Hard block."""
    folded = text.casefold()
    hits: list[str] = []
    for field, label in NEVER_SEND_PROFILE_FIELDS.items():
        value = str(profile.get(field) or "").strip()
        if value and value.casefold() in folded:
            hits.append(label)
    return hits


def _load_window(load: dict, storage=None) -> tuple[str, str] | None:
    """Occupied window for a load: pickup date through delivery.

    Delivery comes from the last delivery stop's appointment date when known,
    otherwise from loaded miles at 500 miles per transit day. A truck cannot be
    in two places at once, so overlapping windows conflict - same-date-only
    checks miss multi-day transit.
    """
    from datetime import date as _date, timedelta as _delta
    pickup = str(load.get("pickup_date") or "")[:10]
    if not pickup:
        return None
    delivery = ""
    if load.get("id"):
        stops = (storage or store).list("freight_load_stops", {"load_id": load["id"]}, order="seq asc", limit=50)
        dates = [_normalize_con_date(s.get("appointment")) for s in stops
                 if not s.get("removed_at") and (s.get("kind") or "") == "delivery" and s.get("appointment")]
        dates = [d for d in dates if re.match(r"^\d{4}-\d{2}-\d{2}$", d)]
        if dates:
            delivery = max(dates)
    if not delivery:
        miles = _number(load.get("loaded_miles")) or 0
        days = max(1, -(-int(miles) // 500))
        try:
            delivery = (_date.fromisoformat(pickup) + _delta(days=days)).isoformat()
        except ValueError:
            return None
    return (pickup, delivery)


def truck_availability(profile: dict, load: dict | None = None, storage=None) -> dict[str, str]:
    """Live availability of a truck, including booked-load window conflicts."""
    status = str(profile.get("availability_status") or "available")
    detail = ""
    if status == "off":
        detail = "Truck is marked off duty"
    elif status == "booked":
        detail = "Truck is marked booked"
    if status == "booking":
        # The booking mutex is a leased claim: a crashed booking flow leaves
        # 'booking' behind, so an old or timestamp-less claim must not strand
        # the truck forever - it recovers to available.
        claim_at = str(profile.get("booking_claim_at") or "")
        stale = True
        if claim_at:
            try:
                stale = datetime.now(timezone.utc) - datetime.fromisoformat(claim_at) > timedelta(minutes=10)
            except ValueError:
                stale = True
        if stale:
            status, detail = "available", ""
        else:
            status, detail = "conflict", "Another booking is in progress"
    pickup = str((load or {}).get("pickup_date") or "")
    window = _load_window(load, storage) if load else None
    if status == "available" and window and profile.get("id"):
        conflict = _truck_window_conflict(str(profile["id"]), load, storage)
        if conflict:
            status, detail = "conflict", conflict
    available_from = str(profile.get("available_from") or "")
    if status == "available" and available_from and pickup and pickup < available_from:
        status, detail = "conflict", f"Not available until {available_from}"
    return {"status": status, "detail": detail}


def _truck_window_conflict(profile_id: str, load: dict, storage=None) -> str:
    """First window conflict with any booked load on this truck, or "".

    The scan is unbounded (filtered at the database by truck and booked
    status): capping it would let an old booking hide from the overlap check.
    """
    storage = storage or store
    window = _load_window(load, storage) if load else None
    if not window:
        return ""
    for row in storage.list("freight_loads", {"truck_profile_id": profile_id, "status": "booked"}, order="", limit=10000):
        if row["id"] == (load or {}).get("id"):
            continue
        other = _load_window(row, storage)
        if other and window[0] <= other[1] and other[0] <= window[1]:
            return f"Booked {other[0]} to {other[1]}"
    return ""


BROKER_CREDIT_STATUSES = {"unknown", "approved", "denied", "exempt"}
BROKER_SETUP_STATUSES = {"not_started", "packet_sent", "complete"}


def _email_domain(email: str) -> str:
    return str(email or "").split("@")[-1].strip().lower() if "@" in str(email or "") else ""


FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com",
    "live.com", "msn.com", "comcast.net", "me.com", "protonmail.com", "proton.me",
    "ymail.com", "att.net", "verizon.net", "mail.com", "zoho.com", "gmx.com",
}


def resolve_broker(email: str, company: str, storage=None, *, thread_id: str = "") -> dict | None:
    """Find or create the broker identity for an email address.

    Matching is exact-email first, then company domain. A new broker starts
    unknown / not_started: credit approval and setup are explicit human steps.
    A NEW email attaching to an existing broker by domain inherits that
    broker's credit and setup, so it stays unconfirmed - and blocks booking -
    until a dispatcher explicitly confirms the identity.
    """
    storage = storage or store
    email = str(email or "").strip().lower()
    if not email:
        return None
    domain = _email_domain(email)
    brokers = storage.list("freight_brokers", order="", limit=1000)
    for broker in brokers:
        if email in [str(item).lower() for item in broker.get("emails") or []]:
            return broker
    # Domain matching inherits the company's credit and setup status, so it is
    # only safe on a company-owned domain - never on free-mail providers.
    if domain in FREE_MAIL_DOMAINS:
        domain = ""
    for broker in brokers:
        if domain and (broker.get("domain") or "").lower() == domain:
            emails = list(broker.get("emails") or [])
            if email not in emails:
                emails.append(email)
                # Confirmation is per email address, never broker-wide: the new
                # alias alone is blocked until a dispatcher confirms it, and the
                # broker's existing verified addresses keep working.
                unconfirmed = [str(item).lower() for item in broker.get("unconfirmed_emails") or []]
                if email not in unconfirmed:
                    unconfirmed.append(email)
                broker = storage.update("freight_brokers", broker["id"], {
                    "emails": emails, "unconfirmed_emails": unconfirmed, "updated_at": now_iso()})
                if thread_id:
                    _create_alert(thread_id, "broker_identity_review",
                                  f"New email {email} matched broker {broker.get('legal_name') or domain} by domain. "
                                  "Confirm this is the same company before it inherits credit and setup; booking is blocked until then.",
                                  storage)
            return broker
    stamp = now_iso()
    return storage.insert("freight_brokers", {
        "id": new_id(),
        "legal_name": str(company or "").strip(),
        "domain": domain,
        "emails": [email],
        "credit_status": "unknown",
        "setup_status": "not_started",
        "blocked": False,
        "unconfirmed_emails": [],
        "created_at": stamp,
        "updated_at": stamp,
    })


def update_broker(broker_id: str, values: dict[str, Any], storage=None) -> dict:
    """Dispatcher records broker credit and setup decisions."""
    storage = storage or store
    broker = storage.get("freight_brokers", broker_id)
    if not broker:
        raise ValueError("Broker not found")
    credit = str(values.get("credit_status") or broker.get("credit_status") or "unknown").strip().lower()
    if credit not in BROKER_CREDIT_STATUSES:
        raise ValueError("Unknown credit status")
    setup = str(values.get("setup_status") or broker.get("setup_status") or "not_started").strip().lower()
    if setup not in BROKER_SETUP_STATUSES:
        raise ValueError("Unknown setup status")
    score = _number(values.get("credit_score"))
    # Confirmation is per email link: each pending alias is confirmed on its
    # own. The legacy single checkbox confirms everything still pending.
    unconfirmed = [str(item).lower() for item in broker.get("unconfirmed_emails") or []]
    confirm_emails = [str(item).strip().lower() for item in (values.get("identity_confirm_emails") or []) if str(item).strip()]
    if confirm_emails:
        unconfirmed = [item for item in unconfirmed if item not in confirm_emails]
    elif values.get("identity_confirmed"):
        unconfirmed = []
    return storage.update("freight_brokers", broker_id, {
        "legal_name": str(values.get("legal_name") or broker.get("legal_name") or "").strip(),
        "mc_number": str(values.get("mc_number") or broker.get("mc_number") or "").strip(),
        "credit_status": credit,
        "credit_score": score if score is not None else broker.get("credit_score"),
        "credit_notes": str(values.get("credit_notes") or broker.get("credit_notes") or "").strip(),
        "setup_status": setup,
        "blocked": bool(values.get("blocked")) if "blocked" in values else bool(broker.get("blocked")),
        "identity_confirmed": True if values.get("identity_confirmed") else bool(broker.get("identity_confirmed", True)),
        "unconfirmed_emails": unconfirmed,
        "updated_at": now_iso(),
    })


BOOKING_STATUSES = {"agreed", "rate_con_review", "booked", "cancelled"}


def _agreement_snapshot(load: dict, broker: dict | None, storage) -> dict:
    """Immutable record of what was agreed, captured at acceptance time."""
    stops = [s for s in storage.list("freight_load_stops", {"load_id": load["id"]}, order="seq asc", limit=50) if not s.get("removed_at")]
    return {
        "agreed_at": now_iso(),
        "origin": route_label(load.get("origin_city"), load.get("origin_state")),
        "destination": route_label(load.get("destination_city"), load.get("destination_state")),
        "pickup_date": load.get("pickup_date") or "",
        "equipment": load.get("equipment_type") or "",
        "weight_lbs": load.get("weight_lbs"),
        "loaded_miles": load.get("loaded_miles"),
        "broker": (broker or {}).get("legal_name") or load.get("broker_company") or load.get("broker_email") or "",
        "stops": [{"kind": s.get("kind"), "city": s.get("city"), "state": s.get("state"),
                   "facility": s.get("facility_name"), "appointment": s.get("appointment")} for s in stops],
    }


def record_agreement(thread_id: str, amount: float, source_message_id: str, storage=None) -> dict | None:
    """Capture the immutable agreement snapshot when a price is accepted."""
    storage = storage or store
    thread = storage.get("freight_threads", thread_id)
    if not thread:
        return None
    existing = [b for b in storage.list("freight_bookings", {"thread_id": thread_id}, order="", limit=10)
                if b.get("status") != "cancelled"]
    if existing:
        return existing[0]
    load = storage.get("freight_loads", thread["load_id"]) or {}
    if load.get("id") and not load.get("broker_id"):
        resolved = resolve_broker(load.get("broker_email"), load.get("broker_company"), storage)
        if resolved:
            load = storage.update("freight_loads", load["id"], {"broker_id": resolved["id"], "updated_at": now_iso()})
    broker = storage.get("freight_brokers", load["broker_id"]) if load.get("broker_id") else None
    stamp = now_iso()
    return storage.insert("freight_bookings", {
        "id": new_id(),
        "load_id": thread["load_id"],
        "thread_id": thread_id,
        "status": "agreed",
        "agreed_rate": amount,
        "snapshot": _agreement_snapshot(load, broker, storage),
        "rate_con_diffs": [],
        "rate_con_reviewed": False,
        "driver_handoff_approved": False,
        "source_message_id": source_message_id,
        "created_at": stamp,
        "updated_at": stamp,
    })


_CON_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y", "%B %d, %Y", "%b %d, %Y", "%b %d %Y", "%B %d %Y")


def _normalize_con_date(value) -> str:
    """Normalize a rate-con date to ISO so 09/23/2026 and Sep 23, 2026 compare equal to 2026-09-23."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    # Keep only the date part when a time rides along ("2026-09-23 08:00").
    day = raw.split("T")[0].strip()
    from datetime import datetime as _dt
    for candidate in {day, raw}:
        for fmt in _CON_DATE_FORMATS:
            try:
                return _dt.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else raw.casefold()


REQUIRED_CON_TERMS = (
    ("total_rate", "total rate"),
    ("pickup_city", "pickup city"),
    ("pickup_state", "pickup state"),
    ("delivery_city", "delivery city"),
    ("delivery_state", "delivery state"),
    ("pickup_date", "pickup date"),
)


def _rate_con_diffs(snapshot: dict, agreed_rate: float, rate_con: dict) -> list[dict]:
    """Field-by-field comparison of the rate con against the agreement.

    A term missing from the rate con is "unverified", never silently equal:
    partial evidence must not read as an exact match.
    """
    diffs: list[dict] = []

    def check(field: str, agreed, received, label: str, *, normalize=None) -> None:
        normalize = normalize or (lambda v: str(v).strip().casefold() if v is not None else "")
        agreed_norm = normalize(agreed)
        received_norm = normalize(received)
        # A rate-con value filling a fact the agreement did not record is new
        # information, not a disagreement.
        if not agreed_norm:
            return
        if not received_norm:
            diffs.append({"field": label, "agreed": agreed, "rate_con": None, "status": "unverified"})
        elif agreed_norm != received_norm:
            diffs.append({"field": label, "agreed": agreed, "rate_con": received, "status": "mismatch"})

    rate = _number(rate_con.get("total_rate"))
    if rate is None:
        diffs.append({"field": "total rate", "agreed": agreed_rate, "rate_con": None, "status": "unverified"})
    elif rate != _number(agreed_rate):
        diffs.append({"field": "total rate", "agreed": agreed_rate, "rate_con": rate, "status": "mismatch"})

    stops = snapshot.get("stops") or []
    pickup_stops = [s for s in stops if (s.get("kind") or "") == "pickup"]
    delivery_stops = [s for s in stops if (s.get("kind") or "") == "delivery"]
    agreed_pickup = route_label(pickup_stops[0].get("city"), pickup_stops[0].get("state")) if pickup_stops else snapshot.get("origin")
    agreed_delivery = route_label(delivery_stops[-1].get("city"), delivery_stops[-1].get("state")) if delivery_stops else snapshot.get("destination")
    appointment = (pickup_stops[0].get("appointment") or "") if pickup_stops else ""
    agreed_pickup_date = appointment or snapshot.get("pickup_date")

    check("destination", agreed_delivery, route_label(rate_con.get("delivery_city"), rate_con.get("delivery_state")) if rate_con.get("delivery_city") else None, "delivery")
    check("origin", agreed_pickup, route_label(rate_con.get("pickup_city"), rate_con.get("pickup_state")) if rate_con.get("pickup_city") else None, "pickup")
    check("pickup_date", agreed_pickup_date, rate_con.get("pickup_date"), "pickup date", normalize=_normalize_con_date)
    check("equipment", snapshot.get("equipment"), rate_con.get("equipment"), "equipment")
    agreed_weight = _number(snapshot.get("weight_lbs"))
    con_weight = _number(rate_con.get("weight_lbs"))
    if agreed_weight is not None:
        if con_weight is None:
            diffs.append({"field": "weight", "agreed": agreed_weight, "rate_con": None, "status": "unverified"})
        elif con_weight != agreed_weight:
            diffs.append({"field": "weight", "agreed": agreed_weight, "rate_con": con_weight, "status": "mismatch"})
    return diffs


def _rate_con_version(terms: dict) -> str:
    """Content hash of the terms under review; approvals bind to it."""
    import hashlib
    canonical = json.dumps({k: v for k, v in sorted(terms.items()) if v not in (None, "")}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def submit_rate_con(booking_id: str, values: dict[str, Any], storage=None, *, source: str = "manual", source_ref: str = "") -> dict:
    """Record the broker's rate confirmation terms and compute exact diffs.

    Every new submission replaces the terms under review and resets the gates:
    a review or driver-handoff approval never carries over to new terms. The
    source reference (for a PDF, the message id and filename it came from) is
    retained so the dispatcher can re-open the original document.
    """
    storage = storage or store
    booking = storage.get("freight_bookings", booking_id)
    if not booking:
        raise ValueError("Booking not found")
    if booking.get("status") == "booked":
        raise ValueError("Load is already booked")
    terms = {str(k): (v.strip() if isinstance(v, str) else v) for k, v in (values or {}).items() if v not in (None, "")}
    diffs = _rate_con_diffs(booking.get("snapshot") or {}, booking.get("agreed_rate"), terms)
    return storage.update("freight_bookings", booking_id, {
        "status": "rate_con_review",
        "rate_con_amount": _number(terms.get("total_rate")),
        "rate_con_terms": terms,
        "rate_con_version": _rate_con_version(terms),
        "rate_con_source": source,
        "rate_con_source_ref": source_ref,
        "rate_con_diffs": diffs,
        "rate_con_reviewed": False,
        "rate_con_review_version": "",
        "driver_handoff_approved": False,
        "driver_handoff_version": "",
        "updated_at": now_iso(),
    })


def review_rate_con(booking_id: str, approve: bool, storage=None) -> dict:
    """Dispatcher decision on the rate con. Differences require explicit override."""
    storage = storage or store
    booking = storage.get("freight_bookings", booking_id)
    if not booking:
        raise ValueError("Booking not found")
    if booking.get("status") != "rate_con_review":
        raise ValueError("No rate confirmation is waiting for review")
    if not approve:
        return storage.update("freight_bookings", booking_id, {
            "status": "agreed", "rate_con_diffs": [], "rate_con_reviewed": False,
            "rate_con_review_version": "", "driver_handoff_approved": False, "driver_handoff_version": "",
            "updated_at": now_iso(),
        })
    terms = booking.get("rate_con_terms") or {}
    # Legacy rows predate stored terms; fall back to the amount alone.
    if not terms and booking.get("rate_con_amount") is not None:
        terms = {"total_rate": booking["rate_con_amount"]}
    missing = [label for key, label in REQUIRED_CON_TERMS if terms.get(key) in (None, "")]
    if missing:
        raise ValueError("Rate confirmation is missing required terms: " + ", ".join(missing))
    return storage.update("freight_bookings", booking_id, {
        "rate_con_reviewed": True,
        "rate_con_review_version": booking.get("rate_con_version") or "",
        "updated_at": now_iso(),
    })


def approve_driver_handoff(booking_id: str, storage=None) -> dict:
    """Human gate: releasing driver identity to the broker."""
    storage = storage or store
    booking = storage.get("freight_bookings", booking_id)
    if not booking:
        raise ValueError("Booking not found")
    if not booking.get("rate_con_reviewed"):
        raise ValueError("Review the rate confirmation before releasing driver details")
    return storage.update("freight_bookings", booking_id, {
        "driver_handoff_approved": True,
        "driver_handoff_version": booking.get("rate_con_version") or "",
        "updated_at": now_iso(),
    })


def mark_booked(booking_id: str, storage=None) -> dict:
    """Final commitment: every gate must be green."""
    storage = storage or store
    booking = storage.get("freight_bookings", booking_id)
    if not booking:
        raise ValueError("Booking not found")
    if booking.get("status") == "booked":
        return booking
    if not booking.get("rate_con_reviewed"):
        raise ValueError("Rate confirmation review is required before booking")
    if not booking.get("driver_handoff_approved"):
        raise ValueError("Driver handoff approval is required before booking")
    version = booking.get("rate_con_version") or ""
    if not version:
        # Rows without recorded terms predate version-bound gates; they must be
        # re-submitted and reviewed, never waved through.
        raise ValueError("Rate confirmation terms are required before booking; submit the rate con again")
    if booking.get("rate_con_review_version") != version or booking.get("driver_handoff_version") != version:
        raise ValueError("Rate confirmation changed after review; review and approve the new terms")
    load = storage.get("freight_loads", booking["load_id"]) or {}
    mission = storage.get("freight_missions", load.get("mission_id")) or {}
    profile = storage.get("freight_truck_profiles", load.get("truck_profile_id") or mission.get("truck_profile_id")) or {}
    blockers = _booking_readiness_blockers(load, mission, profile, storage)
    if blockers:
        raise ValueError("Booking is not ready: " + ", ".join(blockers))
    # Per-truck mutex: one booking flow at a time per truck, across loads and
    # processes. The single-statement compare-and-set is atomic in both SQLite
    # and Postgres, so two concurrent bookings for one truck cannot both pass
    # the window check.
    profile_id = str(profile.get("id") or "")
    locked = False
    if profile_id:
        if not storage.claim_field_not("freight_truck_profiles", profile_id, "availability_status", "booking", "booking"):
            raise ValueError("Another booking is in progress for this truck; retry in a moment")
        locked = True
        # Leased claim: the timestamp lets a crashed flow's leftover claim be
        # recognized as stale and recovered instead of stranding the truck.
        storage.update("freight_truck_profiles", profile_id, {"booking_claim_at": now_iso(), "updated_at": now_iso()})
    previous_availability = str(profile.get("availability_status") or "available")
    try:
        # Atomic reservation: the compare-and-set claim closes the read-then-write
        # race - a second concurrent booking of the same load loses the claim.
        if not storage.claim_status_not("freight_loads", load["id"], "booked", "booked"):
            raise ValueError("Load is already booked")
        if profile_id:
            # Re-verify the truck window after claiming: a booking for the same
            # truck may have landed between the readiness check and the claim.
            conflict = _truck_window_conflict(profile_id, load, storage)
            if conflict:
                storage.update("freight_loads", load["id"], {"status": load.get("status") or "negotiating", "updated_at": now_iso()})
                raise ValueError("Booking is not ready: truck availability (" + conflict + ")")
        stamp = now_iso()
        booking = storage.update("freight_bookings", booking_id, {"status": "booked", "updated_at": stamp})
        if profile_id:
            # The booked load's occupancy window (checked in truck_availability)
            # is the record of the commitment - the profile's availability
            # returns to its prior state so future non-overlapping loads are
            # not blocked, and the mutex is released.
            storage.update("freight_truck_profiles", profile_id, {
                "availability_status": previous_availability, "booking_claim_at": None, "updated_at": stamp})
    except Exception:
        if locked:
            current = storage.get("freight_truck_profiles", profile_id) or {}
            if current.get("availability_status") == "booking":
                storage.update("freight_truck_profiles", profile_id, {
                    "availability_status": previous_availability, "booking_claim_at": None, "updated_at": now_iso()})
        raise
    thread = storage.get("freight_threads", booking["thread_id"])
    if thread:
        _set_stage(thread, load, "booked", storage)
    return booking


# A broker describing a price as "quoted" while asking another question has not
# made a clean new offer. This check runs after model interpretation as well.
MIXED_QUOTED_RATE = re.compile(r"\bquoted\s+\$?\d[\d,]*(?:\.\d+)?\b", re.I)
RATE_CUE = re.compile(r"(?:\brate(?:\s+is)?|\ball[\s-]*in|\boffer(?:ing)?|\bpay(?:ing)?|\b(?:can|could|would)\s+(?:you\s+|we\s+)?(?:do|meet(?:\s+at)?)|[?&]?\bhow\s+about|\bwhat\s+about|\bmeet\s+(?:you\s+)?at|\bcan\s+do|\bget\s+you|\bsqueeze\s+it\s+to|\b(?:best|max(?:imum)?)(?:\s+is)?|\bat\b)\s*[:=\-]?\s*$", re.I)
TIME_CUE = re.compile(r"\b(?:pickup|pick\s*up|delivery|deliver|appointment|appt|eta|tonight|tomorrow)\b.{0,24}\b(?:at|by)\s*$", re.I)
IDENTIFIER_CUE = re.compile(r"\b(?:mc|dot|reference|ref|load\s*(?:#|number|id)|po\s*(?:#|number))\s*[:#-]?\s*$", re.I)
CLOSED_REPLY = re.compile(r"\b(?:load\s+(?:is\s+)?covered|already\s+booked|no\s+longer\s+available|factoring\s+(?:was\s+)?denied|never\s+mind\s*[.!]\s*(?:factoring\s+(?:was\s+)?denied|sorry)|we(?:'ll|\s+will)\s+pass)\b", re.I)
REQUIRED_EQUIPMENT = re.compile(r"\b(?:need|requires?|must\s+(?:be|have)|has\s+to\s+be)\s+(?:a\s+|an\s+|\d+\s*(?:ft|foot)\s+)?(dry\s*van|reefer|flatbed|step\s*deck|power\s*only)\b|\b(dry\s*van|reefer|flatbed|step\s*deck|power\s*only)\s+(?:only|required|needed)\b", re.I)
DESTINATION_MENTION = re.compile(r"\b(?:deliver(?:y)?\s+(?:to|in)|going\s+to|to)\s+([A-Za-z][A-Za-z .'-]{1,35}?),\s*([A-Z]{2})\b", re.I)


class SensitiveOutboundConfirmationRequired(ValueError):
    """Raised before an initial email containing sensitive data can be sent."""

    def __init__(self, load_id: str, fields: list[str]):
        self.load_id = load_id
        self.fields = fields
        super().__init__("Confirm sharing: " + ", ".join(fields))


def sensitive_outbound_fields(subject: str, body: str) -> list[str]:
    """Return human-readable categories requiring explicit first-send confirmation."""
    text = f"{subject}\n{body}"
    return [label for label, pattern in SENSITIVE_OUTBOUND.items() if pattern.search(text)]


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _money(value: float | None) -> str:
    if value is None:
        return ""
    return f"${value:,.0f}"


def route_label(city: str, state: str) -> str:
    return ", ".join(part for part in (str(city or "").strip(), str(state or "").strip().upper()) if part)


def load_economics(load: dict[str, Any], offer: float | None = None) -> dict[str, float | None]:
    rate = _number(offer if offer is not None else load.get("current_offer"))
    if rate is None:
        rate = _number(load.get("posted_rate"))
    loaded = _number(load.get("loaded_miles"))
    deadhead = _number(load.get("deadhead_miles"))
    all_in = loaded + deadhead if loaded is not None and deadhead is not None else None
    return {
        "rate": rate,
        "loaded_miles": loaded,
        "deadhead_miles": deadhead,
        "all_in_miles": all_in,
        "loaded_rpm": round(rate / loaded, 2) if rate is not None and loaded else None,
        "all_in_rpm": round(rate / all_in, 2) if rate is not None and all_in else None,
    }


def mission_price_comparison(load: dict[str, Any], mission: dict[str, Any]) -> dict[str, Any]:
    """Compare an offer with every configured mission floor, without guessing miles."""
    rate = load_economics(load)["rate"]
    loaded = _number(load.get("loaded_miles")) if load.get("loaded_miles_verified") else None
    deadhead = _number(load.get("deadhead_miles")) if load.get("deadhead_miles_verified") else None
    total_floor = _number(mission.get("floor_total"))
    loaded_rpm_floor = _number(mission.get("floor_loaded_rpm"))
    all_in_rpm_floor = _number(mission.get("floor_all_in_rpm"))
    missing: list[str] = []
    floors = [total_floor] if total_floor is not None else []
    if loaded_rpm_floor is not None:
        if loaded:
            floors.append(loaded * loaded_rpm_floor)
        else:
            missing.append("loaded miles")
    if all_in_rpm_floor is not None:
        if not loaded:
            if "loaded miles" not in missing:
                missing.append("loaded miles")
        elif deadhead is None:
            missing.append("deadhead miles")
        else:
            floors.append((loaded + deadhead) * all_in_rpm_floor)
    minimum_total = max(floors) if floors and not missing else None
    return {
        "rate": rate,
        "minimum_total": minimum_total,
        "known_floor": max(floors) if floors else None,
        "missing": missing,
        "difference": rate - minimum_total if rate is not None and minimum_total is not None else None,
    }


def parse_destinations(
    labels: list[str],
    kinds: list[str],
    radii: list[str],
) -> list[dict[str, Any]]:
    """One source of truth per destination: the label carries city and state
    ("Dallas, TX"); no parallel state input to disagree with it."""
    result: list[dict[str, Any]] = []
    allowed = {"city", "state", "region", "anywhere"}
    for index, raw_label in enumerate(labels):
        label = str(raw_label or "").strip()
        if not label:
            continue
        kind = str(kinds[index] if index < len(kinds) else "city").lower()
        if kind not in allowed:
            kind = "city"
        radius = int(_number(radii[index] if index < len(radii) else 0) or 0)
        result.append({"label": label, "kind": kind, "radius_miles": max(0, radius)})
    return result


def auto_lane_issue(mission: dict) -> str | None:
    """Auto mode needs one identifiable origin/destination lane and a price goal."""
    if not re.fullmatch(r"[A-Za-z]{2}", str(mission.get("origin_state") or "")):
        return "Set the origin state for this automatic mission."
    destinations = mission.get("destinations") or []
    if len(destinations) != 1:
        return "Use one destination per automatic mission; create separate missions for different lanes."
    destination = destinations[0]
    kind = str(destination.get("kind") or "").lower()
    label = str(destination.get("label") or "").strip()
    if kind == "state":
        valid = bool(re.fullmatch(r"[A-Za-z]{2}", label))
    elif kind == "city":
        valid = bool(re.fullmatch(r"[^,]+,\s*[A-Za-z]{2}", label)) and not _number(destination.get("radius_miles"))
    else:
        valid = False
    if not valid:
        return "Choose one exact city and state, or one two-letter destination state, for automatic replies."
    if not any((_number(mission.get(key)) or 0) > 0 for key in ("target_total", "target_all_in_rpm")):
        return "Set a target total or target all-in RPM for this lane before enabling automatic replies."
    return None


def seed_freight_template(storage=None) -> None:
    storage = storage or store
    existing = storage.list("email_templates", {"vertical": "freight"}, order="", limit=100)
    current = next((row for row in existing if row.get("angle_tag") == "freight_first_touch"), None)
    subject = "Truck available — {{equipment}}"
    body = (
        "Hi,\n\nI am interested in the load you posted. "
        "I have a {{equipment}} available near {{truck_location}}.\n\n"
        "Please send the pickup, delivery, miles, weight, and rate.\n\n{{signature}}"
    )
    if current:
        # Migrate the original seeded template only when it is still untouched;
        # custom templates remain intact and are covered by the send gate.
        legacy_body = (
            "Hi,\n\nI am interested in the load you posted. "
            "I have a {{equipment}} available near {{truck_location}}.\n\n"
            "MC {{mc_number}}\nPlease send the pickup, delivery, miles, weight, and rate.\n\n{{signature}}"
        )
        if current.get("body") == legacy_body:
            storage.update("email_templates", current["id"], {"body": body, "updated_at": now_iso()})
        return
    stamp = now_iso()
    storage.insert("email_templates", {
        "id": new_id(),
        "name": "Freight — Load inquiry",
        "subject": subject,
        "body": body,
        "type": "plain",
        "angle_tag": "freight_first_touch",
        "active": True,
        "vertical": "freight",
        "created_at": stamp,
        "updated_at": stamp,
    })


def seed_freight_settings(storage=None) -> None:
    storage = storage or store
    freight_templates = storage.list("email_templates", {"vertical": "freight"}, order="created_at asc", limit=100)
    default_template = next((row for row in freight_templates if row.get("angle_tag") == "freight_first_touch"), None)
    current = storage.get("freight_settings", 1)
    stamp = now_iso()
    cfg = storage.get("settings", 1) or {}
    senders = [s for s in list_gmail_senders(active_only=True, storage=storage, cfg=cfg) if s.get("provider", "gmail") == "gmail"]
    default_sender = str(senders[0]["id"]) if senders else "1"
    template_id = default_template.get("id") if default_template else None
    if not current:
        storage.insert("freight_settings", {
            "id": 1, "sender_name": "Freight Dispatch", "email_signature": "Freight Dispatch",
            "reply_to": "", "default_sender_account": default_sender,
            "default_template_id": template_id,
            "created_at": stamp, "updated_at": stamp,
        })
    else:
        updates = {}
        if not current.get("default_template_id") and template_id:
            updates["default_template_id"] = template_id
        if (not current.get("default_sender_account") or current.get("default_sender_account") == "1") and senders:
            updates["default_sender_account"] = default_sender
        if updates:
            updates["updated_at"] = stamp
            storage.update("freight_settings", 1, updates)


def freight_config(storage=None) -> dict[str, Any]:
    storage = storage or store
    base = storage.get("settings", 1) or {}
    freight = storage.get("freight_settings", 1) or {}
    return {
        **base,
        "sender_name": freight.get("sender_name") or "Freight Dispatch",
        "email_signature": freight.get("email_signature") or "Freight Dispatch",
        "signature": freight.get("email_signature") or "Freight Dispatch",
        "reply_to": freight.get("reply_to") or base.get("reply_to") or "",
    }


def freight_sender_context(cfg: dict[str, Any], sender: dict[str, Any]) -> dict[str, Any]:
    context = sender_context(cfg, sender)
    signature = cfg.get("email_signature") or "Freight Dispatch"
    context.update({
        "sender_name": cfg.get("sender_name") or "Freight Dispatch",
        "email_signature": signature,
        "signature": signature,
        "reply_to": cfg.get("reply_to") or sender.get("email") or "",
    })
    return context


def template_context(load: dict[str, Any], mission: dict[str, Any], profile: dict[str, Any], sender: dict[str, Any]) -> dict[str, Any]:
    economics = load_economics(load)
    return {
        "email": load.get("broker_email") or "",
        "origin": route_label(load.get("origin_city", ""), load.get("origin_state", "")),
        "destination": route_label(load.get("destination_city", ""), load.get("destination_state", "")),
        "pickup_date": load.get("pickup_date") or "the requested date",
        "equipment": load.get("equipment_type") or mission.get("equipment_type") or profile.get("equipment_type") or "truck",
        "truck_location": route_label(profile.get("current_city", ""), profile.get("current_state", "")) or route_label(mission.get("origin_city", ""), mission.get("origin_state", "")) or "your area",
        "mc_number": profile.get("mc_number") or "",
        "dot_number": profile.get("dot_number") or "",
        "broker_company": load.get("broker_company") or "",
        "dat_reference": load.get("dat_reference") or "",
        "loaded_miles": economics.get("loaded_miles") or "",
        "deadhead_miles": economics.get("deadhead_miles") or "",
        "posted_rate": _money(economics.get("rate")),
        "all_in_rpm": f"{economics['all_in_rpm']:.2f}" if economics.get("all_in_rpm") is not None else "",
        **sender,
    }


def _load_bundle(load_id: str, storage=None) -> tuple[dict, dict, dict, dict, dict]:
    storage = storage or store
    load = storage.get("freight_loads", load_id)
    if not load:
        raise ValueError("Load not found")
    mission_id = load.get("mission_id")
    mission = storage.get("freight_missions", mission_id) if mission_id else {}
    mission = mission or {}
    profile_id = load.get("truck_profile_id") or mission.get("truck_profile_id")
    profile = storage.get("freight_truck_profiles", profile_id) if profile_id else {}
    template_id = load.get("template_id")
    template = storage.get("email_templates", template_id) if template_id else {}
    profile, template = profile or {}, template or {}
    cfg = freight_config(storage)
    return load, mission, profile, template, cfg


def prepare_first_touch(load_id: str, storage=None) -> dict[str, Any]:
    storage = storage or store
    load, mission, profile, template, cfg = _load_bundle(load_id, storage)
    if not valid_email(load.get("broker_email", "")):
        raise ValueError("A valid broker email is required")
    sender = get_gmail_sender(str(load.get("sender_account") or "1"), storage=storage, cfg=cfg)
    if not sender or sender.get("provider", "gmail") != "gmail":
        raise ValueError("Choose an active Gmail sender")
    sender_cfg = freight_sender_context(cfg, sender)
    context = template_context(load, mission, profile, sender_cfg)
    raw_subject = load.get("subject") if load.get("subject") and load.get("subject") != "Load inquiry" else template.get("subject") or "Load inquiry"
    subject, subject_missing = render_template(raw_subject, context, sender_cfg)
    body, body_missing = render_template(template.get("body") or "", context, sender_cfg)
    missing = sorted(set(subject_missing + body_missing))
    if missing:
        raise ValueError(f"Complete the profile/load fields required by the template: {', '.join(missing)}")
    return {
        "load": load, "mission": mission, "profile": profile, "template": template,
        "cfg": cfg, "sender": sender, "sender_cfg": sender_cfg, "subject": subject,
        "body": body, "sensitive_fields": sensitive_outbound_fields(subject, body),
    }


def send_first_touch(load_id: str, storage=None, *, confirm_sensitive: bool = False) -> dict[str, Any]:
    storage = storage or store
    prepared = prepare_first_touch(load_id, storage)
    load = prepared["load"]
    cfg = prepared["cfg"]
    template = prepared["template"]
    sender = prepared["sender"]
    sender_cfg = prepared["sender_cfg"]
    subject = prepared["subject"]
    body = prepared["body"]
    sensitive_fields = prepared["sensitive_fields"]
    if sensitive_fields and not confirm_sensitive:
        raise SensitiveOutboundConfirmationRequired(load_id, sensitive_fields)
    message_id = make_msgid(domain=(sender.get("email") or "gmail.com").split("@")[-1])
    provider = get_provider("gmail", cfg, str(sender["id"]), sender)
    result = provider.send(
        to=load["broker_email"], subject=subject, body=body, content_type=template.get("type") or "plain",
        from_address=sender.get("email") or "", from_name=sender_cfg.get("sender_name") or "Dispatch",
        reply_to=sender_cfg.get("reply_to") or sender.get("email") or "", headers={"Message-ID": message_id},
    )
    if not result.ok:
        storage.update("freight_loads", load_id, {"status": "failed", "updated_at": now_iso()})
        raise ValueError(result.error or "Email delivery failed")
    stamp = now_iso()
    thread_id = new_id()
    root_id = result.provider_id or message_id
    storage.insert("freight_threads", {
        "id": thread_id, "load_id": load_id, "sender_account": str(sender["id"]),
        "recipient_email": load["broker_email"], "subject": subject, "root_message_id": root_id,
        "last_message_id": root_id, "last_imap_uid": None, "state": "waiting",
        "last_activity_at": stamp, "created_at": stamp, "updated_at": stamp,
    })
    storage.insert("freight_messages", {
        "id": new_id(), "thread_id": thread_id, "direction": "out", "provider_message_id": root_id,
        "from_email": sender.get("email") or "", "to_email": load["broker_email"], "subject": subject,
        "body_text": body, "classification": {"kind": "first_touch"}, "status": "sent", "created_at": stamp,
    })
    storage.update("freight_loads", load_id, {"subject": subject, "status": "waiting", "updated_at": stamp})
    return {"thread_id": thread_id, "message_id": message_id, "provider_id": result.provider_id}


def extract_numeric_facts(body: str) -> list[dict[str, Any]]:
    """Keep the original span and its meaning separate; unknown numbers never authorize a send."""
    text = strip_quoted_reply(body)
    phones = [match.span() for match in PHONE_NUMBER.finditer(text)]
    dates = [match.span() for match in DATE_NUMBER.finditer(text)]
    facts: list[dict[str, Any]] = []
    for match in NUMBER_CANDIDATE.finditer(text):
        amount = match.group("amount")
        value = _number(amount)
        if value is None:
            continue
        start, end = match.span()
        line_start = text.rfind("\n", 0, start) + 1
        next_newline = text.find("\n", end)
        line_end = len(text) if next_newline < 0 else next_newline
        before = re.split(r"[.!?;]", text[max(line_start, start - 42):start])[-1]
        after = text[end:min(line_end, end + 32)]
        sentence = text[line_start:line_end].strip()
        rate_cue = bool(RATE_CUE.search(before))
        weight_suffix = bool(re.match(r"\s*(?:lbs?|pounds?)\b", after, re.I))
        miles_suffix = bool(re.match(r"\s*(?:miles?|mi)\b", after, re.I))
        loaded_miles_suffix = bool(re.match(r"\s*loaded\s+(?:miles?|mi)\b", after, re.I))
        deadhead_cue = bool(re.search(r"\bdeadhead(?:\s+miles?)?\s*[:=-]?\s*$", before, re.I))
        deadhead_suffix = bool(re.match(r"\s*deadhead\s+(?:miles?|mi)\b", after, re.I))
        unit = "unknown"
        if any(start < date_end and end > date_start for date_start, date_end in dates):
            unit = "date"
        elif any(start < phone_end and end > phone_start for phone_start, phone_end in phones):
            unit = "phone"
        elif re.match(r"\s*(?:am|pm)\b", after, re.I) or (TIME_CUE.search(before) and value <= 2359):
            unit = "time"
        elif weight_suffix:
            unit = "weight"
        elif miles_suffix or loaded_miles_suffix or deadhead_suffix:
            unit = "deadhead_miles" if deadhead_cue or deadhead_suffix else "miles"
        elif re.match(r"\s*(?:/\s*(?:loaded\s+)?mi(?:le)?s?|per\s+(?:loaded\s+)?mi(?:le)?|rpm)\b", after, re.I) or re.search(r"\b(?:rpm|per\s+mile)\s*[:=-]?\s*$", before, re.I):
            unit = "rate_per_mile"
        elif IDENTIFIER_CUE.search(before):
            unit = "identifier"
        elif 100 <= value <= 100000 and (match.group("currency") or rate_cue):
            unit = "total_rate"
        elif re.search(r"\b(?:weight|gross|cargo\s+weight)\b.{0,22}$", before, re.I):
            unit = "weight"
        elif re.search(r"\b(?:loaded|deadhead|total)\s+miles?\s*[:=-]?\s*$", before, re.I):
            unit = "deadhead_miles" if deadhead_cue else "miles"
        elif deadhead_cue:
            unit = "deadhead_miles"
        elif 100 <= value <= 100000 and re.fullmatch(r"\s*\$?\s*\d[\d,.]*\s*", sentence):
            unit = "total_rate"
        facts.append({"value": value, "unit": unit, "span": [start, end], "text": text[start:end].strip(), "sentence": sentence, "context_before": before.strip(), "context_after": after.strip()})
    return facts


def extract_offer(body: str) -> float | None:
    facts = extract_numeric_facts(body)
    offers = {fact["value"] for fact in facts if fact["unit"] == "total_rate"}
    return next(iter(offers)) if len(offers) == 1 else None


def split_message_body(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    quote_match = re.search(
        r"(?:\r?\n|^)\s*(?:On\s+[\s\S]*?wrote:|---+\s*(?:Original|Forwarded)[\s\S]*?---+|From:\s+.*?\n\s*(?:Sent|To|Subject):)",
        text,
        re.I,
    )
    if quote_match:
        idx = quote_match.start()
        main = text[:idx].strip()
        quoted = text[idx:].strip()
        return main, quoted
    lines = text.splitlines()
    main_lines, quoted_lines = [], []
    in_quote = False
    for line in lines:
        if not in_quote and line.strip().startswith(">"):
            in_quote = True
        if in_quote:
            quoted_lines.append(line)
        else:
            main_lines.append(line)
    main = "\n".join(main_lines).strip()
    quoted = "\n".join(quoted_lines).strip()
    return main, quoted


def strip_quoted_reply(body: str) -> str:
    main, _ = split_message_body(body)
    return main


def format_freight_message(msg: dict) -> dict:
    body = msg.get("body_text") or ""
    clean_body, quoted_body = split_message_body(body)
    created_at_raw = msg.get("created_at") or ""
    formatted_time = created_at_raw
    if created_at_raw:
        try:
            dt = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00")).astimezone()
            formatted_time = dt.strftime("%b %d · %I:%M %p")
        except Exception:
            formatted_time = created_at_raw[:16]
    return {
        **msg,
        "clean_body": clean_body,
        "quoted_body": quoted_body,
        "formatted_time": formatted_time,
    }


def classify_reply(body: str) -> dict[str, Any]:
    text = strip_quoted_reply(body)
    protected = [kind for kind, pattern in PROTECTED_PATTERNS.items() if pattern.search(text)]
    questions: list[str] = []
    if TEAM_QUESTION.search(text): questions.append("team_status")
    if EQUIPMENT_QUESTION.search(text): questions.append("equipment_type")
    if MC_QUESTION.search(text): questions.append("mc_or_dot")
    facts = extract_numeric_facts(text)
    offers = {fact["value"] for fact in facts if fact["unit"] == "total_rate"}
    per_mile = {fact["value"] for fact in facts if fact["unit"] == "rate_per_mile"}
    unknown_amounts = [fact for fact in facts if fact["unit"] == "unknown" and 100 <= fact["value"] <= 100000]
    ambiguous_offer = len(offers) > 1 or bool(offers and per_mile) or bool(offers and unknown_amounts) or (not offers and len(per_mile) > 1)
    offer = next(iter(offers)) if len(offers) == 1 and not ambiguous_offer else None
    return {
        "kind": "protected" if protected else "ambiguous_rate" if ambiguous_offer else "rate_offer" if offer is not None else "rate_per_mile" if per_mile else "profile_question" if questions else "other",
        "protected": protected,
        "questions": questions,
        "offer": offer,
        "rate_per_mile": next(iter(per_mile)) if len(per_mile) == 1 else None,
        "ambiguous_offer": ambiguous_offer,
        "numeric_facts": facts,
    }


def _set_stage(thread: dict, load: dict, state: str, storage=None) -> None:
    storage = storage or store
    stamp = now_iso()
    storage.update("freight_threads", thread["id"], {"state": state, "updated_at": stamp})
    storage.update("freight_loads", load["id"], {"status": state, "updated_at": stamp})


def _supersede_pending_drafts(thread_id: str, storage=None, *, keep_in_reply_to: str = "") -> None:
    storage = storage or store
    for draft in storage.list("freight_drafts", {"thread_id": thread_id, "status": "pending"}, order="", limit=1000):
        # Replaying one broker message keeps the draft that already answers it.
        if keep_in_reply_to and (draft.get("in_reply_to_message_id") or "") == keep_in_reply_to:
            continue
        storage.update("freight_drafts", draft["id"], {"status": "superseded", "updated_at": now_iso()})


def set_thread_state(thread_id: str, state: str, storage=None) -> dict:
    """Human workflow transition; automatic evaluation never books a load."""
    storage = storage or store
    if state not in {"negotiating", "booked", "closed", "passed"}:
        raise ValueError("Invalid freight state")
    thread = storage.get("freight_threads", thread_id)
    if not thread:
        raise ValueError("Freight thread not found")
    load = storage.get("freight_loads", thread["load_id"])
    if not load:
        raise ValueError("Freight load not found")
    if state == "booked":
        bookings = [b for b in storage.list("freight_bookings", {"thread_id": thread_id}, order="created_at desc", limit=5)
                    if b.get("status") != "cancelled"]
        if bookings:
            mark_booked(bookings[0]["id"], storage)
            return {"thread": storage.get("freight_threads", thread_id), "load": storage.get("freight_loads", load["id"])}
        raise ValueError("No agreement is recorded for this thread; booking starts when the broker accepts a price")
    if state in {"closed", "passed"}:
        _supersede_pending_drafts(thread_id, storage)
    _set_stage(thread, load, state, storage)
    return {"thread": storage.get("freight_threads", thread_id), "load": storage.get("freight_loads", load["id"])}


def verify_load_facts(load_id: str, values: dict[str, Any], storage=None) -> dict:
    """A dispatcher confirms or updates load facts after the broker replies."""
    storage = storage or store
    load = storage.get("freight_loads", load_id)
    if not load:
        raise ValueError("Freight load not found")
    mission = storage.get("freight_missions", load.get("mission_id")) or {}
    profile = storage.get("freight_truck_profiles", load.get("truck_profile_id")) or {}

    origin_city = str(values.get("origin_city") or load.get("origin_city") or "").strip()
    origin_state = str(values.get("origin_state") or load.get("origin_state") or "").strip().upper()
    origin_unchanged = origin_city == (load.get("origin_city") or "") and origin_state == (load.get("origin_state") or "")
    city = str(values.get("destination_city") or "").strip()
    state = str(values.get("destination_state") or "").strip().upper()

    if origin_city and origin_state and len(origin_state) != 2:
        raise ValueError("Enter the broker's pickup city and two-letter state")

    destination_verified = False
    if city or state:
        if not city or len(state) != 2:
            raise ValueError("Enter the broker's destination city and two-letter state")
        destination_match = _destination_matches(mission, city, state)
        has_radius = any(_number(item.get("radius_miles")) for item in mission.get("destinations") or [])
        if destination_match is False and not has_radius:
            raise ValueError("Destination is outside this mission; choose a matching mission")
        destination_verified = True
    else:
        city = load.get("destination_city") or "Open destinations"
        state = load.get("destination_state") or ""
        destination_verified = bool(load.get("destination_verified")) and (city != "Open destinations")

    equipment = str(values.get("equipment_type") or "").strip()
    saved_equipment = str(profile.get("equipment_type") or mission.get("equipment_type") or "").strip()
    equipment_verified = False
    if equipment:
        if saved_equipment and equipment.casefold() != saved_equipment.casefold():
            raise ValueError("Broker equipment differs from the truck profile; use a matching profile or mission")
        equipment_verified = bool(values.get("equipment_confirmed")) or (bool(load.get("equipment_verified")) and equipment == (load.get("equipment_type") or ""))
    else:
        equipment = load.get("equipment_type") or saved_equipment or ""
        equipment_verified = bool(load.get("equipment_verified"))

    pickup_text = str(values.get("pickup_date") or "").strip()
    pickup_verified = False
    if pickup_text:
        try:
            pickup = datetime.fromisoformat(pickup_text).date()
            start = datetime.fromisoformat(mission["pickup_start"]).date() if mission.get("pickup_start") else None
            end = datetime.fromisoformat(mission["pickup_end"]).date() if mission.get("pickup_end") else None
        except ValueError as exc:
            raise ValueError("Enter a valid pickup date") from exc
        if (start and pickup < start) or (end and pickup > end):
            raise ValueError("Pickup date is outside this mission's window")
        pickup_verified = bool(values.get("pickup_date_confirmed")) or (bool(load.get("pickup_date_verified")) and pickup_text == (load.get("pickup_date") or ""))
    else:
        pickup_text = load.get("pickup_date") or ""
        pickup_verified = bool(load.get("pickup_date_verified"))

    schedule_confirmed = bool(values.get("schedule_confirmed")) or bool(load.get("schedule_verified"))

    loaded = _number(values.get("loaded_miles"))
    deadhead = _number(values.get("deadhead_miles"))
    weight = _number(values.get("weight_lbs"))

    if loaded is not None and loaded <= 0:
        raise ValueError("Loaded miles must be positive")
    if deadhead is not None and deadhead < 0:
        raise ValueError("Deadhead miles cannot be negative")
    if weight is not None and weight <= 0:
        raise ValueError("Weight must be positive")
    max_weight = _number(profile.get("max_weight_lbs")) or _number(mission.get("max_weight_lbs"))
    if max_weight and weight and weight > max_weight:
        raise ValueError("Broker load weight exceeds this truck's limit")

    final_loaded = loaded if loaded is not None else load.get("loaded_miles")
    final_deadhead = deadhead if deadhead is not None else load.get("deadhead_miles")
    final_weight = weight if weight is not None else load.get("weight_lbs")

    updates = {
        "origin_city": origin_city,
        "origin_state": origin_state,
        "origin_verified": bool(origin_city and origin_state) and (bool(values.get("origin_confirmed")) or (origin_unchanged and bool(load.get("origin_verified")))),
        "destination_city": city,
        "destination_state": state,
        "destination_verified": destination_verified,
        "equipment_type": equipment,
        "equipment_verified": equipment_verified,
        "pickup_date": pickup_text or None,
        "pickup_date_verified": pickup_verified,
        "schedule_verified": schedule_confirmed,
        "loaded_miles": final_loaded,
        "loaded_miles_verified": final_loaded is not None,
        "deadhead_miles": final_deadhead,
        "deadhead_miles_verified": final_deadhead is not None,
        "weight_lbs": final_weight,
        "updated_at": now_iso(),
    }
    return storage.update("freight_loads", load_id, updates)


def add_load_stop(load_id: str, values: dict[str, Any], storage=None) -> dict:
    """Dispatcher-entered stop. Manual entry is already confirmed by a human."""
    storage = storage or store
    load = storage.get("freight_loads", load_id)
    if not load:
        raise ValueError("Freight load not found")
    kind = str(values.get("kind") or "").strip().lower()
    if kind not in {"pickup", "delivery"}:
        raise ValueError("Choose pickup or delivery")
    city = str(values.get("city") or "").strip()
    state = str(values.get("state") or "").strip().upper()
    if not city or len(state) != 2:
        raise ValueError("Enter the stop city and two-letter state")
    appointment = str(values.get("appointment") or "").strip()
    if bool(values.get("fcfs")) or appointment.upper() == "FCFS":
        appointment = "FCFS"
    active = [s for s in storage.list("freight_load_stops", {"load_id": load_id}, order="seq asc", limit=50) if not s.get("removed_at")]
    stamp = now_iso()
    return storage.insert("freight_load_stops", {
        "id": new_id(),
        "load_id": load_id,
        "seq": max([int(s.get("seq") or 0) for s in active], default=0) + 1,
        "kind": kind,
        "facility_name": str(values.get("facility_name") or "").strip(),
        "city": city,
        "state": state,
        "appointment": appointment or None,
        "verified": True,
        "appointment_verified": bool(appointment),
        "evidence": "Entered manually by dispatch",
        "source_message_id": "manual",
        "created_at": stamp,
        "updated_at": stamp,
    })


def verify_load_stop(stop_id: str, values: dict[str, Any], storage=None) -> dict:
    """A dispatcher confirms one stop's details and appointment time or window."""
    storage = storage or store
    stop = storage.get("freight_load_stops", stop_id)
    if not stop:
        raise ValueError("Freight stop not found")
    if stop.get("removed_at"):
        raise ValueError("This stop was dropped from the broker's latest route")
    city = str(values.get("city") or stop.get("city") or "").strip()
    state = str(values.get("state") or stop.get("state") or "").strip().upper()
    if not city or len(state) != 2:
        raise ValueError("Enter the stop city and two-letter state")
    appointment = str(values.get("appointment") or stop.get("appointment") or "").strip()
    fcfs = bool(values.get("fcfs")) or appointment.upper() == "FCFS"
    if fcfs:
        appointment = "FCFS"
    if not appointment:
        raise ValueError("Enter the appointment time or window for this stop, or mark it FCFS")
    return storage.update("freight_load_stops", stop_id, {
        "city": city,
        "state": state,
        "facility_name": str(values.get("facility_name") or stop.get("facility_name") or "").strip(),
        "appointment": appointment,
        "verified": True,
        "appointment_verified": True,
        "updated_at": now_iso(),
    })


def _sync_load_stops(load: dict, classification: dict, message_id: str, storage) -> None:
    """Reconcile evidence-backed stops from the latest broker message, in route order.

    Stops are matched by occurrence: the broker's list defines the full route,
    so repeat stops in the same city each get their own row, position sets seq,
    and a stop that disappears from the route is marked removed with a review
    alert instead of silently lingering or vanishing. A broker update that
    changes a stop's facility or appointment clears that stop's verification;
    unchanged stops keep theirs, and a pure reorder never clears it.
    """
    if classification.get("source") != "gemini":
        return
    stops = classification.get("stops") or []
    if not stops:
        return
    existing = storage.list("freight_load_stops", {"load_id": load["id"]}, order="seq asc", limit=50)
    active = [row for row in existing if not row.get("removed_at")]
    stamp = now_iso()
    matched_ids: set[str] = set()
    for position, stop in enumerate(stops, start=1):
        match = next((row for row in active
                      if row["id"] not in matched_ids
                      and row.get("kind") == stop["kind"]
                      and (row.get("city") or "").casefold() == stop["city"].casefold()
                      and (row.get("state") or "").upper() == stop["state"]), None)
        if match:
            matched_ids.add(match["id"])
            updates: dict[str, Any] = {"seq": position, "evidence": stop["evidence"], "source_message_id": message_id, "updated_at": stamp}
            changed = False
            if stop.get("facility") and stop["facility"] != (match.get("facility_name") or ""):
                updates["facility_name"] = stop["facility"]
                changed = True
            if stop.get("appointment") and stop["appointment"] != (match.get("appointment") or ""):
                updates["appointment"] = stop["appointment"]
                changed = True
            if changed:
                updates["verified"] = False
                updates["appointment_verified"] = False
            storage.update("freight_load_stops", match["id"], updates)
        else:
            storage.insert("freight_load_stops", {
                "id": new_id(),
                "load_id": load["id"],
                "seq": position,
                "kind": stop["kind"],
                "facility_name": stop.get("facility") or "",
                "city": stop["city"],
                "state": stop["state"],
                "appointment": stop.get("appointment") or None,
                "verified": False,
                "appointment_verified": False,
                "evidence": stop["evidence"],
                "source_message_id": message_id,
                "created_at": stamp,
                "updated_at": stamp,
            })
    # Removals only on a complete route restatement: a partial update ("the
    # second pickup moved to 3pm") mentions one stop and must not wipe the rest.
    if (classification.get("route_scope") or "partial") != "complete":
        return
    removed = [row for row in active if row["id"] not in matched_ids]
    for row in removed:
        storage.update("freight_load_stops", row["id"], {"removed_at": stamp, "updated_at": stamp})
    if removed:
        threads = storage.list("freight_threads", {"load_id": load["id"]}, order="", limit=1)
        if threads:
            labels = ", ".join(f"{row.get('kind')} {row.get('city')}{', ' + row.get('state') if row.get('state') else ''}" for row in removed)
            _create_alert(threads[0]["id"], "stop_removed",
                          f"The broker's latest update dropped stop(s): {labels}. Confirm the new route before booking.", storage)
            # The route the agreement and rate-con review were made against no
            # longer exists: unbind the approvals. The open stop_removed alert
            # is a durable booking gate (checked in _booking_readiness_blockers).
            for booking in storage.list("freight_bookings", {"thread_id": threads[0]["id"]}, order="", limit=10):
                if booking.get("status") in {"agreed", "rate_con_review"}:
                    storage.update("freight_bookings", booking["id"], {
                        "rate_con_reviewed": False, "rate_con_review_version": "",
                        "driver_handoff_approved": False, "driver_handoff_version": "",
                        "updated_at": stamp})

def _record_negotiation_event(thread_id: str, source_id: str, event_type: str, amount: float, details: dict | None = None, storage=None) -> None:
    storage = storage or store
    if storage.list("freight_negotiation_events", {"event_type": event_type, "source_id": source_id}, order="", limit=1):
        return
    storage.insert("freight_negotiation_events", {
        "id": new_id(), "thread_id": thread_id, "source_id": source_id,
        "event_type": event_type, "amount": amount, "unit": "total_rate",
        "details": details or {}, "created_at": now_iso(),
    })


def _counter_count(thread_id: str, load: dict, storage=None) -> int:
    """Backfill sent legacy counter drafts, then count durable events rather than all replies."""
    storage = storage or store
    for draft in storage.list("freight_drafts", {"thread_id": thread_id, "status": "sent"}, order="created_at asc", limit=1000):
        if not str(draft.get("reason") or "").startswith("counter_"):
            continue
        amount = extract_offer(draft.get("body_text") or "") or _number((draft.get("policy_snapshot") or {}).get("counter"))
        if amount is not None:
            _record_negotiation_event(thread_id, draft["id"], "counter", amount, {"legacy_backfill": True}, storage)
    events = storage.list("freight_negotiation_events", {"thread_id": thread_id, "event_type": "counter"}, order="", limit=10000)
    count = sum(1 for event in events if not (event.get("details") or {}).get("restate"))
    if int(load.get("current_round") or 0) != count:
        storage.update("freight_loads", load["id"], {"current_round": count, "updated_at": now_iso()})
    return count


def _destination_from_text(text: str) -> tuple[str, str] | None:
    matches = list(DESTINATION_MENTION.finditer(text))
    return (matches[-1].group(1).strip(), matches[-1].group(2).upper()) if matches else None


def _destination_matches(mission: dict, city: str, state: str) -> bool | None:
    destinations = mission.get("destinations") or []
    if not destinations:
        return None
    uncertain = False
    for destination in destinations:
        kind = str(destination.get("kind") or "city").lower()
        label = str(destination.get("label") or "").strip()
        if kind in {"anywhere", "region"}:
            uncertain = True
        elif kind == "state":
            if len(label) != 2:
                uncertain = True
            elif label.upper() == state:
                return True
        elif kind == "city":
            match = re.fullmatch(r"\s*([^,]+),\s*([A-Za-z]{2})\s*", label)
            if not match:
                uncertain = True
            elif match.group(1).strip().casefold() == city.casefold() and match.group(2).upper() == state:
                return True
            elif _number(destination.get("radius_miles")):
                uncertain = True  # Resolve a radius with geocoding before ruling a city out.
    return None if uncertain else False


def _pickup_date_from_text(text: str, mission: dict):
    pickup = re.search(r"\b(?:pickup|pick\s*up)\b.{0,20}?\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b", text, re.I)
    if not pickup:
        return None
    raw = pickup.group(1)
    try:
        if "-" in raw:
            return datetime.fromisoformat(raw).date()
        parts = [int(part) for part in raw.split("/")]
        anchor = mission.get("pickup_start") or mission.get("pickup_end")
        year = parts[2] if len(parts) == 3 else datetime.fromisoformat(anchor).year if anchor else datetime.now(timezone.utc).year
        return datetime(2000 + year if year < 100 else year, parts[0], parts[1]).date()
    except (ValueError, TypeError):
        return None


def _enrich_load_facts(load: dict, mission: dict, classification: dict, text: str, storage=None, *, preserve_verified: bool = False) -> dict:
    storage = storage or store
    updates: dict[str, Any] = {}
    model_origin = classification.get("origin")
    if model_origin and not (preserve_verified and load.get("origin_verified")):
        updates["origin_city"] = model_origin["city"]
        updates["origin_state"] = model_origin["state"].upper()
        updates["origin_verified"] = True
    model_equipment = classification.get("equipment")
    if model_equipment and not (preserve_verified and load.get("equipment_verified")):
        updates["equipment_type"] = model_equipment["type"]
        updates["equipment_verified"] = True
    if (classification.get("pickup_date") or re.search(r"\b(?:pickup|pick\s*up|\bpu\b|delivery|deliver|appt|appointment|eta)\b", text, re.I)) and not (preserve_verified and load.get("schedule_verified")):
        updates["schedule_verified"] = False
    weights = [fact["value"] for fact in classification["numeric_facts"] if fact["unit"] == "weight"]
    if len(set(weights)) == 1 and not (preserve_verified and load.get("weight_lbs") is not None):
        updates["weight_lbs"] = weights[0]
    miles = [fact["value"] for fact in classification["numeric_facts"] if fact["unit"] == "miles"]
    if len(set(miles)) == 1 and not (preserve_verified and load.get("loaded_miles_verified")):
        updates["loaded_miles"] = miles[0]
        updates["loaded_miles_verified"] = True
    deadhead = [fact["value"] for fact in classification["numeric_facts"] if fact["unit"] == "deadhead_miles"]
    if len(set(deadhead)) == 1 and not (preserve_verified and load.get("deadhead_miles_verified")):
        updates["deadhead_miles"] = deadhead[0]
        updates["deadhead_miles_verified"] = True
    model_destination = classification.get("destination")
    destination = (model_destination["city"], model_destination["state"].upper()) if model_destination else (None if classification.get("source") == "gemini" else _destination_from_text(text))
    if destination and not (preserve_verified and load.get("destination_verified")):
        updates["destination_city"], updates["destination_state"] = destination
        updates["destination_verified"] = _destination_matches(mission, *destination) is True
    model_pickup = classification.get("pickup_date")
    try:
        pickup_date = datetime.fromisoformat(model_pickup["value"]).date() if model_pickup else (None if classification.get("source") == "gemini" else _pickup_date_from_text(text, mission))
    except ValueError:
        pickup_date = None
    if pickup_date and not (preserve_verified and load.get("pickup_date_verified")):
        updates["pickup_date"] = pickup_date.isoformat()
        try:
            start = datetime.fromisoformat(mission["pickup_start"]).date() if mission.get("pickup_start") else None
            end = datetime.fromisoformat(mission["pickup_end"]).date() if mission.get("pickup_end") else None
            updates["pickup_date_verified"] = (not start or pickup_date >= start) and (not end or pickup_date <= end)
        except (ValueError, TypeError):
            updates["pickup_date_verified"] = False
    if updates:
        updates["updated_at"] = now_iso()
        storage.update("freight_loads", load["id"], updates)
    return {**load, **updates}


def _mismatch_reasons(text: str, load: dict, mission: dict, profile: dict, classification: dict | None = None) -> list[str]:
    reasons: list[str] = []
    equipment = str(profile.get("equipment_type") or mission.get("equipment_type") or "").lower()
    model_origin = (classification or {}).get("origin")
    if model_origin and mission.get("origin_city") and mission.get("origin_state"):
        if (model_origin["city"].casefold(), model_origin["state"].upper()) != (str(mission["origin_city"]).casefold(), str(mission["origin_state"]).upper()):
            reasons.append("Broker pickup location differs from this mission.")
    model_equipment = (classification or {}).get("equipment")
    if model_equipment and equipment and model_equipment["type"].casefold() != equipment:
        reasons.append(f"Broker load needs {model_equipment['type']}; profile has {equipment}.")
    required = REQUIRED_EQUIPMENT.search(text) if (classification or {}).get("source") != "gemini" else None
    required_type = str((classification or {}).get("required_equipment") or "").lower().strip()
    if not required_type and required:
        required_type = (required.group(1) or required.group(2)).lower().replace("  ", " ")
    if required_type:
        if required_type not in equipment:
            reasons.append(f"Broker requires {required_type}; profile has {equipment or 'no equipment set'}.")
    if (classification or {}).get("source") != "gemini" and equipment and re.search(rf"\b{re.escape(equipment)}s?\b.{0,25}\b(?:will not work|won't work|cannot use|can't use|not accepted)\b", text, re.I):
        reasons.append(f"Broker cannot use the saved {equipment}.")
    if (classification or {}).get("source") != "gemini" and re.search(r"\b(?:true\s+team|team\s+truck)\s+(?:is\s+)?(?:required|only|needed)\b", text, re.I) and str(profile.get("team_status") or "").lower() not in {"team", "true team", "yes"}:
        reasons.append("Broker requires a true team, but the profile is not a true team.")
    max_weight = _number(profile.get("max_weight_lbs")) or _number(mission.get("max_weight_lbs"))
    weight = _number(load.get("weight_lbs"))
    if max_weight and weight and weight > max_weight:
        reasons.append(f"Load weight {weight:,.0f} lbs exceeds the truck limit {max_weight:,.0f} lbs.")
    model_destination = (classification or {}).get("destination")
    destination = (model_destination["city"], model_destination["state"].upper()) if model_destination else (None if (classification or {}).get("source") == "gemini" else _destination_from_text(text))
    if destination and _destination_matches(mission, *destination) is False:
        reasons.append(f"Destination {destination[0]}, {destination[1]} is outside this mission's selected destinations.")
    model_pickup = (classification or {}).get("pickup_date")
    try:
        pickup_date = datetime.fromisoformat(model_pickup["value"]).date() if model_pickup else (None if (classification or {}).get("source") == "gemini" else _pickup_date_from_text(text, mission))
    except ValueError:
        pickup_date = None
    if pickup_date and (mission.get("pickup_start") or mission.get("pickup_end")):
        try:
            start = datetime.fromisoformat(mission["pickup_start"]).date() if mission.get("pickup_start") else None
            end = datetime.fromisoformat(mission["pickup_end"]).date() if mission.get("pickup_end") else None
            if (start and pickup_date < start) or (end and pickup_date > end):
                reasons.append(f"Pickup date {pickup_date.isoformat()} is outside the mission pickup window.")
        except (ValueError, TypeError):
            reasons.append("Broker pickup date needs review.")
    return reasons


def _required_floor(load: dict, mission: dict) -> float | None:
    candidates = [_number(mission.get("floor_total"))]
    loaded = _number(load.get("loaded_miles")) if load.get("loaded_miles_verified") else None
    deadhead = _number(load.get("deadhead_miles")) if load.get("deadhead_miles_verified") else None
    all_in = loaded + deadhead if loaded is not None and deadhead is not None else None
    if loaded and _number(mission.get("floor_loaded_rpm")):
        candidates.append(loaded * float(mission["floor_loaded_rpm"]))
    if all_in and _number(mission.get("floor_all_in_rpm")):
        candidates.append(all_in * float(mission["floor_all_in_rpm"]))
    valid = [value for value in candidates if value is not None]
    return max(valid) if valid else None


def _counter_value(load: dict, mission: dict, offer: float) -> float | None:
    configured = _number(mission.get("counter_amount")) or _number(mission.get("target_total"))
    if configured:
        return max(configured, _required_floor(load, mission) or 0, offer)
    target_rpm = _number(mission.get("target_all_in_rpm"))
    loaded = _number(load.get("loaded_miles")) if load.get("loaded_miles_verified") else None
    deadhead = _number(load.get("deadhead_miles")) if load.get("deadhead_miles_verified") else None
    all_in = loaded + deadhead if loaded is not None and deadhead is not None else None
    if target_rpm and all_in:
        # Nearest-$25 rounding must never pull a counter below an active floor.
        return max(round(target_rpm * all_in / 25) * 25, _required_floor(load, mission) or 0, offer)
    return None


def _booking_readiness_blockers(load: dict, mission: dict, profile: dict, storage=None) -> list[str]:
    """Facts that must be resolved before the carrier commits to the load."""
    blockers: list[str] = []
    stops = [s for s in ((storage or store).list("freight_load_stops", {"load_id": load["id"]}, order="seq asc", limit=50) if load.get("id") else []) if not s.get("removed_at")]
    for stop in stops:
        label = f"stop {stop.get('seq')} {stop.get('kind')} ({stop.get('city')}{', ' + stop.get('state') if stop.get('state') else ''})"
        if not stop.get("verified"):
            blockers.append(f"{label} confirmed")
        elif not stop.get("appointment") or not stop.get("appointment_verified"):
            blockers.append(f"{label} appointment")
    if load.get("id"):
        for thread in (storage or store).list("freight_threads", {"load_id": load["id"]}, order="", limit=10):
            if (storage or store).list("freight_alerts", {"thread_id": thread["id"], "kind": "stop_removed", "status": "open"}, order="", limit=1):
                blockers.append("route change review (a stop was removed; confirm the new route)")
                break
    if profile.get("id"):
        availability = truck_availability(profile, load, storage)
        if availability["status"] != "available":
            blockers.append(f"truck availability ({availability['detail'] or availability['status']})")
    if not load.get("broker_id"):
        blockers.append("verified broker identity")
    else:
        broker = (storage or store).get("freight_brokers", load["broker_id"]) or {}
        if broker.get("blocked"):
            blockers.append(f"blocked broker ({broker.get('legal_name') or broker.get('domain') or 'unknown'})")
        unconfirmed = [str(item).lower() for item in broker.get("unconfirmed_emails") or []]
        if str(load.get("broker_email") or "").strip().lower() in unconfirmed:
            blockers.append("broker identity confirmation")
        elif not unconfirmed and not broker.get("identity_confirmed", True):
            # Rows predating per-email confirmation keep the broker-wide gate.
            blockers.append("broker identity confirmation")
        if broker.get("credit_status") not in {"approved", "exempt"}:
            blockers.append("broker credit approval")
        if broker.get("setup_status") != "complete":
            blockers.append("broker setup packet")
    if any((mission.get("permissions") or {}).get(key) for key in ("auto_profile_reply", "auto_counter", "auto_pass")):
        lane_issue = auto_lane_issue(mission)
        if lane_issue:
            blockers.append(lane_issue)
    if not mission.get("active", True) or not profile.get("active", True):
        blockers.append("active truck profile and mission")
    if not load.get("origin_verified"):
        blockers.append("broker pickup city")
    if not load.get("equipment_verified"):
        blockers.append("broker equipment")
    if not load.get("schedule_verified"):
        blockers.append("pickup and delivery schedule")
    if not load.get("destination_verified"):
        blockers.append("broker destination")
    pickup_value = str(load.get("pickup_date") or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", pickup_value):
        # Without a pickup date there is no occupancy window, so collision
        # checks cannot run: booking must never skip them.
        blockers.append("broker pickup date")
    elif (mission.get("pickup_start") or mission.get("pickup_end")) and not load.get("pickup_date_verified"):
        blockers.append("broker pickup date")
    if not (_number(profile.get("max_weight_lbs")) or _number(mission.get("max_weight_lbs"))):
        blockers.append("truck weight capacity")
    if load.get("weight_lbs") is None:
        blockers.append("broker load weight")
    if not load.get("loaded_miles_verified"):
        blockers.append("broker loaded miles")
    if not load.get("deadhead_miles_verified"):
        blockers.append("actual deadhead miles")
    return blockers


def _auto_send_blockers(load: dict, mission: dict, profile: dict) -> list[str]:
    """Configuration problems that make even an automatic rate reply unsafe.

    Broker load-fit details belong to the stricter booking gate. Dispatchers can
    negotiate while collecting those facts, as long as the configured price
    policy has the mileage inputs it needs.
    """
    blockers: list[str] = []
    if any((mission.get("permissions") or {}).get(key) for key in ("auto_profile_reply", "auto_counter", "auto_pass")):
        lane_issue = auto_lane_issue(mission)
        if lane_issue:
            blockers.append(lane_issue)
    if not mission.get("active", True) or not profile.get("active", True):
        blockers.append("active truck profile and mission")
    if not (_number(profile.get("max_weight_lbs")) or _number(mission.get("max_weight_lbs"))):
        blockers.append("truck weight capacity")
    return blockers


def _broker_detail_labels(blockers: list[str], readings: list[dict[str, Any]]) -> list[str]:
    """Turn booking gaps into questions the broker can actually answer."""
    internal = {"active truck profile and mission", "truck weight capacity", "actual deadhead miles",
                "broker credit approval", "broker setup packet"}
    requested = [
        item for item in blockers
        if item not in internal
        and not item.startswith("truck availability (")
        and not item.startswith("blocked broker (")
        and not re.match(r"^stop \d+ ", item)
    ]
    if "pickup and delivery schedule" in requested:
        has_pickup = any(row.get("pickup_schedule_evidence") for row in readings)
        has_delivery = any(row.get("delivery_schedule_evidence") for row in readings)
        requested.remove("pickup and delivery schedule")
        if not has_pickup:
            requested.append("pickup appointment time or window")
        if not has_delivery:
            requested.append("delivery appointment time or window")
    labels = {
        "broker pickup city": "pickup city and state",
        "broker destination": "delivery city and state",
        "broker equipment": "required trailer",
        "broker pickup date": "pickup date",
        "broker load weight": "load weight",
        "broker loaded miles": "loaded miles",
        "pickup appointment time or window": "pickup appointment time or window",
        "delivery appointment time or window": "delivery appointment time or window",
    }
    return [labels.get(item, item) for item in requested]


def _create_alert(thread_id: str, kind: str, summary: str, storage=None) -> dict:
    storage = storage or store
    existing = storage.list("freight_alerts", {"thread_id": thread_id, "kind": kind, "status": "open"}, order="", limit=1)
    if existing:
        return existing[0]
    return storage.insert("freight_alerts", {
        "id": new_id(), "thread_id": thread_id, "kind": kind, "summary": summary,
        "status": "open", "created_at": now_iso(), "resolved_at": None,
    })


def _create_draft(thread_id: str, subject: str, body: str, reason: str, policy: dict, in_reply_to: str, storage=None) -> dict:
    storage = storage or store
    # Reprocessing the same broker message (crash recovery, re-check) must not
    # stack identical replies: one pending draft per message + reason.
    if in_reply_to:
        for existing in storage.list("freight_drafts", {"thread_id": thread_id, "status": "pending"}, order="", limit=200):
            if existing.get("reason") == reason and (existing.get("in_reply_to_message_id") or "") == in_reply_to:
                return existing
    return storage.insert("freight_drafts", {
        "id": new_id(), "thread_id": thread_id, "in_reply_to_message_id": in_reply_to,
        "subject": subject, "body_text": body, "reason": reason, "policy_snapshot": policy,
        "status": "pending", "created_at": now_iso(), "updated_at": now_iso(),
    })


def _already_sent_reply(thread_id: str, body: str, reason: str, storage=None, *, in_reply_to: str = "") -> bool:
    """Suppress only a true duplicate: the same counter to the SAME broker message.

    Text overlap with older turns is never a reason to stop replying: a broker's
    new message always gets an answer, even when parts of that answer repeat
    what was said before. Re-evaluating one broker message (for example after
    manual fact confirmation) must not resend its counter, so dedupe keys on
    the triggering message, never on message text.
    """
    storage = storage or store
    if not reason.startswith("counter_") or not in_reply_to:
        return False
    amount = extract_offer(body)
    if amount is None:
        return False
    duplicates = [
        draft for draft in storage.list("freight_drafts", {"thread_id": thread_id}, order="", limit=500)
        if draft.get("status") == "sent"
        and str(draft.get("reason") or "").startswith("counter_")
        and str(draft.get("in_reply_to_message_id") or "") == str(in_reply_to)
    ]
    return any(extract_offer(draft.get("body_text") or "") == amount for draft in duplicates)


def _sent_draft_policies(thread_id: str, reason: str, storage=None) -> list[dict]:
    storage = storage or store
    return [
        draft.get("policy_snapshot") or {}
        for draft in storage.list("freight_drafts", {"thread_id": thread_id}, order="", limit=500)
        if draft.get("status") == "sent" and str(draft.get("reason") or "") == reason
    ]


def _answered_profile_questions(thread_id: str, storage=None) -> set[str]:
    """Broker questions already answered on an earlier turn of this thread."""
    answered: set[str] = set()
    for policy in _sent_draft_policies(thread_id, "profile_fact_reply", storage):
        answered.update(policy.get("answered_questions") or [])
    return answered


def _counter_send_count(thread_id: str, amount: float, storage=None) -> int:
    """How many times this exact counter amount was already sent on the thread."""
    storage = storage or store
    events = storage.list("freight_negotiation_events", {"thread_id": thread_id, "event_type": "counter"}, order="", limit=1000)
    return sum(1 for event in events if _number(event.get("amount")) == amount)


def _profile_answers(questions: list[str], profile: dict, mission: dict) -> tuple[list[str], list[str], list[str]]:
    """Answer broker questions about our truck from shareable profile facts.

    Returns (answers, missing_labels, answered_questions).
    """
    shareable = set(profile.get("shareable_fields") or [])
    answers: list[str] = []
    missing: list[str] = []
    answered: list[str] = []
    for question in questions:
        if question == "team_status":
            if profile.get("team_status") and "team_status" in shareable:
                value = str(profile["team_status"]).lower()
                answers.append("Yes, this is a true team." if value in {"team", "true team", "yes"} else f"This is a {profile['team_status']} truck.")
                answered.append(question)
            else:
                missing.append("team status")
        elif question == "equipment_type":
            equipment = profile.get("equipment_type") or mission.get("equipment_type")
            if equipment and "equipment_type" in shareable:
                length = profile.get("trailer_length_ft") or mission.get("trailer_length_ft")
                answers.append(f"We have a {length} ft {equipment}." if length else f"We have a {equipment}.")
                answered.append(question)
            else:
                missing.append("equipment type")
        elif question == "mc_or_dot":
            pieces = []
            if profile.get("mc_number") and "mc_number" in shareable: pieces.append(f"MC {profile['mc_number']}")
            if profile.get("dot_number") and "dot_number" in shareable: pieces.append(f"DOT {profile['dot_number']}")
            if pieces:
                answers.append(" / ".join(pieces))
                answered.append(question)
            else:
                missing.append("MC/DOT")
    return answers, missing, answered


def record_test_send(draft_id: str, storage=None) -> dict[str, Any]:
    """Complete a draft inside the local agent tester without email transport."""
    storage = storage or store
    draft = storage.get("freight_drafts", draft_id)
    if not draft or draft.get("status") != "pending":
        raise ValueError("Draft is no longer available")
    thread = storage.get("freight_threads", draft.get("thread_id")) or {}
    load = storage.get("freight_loads", thread.get("load_id")) or {}
    stamp = now_iso()
    message_id = f"agent-test-out:{new_id()}"
    storage.insert("freight_messages", {
        "id": new_id(), "thread_id": thread.get("id"), "direction": "out",
        "provider_message_id": message_id, "from_email": "", "to_email": "",
        "subject": thread.get("subject") or draft.get("subject") or "Agent test response",
        "body_text": draft.get("body_text") or "",
        "classification": {"kind": "agent_test_response", "reason": draft.get("reason")},
        "status": "test_sent", "created_at": stamp,
    })
    storage.update("freight_drafts", draft_id, {"status": "sent", "updated_at": stamp})
    counter_amount = extract_offer(draft.get("body_text") or "") if str(draft.get("reason") or "").startswith("counter_") else None
    if counter_amount is not None:
        restate = _counter_send_count(thread["id"], counter_amount, storage) > 0
        _record_negotiation_event(thread["id"], draft_id, "counter", counter_amount, {"reason": draft.get("reason"), "transport": "local", "restate": restate}, storage)
    next_state = "negotiating" if counter_amount is not None else "passed" if draft.get("reason") == "pass_below_floor" else "waiting"
    storage.update("freight_threads", thread["id"], {"last_message_id": message_id, "state": next_state, "last_activity_at": stamp, "updated_at": stamp})
    if load.get("id"):
        storage.update("freight_loads", load["id"], {"status": next_state, "updated_at": stamp})
        _counter_count(thread["id"], load, storage)
    return {"message_id": message_id, "transport": "local"}


def _finish_permitted_draft(draft: dict, mission: dict, storage=None, *, transport: str = "gmail") -> dict[str, Any]:
    """Auto-send bounded replies explicitly enabled on an active mission."""
    storage = storage or store
    permissions = mission.get("permissions") or {}
    reason = draft.get("reason") or ""
    policy = draft.get("policy_snapshot") or {}
    if not mission.get("active", True) or auto_lane_issue(mission):
        return {"action": "draft", "draft": draft}
    facts_permitted = not policy.get("includes_profile_answers") or permissions.get("auto_profile_reply")
    auto_mode = any(permissions.get(key) for key in ("auto_profile_reply", "auto_counter", "auto_pass"))
    permitted = (
        (reason == "profile_fact_reply" and permissions.get("auto_profile_reply"))
        or (reason == "clarify_load_details" and auto_mode and facts_permitted)
        or (reason.startswith("counter_") and permissions.get("auto_counter") and facts_permitted and policy.get("safe_to_auto_send"))
        or (reason == "pass_below_floor" and permissions.get("auto_pass") and facts_permitted and policy.get("safe_to_auto_send"))
    )
    if not permitted:
        return {"action": "draft", "draft": draft}
    if transport == "test":
        sent = record_test_send(draft["id"], storage)
        return {"action": "sent", "draft": storage.get("freight_drafts", draft["id"]), **sent}
    try:
        sent = send_draft(draft["id"], storage)
        return {"action": "sent", "draft": draft, **sent}
    except Exception as exc:
        summary = f"Automatic reply was not confirmed: {str(exc)[:160]}"
        _create_alert(draft["thread_id"], "auto_send_failed", summary, storage)
        return {"action": "alert", "draft": storage.get("freight_drafts", draft["id"]), "warning": summary}


def evaluate_inbound(thread: dict, message: dict, storage=None, *, preserve_verified_facts: bool = False, transport: str = "gmail") -> dict[str, Any]:
    storage = storage or store
    thread = storage.get("freight_threads", thread["id"]) or thread
    prior_state = thread.get("state") or "waiting"
    if prior_state == "closed":
        return {"action": "ignored", "reason": "thread_closed"}
    _supersede_pending_drafts(thread["id"], storage, keep_in_reply_to=message.get("provider_message_id") or "")
    load = storage.get("freight_loads", thread["load_id"]) or {}
    mission = storage.get("freight_missions", load.get("mission_id")) or {}
    profile_id = load.get("truck_profile_id") or mission.get("truck_profile_id")
    profile = storage.get("freight_truck_profiles", profile_id) or {}
    broker = resolve_broker(load.get("broker_email"), load.get("broker_company"), storage, thread_id=thread["id"])
    if broker and not load.get("broker_id"):
        load = storage.update("freight_loads", load["id"], {"broker_id": broker["id"], "updated_at": now_iso()})
    if not mission.get("active", True) or not profile.get("active", True):
        summary = "Truck profile or mission is inactive. Review this broker reply before continuing."
        _create_alert(thread["id"], "inactive_configuration", summary, storage)
        _set_stage(thread, load, "needs_attention", storage)
        return {"action": "alert", "summary": summary}
    text = strip_quoted_reply(message.get("body_text") or "")
    if settings.FREIGHT_AGENT_MODE == "rules":
        classification = classify_reply(text)
    else:
        prior_classification = message.get("classification") or {}
        if preserve_verified_facts and prior_classification.get("source") == "gemini":
            classification = prior_classification
        else:
            history = storage.list("freight_messages", {"thread_id": thread["id"]}, order="created_at asc", limit=100)
            history = [row for row in history if row["id"] != message["id"]]
            try:
                classification = interpret_broker_reply(text, history, load, mission, profile)
            except Exception as exc:
                logger.warning("Freight interpretation failed for thread %s: %s", thread["id"], type(exc).__name__)
                summary = "Could not interpret the broker reply. Review it before responding."
                _create_alert(thread["id"], "agent_interpretation_failed", summary, storage)
                _set_stage(thread, load, "needs_attention", storage)
                return {"action": "alert", "summary": summary}
        # A lexical match may add a protected handoff, but never remove one found by the model.
        lexical_protected = [item for item in classify_reply(text)["protected"] if item != "price_accepted"]
        classification["protected"] = list(dict.fromkeys(classification["protected"] + lexical_protected))
        if classification.get("intent") == "acceptance" and "price_accepted" not in classification["protected"]:
            classification["protected"].append("price_accepted")
    if MIXED_QUOTED_RATE.search(text) and "?" in text:
        classification["ambiguous_offer"] = True
        classification["offer"] = None
        classification["rate_per_mile"] = None
        classification["kind"] = "ambiguous_rate"
    storage.update("freight_messages", message["id"], {"classification": classification})
    # A clear close or factoring denial is not an invitation to keep negotiating.
    # Do not override model uncertainty for other messages.
    if classification.get("intent") == "closed" or (classification.get("source") != "gemini" and CLOSED_REPLY.search(text)):
        summary = "Broker says the load is unavailable or this conversation is finished."
        _create_alert(thread["id"], "thread_closed", summary, storage)
        _set_stage(thread, load, "closed", storage)
        return {"action": "closed", "classification": classification, "summary": summary}
    protected = classification["protected"]
    if protected:
        labels = {
            "call_requested": "Broker requested a call.",
            "sensitive_driver_info": "Broker requested sensitive driver information.",
            "rate_confirmation": "Broker mentioned or sent a rate confirmation.",
            "price_accepted": "Broker appears to have accepted or confirmed a price.",
            "operational_terms": "Broker mentioned special operating or payment terms; dispatcher review required.",
            "rate_refused": "Broker refused a rate; review before making another offer.",
        }
        summary = " ".join(labels[item] for item in protected)
        _create_alert(thread["id"], protected[0], summary, storage)
        if "price_accepted" in protected:
            accepted_amount = _number(classification.get("offer")) or _number(load.get("current_offer")) or _number(load.get("posted_rate"))
            if accepted_amount:
                record_agreement(thread["id"], accepted_amount, message.get("provider_message_id") or message["id"], storage)
        next_state = "booked" if prior_state == "booked" else "accepted_pending_review" if {"rate_confirmation", "price_accepted"} & set(protected) else "protected_review"
        _set_stage(thread, load, next_state, storage)
        return {"action": "alert", "classification": classification, "summary": summary}

    if classification.get("intent") == "handoff":
        summary = classification.get("summary") or "Broker requested a human handoff."
        _create_alert(thread["id"], "broker_handoff", summary, storage)
        _set_stage(thread, load, "protected_review", storage)
        return {"action": "alert", "classification": classification, "summary": summary}

    permissions = mission.get("permissions") or {}
    if any(permissions.get(key) for key in ("auto_profile_reply", "auto_counter", "auto_pass")):
        lane_issue = auto_lane_issue(mission)
        if lane_issue:
            _create_alert(thread["id"], "auto_lane_configuration", lane_issue, storage)
            _set_stage(thread, load, "needs_attention", storage)
            return {"action": "alert", "classification": classification, "summary": lane_issue}

    if prior_state in {"booked", "accepted_pending_review", "offer_review", "protected_review", "mismatch"}:
        summary = f"New broker reply on a {prior_state.replace('_', ' ')} load needs review."
        _create_alert(thread["id"], "state_review", summary, storage)
        return {"action": "alert", "classification": classification, "summary": summary}

    conflicting_facts = [unit for unit in ("weight", "miles", "deadhead_miles") if len({fact["value"] for fact in classification["numeric_facts"] if fact["unit"] == unit}) > 1]
    destinations_mentioned = {(match.group(1).strip().casefold(), match.group(2).upper()) for match in DESTINATION_MENTION.finditer(text)} if classification.get("source") != "gemini" else set()
    if len(destinations_mentioned) > 1:
        conflicting_facts.append("destinations")
    if conflicting_facts:
        summary = f"Broker gave multiple {', '.join(conflicting_facts)} values. Review the load details before negotiating."
        _create_alert(thread["id"], "conflicting_load_facts", summary, storage)
        _set_stage(thread, load, "needs_attention", storage)
        return {"action": "alert", "classification": classification, "summary": summary}

    load = _enrich_load_facts(load, mission, classification, text, storage, preserve_verified=preserve_verified_facts)
    _sync_load_stops(load, classification, message["id"], storage)
    mismatches = _mismatch_reasons(text, load, mission, profile, classification)
    if mismatches:
        summary = " ".join(mismatches)
        _create_alert(thread["id"], "load_mismatch", summary, storage)
        _set_stage(thread, load, "mismatch", storage)
        return {"action": "alert", "classification": classification, "summary": summary}

    if classification["ambiguous_offer"]:
        summary = "More than one possible rate appears in this reply. Confirm the total all-in rate before sending."
        draft = _create_draft(thread["id"], thread["subject"], "Can you confirm the total all-in rate for this load?", "clarify_rate", {"manual_only": True}, message.get("provider_message_id") or "", storage)
        _create_alert(thread["id"], "ambiguous_rate", summary, storage)
        _set_stage(thread, load, "needs_attention", storage)
        return {"action": "draft", "draft": draft, "classification": classification, "summary": summary}

    offer = classification.get("offer")
    rpm = classification.get("rate_per_mile")
    if rpm is not None and offer is None:
        if load.get("loaded_miles_verified") and _number(load.get("loaded_miles")):
            offer = round(rpm * float(load["loaded_miles"]), 2)
            classification["offer"] = offer
            classification["offer_source"] = "rate_per_mile"
            storage.update("freight_messages", message["id"], {"classification": classification})
        elif _number(mission.get("floor_loaded_rpm")) and rpm >= float(mission["floor_loaded_rpm"]):
            body = f"${rpm:g}/mile works for us. Please send over the rate confirmation with total miles."
            draft = _create_draft(thread["id"], thread["subject"], body, "accept_per_mile", {"manual_only": True}, message.get("provider_message_id") or "", storage)
            _set_stage(thread, load, "draft_ready", storage)
            return {"classification": classification, **_finish_permitted_draft(draft, mission, storage, transport=transport)}
        else:
            summary = f"Broker quoted ${rpm:g} per mile; loaded miles need confirmation before calculating a total."
            policy = {"safe_to_auto_send": True, "auto_send_blockers": [], "booking_readiness_blockers": _booking_readiness_blockers(load, mission, profile, storage)}
            draft = _create_draft(thread["id"], thread["subject"], "Can you confirm the loaded miles and total all-in rate?", "clarify_load_details", policy, message.get("provider_message_id") or "", storage)
            _set_stage(thread, load, "draft_ready", storage)
            return {"classification": classification, "summary": summary, **_finish_permitted_draft(draft, mission, storage, transport=transport)}

    unknown_amounts = [fact for fact in classification["numeric_facts"] if fact["unit"] == "unknown" and 100 <= fact["value"] <= 100000]
    if offer is None and unknown_amounts:
        summary = "A possible rate was mentioned without clear rate wording. Confirm it before negotiating."
        draft = _create_draft(thread["id"], thread["subject"], "Can you confirm the total all-in rate?", "clarify_rate", {"manual_only": True}, message.get("provider_message_id") or "", storage)
        _create_alert(thread["id"], "unclear_rate", summary, storage)
        _set_stage(thread, load, "needs_attention", storage)
        return {"action": "draft", "draft": draft, "classification": classification, "summary": summary}

    carried_offer = False
    if (offer is None and classification.get("source") == "gemini" and classification.get("intent") == "details"
            and not classification["questions"] and prior_state in {"waiting", "draft_ready", "needs_attention", "negotiating"}):
        offer = _number(load.get("current_offer"))
        if offer is not None:
            carried_offer = True
            classification["active_offer"] = offer
            storage.update("freight_messages", message["id"], {"classification": classification})
    if offer is not None and not carried_offer:
        _record_negotiation_event(thread["id"], message["id"], "offer", offer, {"source": classification.get("offer_source") or "total_rate"}, storage)
    if prior_state == "passed":
        previous_offer = _number(load.get("current_offer"))
        if offer is None or (previous_offer is not None and offer <= previous_offer):
            summary = "Load was passed; review this broker reply before resuming."
            _create_alert(thread["id"], "passed_reply", summary, storage)
            return {"action": "alert", "classification": classification, "summary": summary}
        _set_stage(thread, load, "negotiating", storage)

    answered_before = _answered_profile_questions(thread["id"], storage)
    new_questions = [q for q in classification["questions"] if q not in answered_before]
    answers, missing, answered_now = _profile_answers(new_questions, profile, mission)
    if missing:
        summary = f"Complete or allow sharing for: {', '.join(missing)}."
        _create_alert(thread["id"], "profile_missing", summary, storage)
        _set_stage(thread, load, "needs_attention", storage)
        return {"action": "alert", "classification": classification, "summary": summary}

    if offer is not None:
        if not carried_offer:
            storage.update("freight_loads", load["id"], {"current_offer": offer, "updated_at": now_iso()})
        if not load.get("destination_verified"):
            summary = "Confirm the actual delivery city and state before judging this lane's rate."
            policy = {"offer": offer, "counter": None, "safe_to_auto_send": True, "auto_send_blockers": [], "booking_readiness_blockers": _booking_readiness_blockers(load, mission, profile, storage)}
            draft = _create_draft(thread["id"], thread["subject"], "What is the delivery city and state for this load?", "clarify_load_details", policy, message.get("provider_message_id") or "", storage)
            _set_stage(thread, load, "draft_ready", storage)
            return {"classification": classification, "summary": summary, **_finish_permitted_draft(draft, mission, storage, transport=transport)}
        if len(mission.get("destinations") or []) != 1 or str((mission.get("destinations") or [{}])[0].get("kind") or "") not in {"city", "state"}:
            summary = "This mission covers multiple or open destinations. Choose a specific lane and its target before deciding on this offer."
            _create_alert(thread["id"], "lane_target_needed", summary, storage)
            _set_stage(thread, load, "needs_attention", storage)
            return {"action": "alert", "classification": classification, "summary": summary}
        target_set = any((_number(mission.get(key)) or 0) > 0 for key in ("target_total", "target_all_in_rpm"))
        if not target_set:
            summary = "Set a price target for this destination lane before deciding on the broker's offer."
            _create_alert(thread["id"], "lane_target_needed", summary, storage)
            _set_stage(thread, load, "needs_attention", storage)
            return {"action": "alert", "classification": classification, "summary": summary}
        rpm_policy = any(_number(mission.get(field)) for field in ("floor_loaded_rpm", "floor_all_in_rpm", "target_all_in_rpm"))
        all_in_policy = any(_number(mission.get(field)) for field in ("floor_all_in_rpm", "target_all_in_rpm"))
        if (rpm_policy and not load.get("loaded_miles_verified")) or (all_in_policy and not load.get("deadhead_miles_verified")):
            summary = "Miles needed for the mission's per-mile limits are unverified. Review loaded and deadhead miles before negotiating."
            _create_alert(thread["id"], "miles_unverified", summary, storage)
            _set_stage(thread, load, "needs_attention", storage)
            return {"action": "alert", "classification": classification, "summary": summary}
        floor = _required_floor(load, mission)
        counter = _counter_value(load, mission, offer)
        economics = load_economics(load, offer)
        max_rounds = int(mission.get("maximum_counter_rounds") or 3)
        current_round = _counter_count(thread["id"], load, storage)
        booking_blockers = _booking_readiness_blockers(load, mission, profile, storage)
        auto_blockers = _auto_send_blockers(load, mission, profile)
        previous = storage.list("freight_messages", {"thread_id": thread["id"], "direction": "in"}, order="created_at asc", limit=100)
        readings = [row.get("classification") or {} for row in previous if row["id"] != message["id"]] + [classification]
        requested_details = _broker_detail_labels(booking_blockers, readings)
        if floor is None and counter is None:
            summary = "This mission has no usable total or per-mile price boundary. Set one before negotiating."
            _create_alert(thread["id"], "price_policy_missing", summary, storage)
            _set_stage(thread, load, "needs_attention", storage)
            return {"action": "alert", "classification": classification, "summary": summary}
        if floor is not None and offer < floor:
            body = f"We would need {_money(counter)}." if counter and counter > offer else "Pass, thank you."
            reason = "counter_below_floor" if counter and counter > offer else "pass_below_floor"
        elif counter and offer < counter:
            body = f"We would need {_money(counter)}."
            reason = "counter_to_target"
        else:
            if requested_details:
                body = "Thanks for the rate. Before we confirm, please send the remaining load details: " + ", ".join(requested_details) + "."
                policy = {
                    "offer": offer, "floor": floor, "counter": None, "includes_profile_answers": False,
                    "requested_details": requested_details,
                    "safe_to_auto_send": not auto_blockers, "auto_send_blockers": auto_blockers,
                    "booking_readiness_blockers": booking_blockers, **economics,
                }
                draft = _create_draft(thread["id"], thread["subject"], body, "clarify_load_details", policy, message.get("provider_message_id") or "", storage)
                _set_stage(thread, load, "draft_ready", storage)
                return {"classification": classification, **_finish_permitted_draft(draft, mission, storage, transport=transport)}
            summary = f"Broker offered {_money(offer)}, which meets the configured envelope. Review before accepting."
            _create_alert(thread["id"], "price_at_or_above_target", summary, storage)
            _set_stage(thread, load, "offer_review", storage)
            return {"action": "alert", "classification": classification, "summary": summary}
        if reason.startswith("counter_") and max_rounds and current_round >= max_rounds:
            summary = f"Counter limit reached. Broker offered {_money(offer)}."
            _create_alert(thread["id"], "counter_limit", summary, storage)
            _set_stage(thread, load, "needs_attention", storage)
            return {"action": "alert", "classification": classification, "summary": summary}
        if reason.startswith("counter_") and classification.get("source") == "gemini":
            try:
                history = storage.list("freight_messages", {"thread_id": thread["id"]}, order="created_at asc", limit=100)
                body = compose_counter_reply(text, [row for row in history if row["id"] != message["id"]], counter)
            except Exception as exc:
                logger.warning("Counter phrasing failed for thread %s: %s", thread["id"], type(exc).__name__)
        if answers:
            body = " ".join(answers + [body])
        if reason.startswith("counter_") and requested_details:
            body += " Also, please confirm " + ", ".join(requested_details) + "."
        if reason.startswith("counter_") and counter is not None and _counter_send_count(thread["id"], counter, storage) >= 2:
            auto_blockers = auto_blockers + [f"counter {_money(counter)} already sent twice"]
            summary = f"The counter {_money(counter)} was already sent twice on this thread. Follow up personally before repeating it."
            _create_alert(thread["id"], "counter_restate_limit", summary, storage)
        policy = {
            "offer": offer, "floor": floor, "counter": counter, "includes_profile_answers": bool(answers),
            "safe_to_auto_send": not auto_blockers, "auto_send_blockers": auto_blockers,
            "booking_readiness_blockers": booking_blockers, **economics,
        }
        if _already_sent_reply(thread["id"], body, reason, storage, in_reply_to=message.get("provider_message_id") or ""):
            # The same broker message was re-evaluated after its counter went out.
            return {"action": "duplicate", "classification": classification, "summary": "This reply was already sent for this broker message."}
        draft = _create_draft(thread["id"], thread["subject"], body, reason, policy, message.get("provider_message_id") or "", storage)
        if auto_blockers:
            _create_alert(thread["id"], "auto_send_blocked", f"Automatic rate reply paused: {', '.join(auto_blockers)}.", storage)
        _set_stage(thread, load, "draft_ready", storage)
        return {"classification": classification, **_finish_permitted_draft(draft, mission, storage, transport=transport)}

    if classification["questions"] and not new_questions:
        # Every question in this turn was already answered earlier. Never go
        # silent: prepare a manual restate so the human can re-send the answers.
        restate_answers, _, restate_answered = _profile_answers(classification["questions"], profile, mission)
        if restate_answers:
            body = " ".join(restate_answers)
            policy = {"manual_only": True, "answered_questions": restate_answered, "restate": True}
            draft = _create_draft(thread["id"], thread["subject"], body, "profile_fact_restate", policy, message.get("provider_message_id") or "", storage)
            summary = "Broker repeated a question that was already answered. Review and re-send the answer if needed."
            _create_alert(thread["id"], "repeated_question", summary, storage)
            _set_stage(thread, load, "draft_ready", storage)
            return {"action": "draft", "draft": draft, "classification": classification, "summary": summary}

    if answers:
        body = " ".join(answers)
        policy = {"answered_questions": answered_now}
        draft = _create_draft(thread["id"], thread["subject"], body, "profile_fact_reply", policy, message.get("provider_message_id") or "", storage)
        _set_stage(thread, load, "draft_ready", storage)
        return {"classification": classification, **_finish_permitted_draft(draft, mission, storage, transport=transport)}

    suggested = classification.get("suggested_reply") or ""
    if suggested and classification.get("source") == "gemini":
        draft = _create_draft(thread["id"], thread["subject"], suggested, "agent_suggested_reply", {"manual_only": True, "agent_summary": classification.get("summary")}, message.get("provider_message_id") or "", storage)
        _set_stage(thread, load, "draft_ready", storage)
        return {"action": "draft", "draft": draft, "classification": classification, "summary": classification.get("summary") or "Review the suggested reply."}
    # Never end a turn silently. When the broker's message does not fit a known
    # shape, keep the thread working: ask for the most valuable missing load
    # detail, or hand the human a follow-up draft with an explicit next action.
    booking_blockers = _booking_readiness_blockers(load, mission, profile, storage)
    auto_blockers = _auto_send_blockers(load, mission, profile)
    previous = storage.list("freight_messages", {"thread_id": thread["id"], "direction": "in"}, order="created_at asc", limit=100)
    readings = [row.get("classification") or {} for row in previous if row["id"] != message["id"]] + [classification]
    requested_details = _broker_detail_labels(booking_blockers, readings)
    if requested_details:
        body = "Thanks for the details. When you can, please also confirm " + ", ".join(requested_details) + "."
        policy = {
            "requested_details": requested_details, "includes_profile_answers": False,
            "safe_to_auto_send": not auto_blockers, "auto_send_blockers": auto_blockers,
            "booking_readiness_blockers": booking_blockers,
        }
        policy["manual_only"] = True
        draft = _create_draft(thread["id"], thread["subject"], body, "clarify_load_details", policy, message.get("provider_message_id") or "", storage)
        summary = "Broker reply did not match a standard reply type; review the follow-up question that keeps the thread moving."
        _set_stage(thread, load, "draft_ready", storage)
        return {"action": "draft", "draft": draft, "classification": classification, "summary": summary}
    summary = "The broker reply needs review because it did not match a permitted reply type. A follow-up draft is ready to edit."
    draft = _create_draft(thread["id"], thread["subject"], "Just checking in on this load - is it still available?", "follow_up_nudge", {"manual_only": True}, message.get("provider_message_id") or "", storage)
    _create_alert(thread["id"], "ambiguous_reply", summary, storage)
    _set_stage(thread, load, "draft_ready", storage)
    return {"action": "draft", "draft": draft, "classification": classification, "summary": summary}


def reevaluate_verified_load(thread_id: str, message: dict, storage=None, *, transport: str = "gmail") -> dict[str, Any]:
    """Resume a thread after manual fact confirmation and reconsider its latest reply."""
    storage = storage or store
    thread = storage.get("freight_threads", thread_id)
    if not thread:
        raise ValueError("Freight thread not found")
    load = storage.get("freight_loads", thread["load_id"])
    if not load:
        raise ValueError("Freight load not found")
    if thread.get("state") in {"closed", "booked"}:
        return {"action": "skipped", "reason": "thread_closed_or_booked"}
    _set_stage(thread, load, "waiting", storage)
    return evaluate_inbound(thread, message, storage, preserve_verified_facts=True, transport=transport)


def send_draft(draft_id: str, storage=None, *, confirm_sensitive: bool = False) -> dict[str, Any]:
    storage = storage or store
    draft = storage.get("freight_drafts", draft_id)
    if not draft or draft.get("status") != "pending":
        raise ValueError("Draft is no longer available")
    thread = storage.get("freight_threads", draft["thread_id"]) or {}
    load = storage.get("freight_loads", thread.get("load_id")) or {}
    if thread.get("state") in {"closed", "booked", "mismatch", "accepted_pending_review", "offer_review", "protected_review"}:
        raise ValueError("This thread needs review or is closed; reopen it before sending a draft")
    if draft.get("in_reply_to_message_id") and thread.get("last_message_id") != draft["in_reply_to_message_id"]:
        raise ValueError("A newer broker reply arrived; review it before sending this draft")
    profile_id = load.get("truck_profile_id") or (storage.get("freight_missions", load.get("mission_id")) or {}).get("truck_profile_id")
    profile = (storage.get("freight_truck_profiles", profile_id) or {}) if profile_id else {}
    never_send = _sensitive_profile_values_in_text(profile, (draft.get("subject") or "") + "\n" + (draft.get("body_text") or ""))
    if never_send:
        raise ValueError("Draft contains protected driver/vehicle data (" + ", ".join(never_send) + "); remove it before sending")
    sensitive_fields = sensitive_outbound_fields(draft.get("subject") or thread.get("subject", ""), draft.get("body_text") or "")
    if sensitive_fields and not confirm_sensitive:
        raise SensitiveOutboundConfirmationRequired(str(load.get("id") or ""), sensitive_fields)
    counter_amount = extract_offer(draft.get("body_text") or "") if str(draft.get("reason") or "").startswith("counter_") else None
    if str(draft.get("reason") or "").startswith("counter_") and counter_amount is None:
        raise ValueError("A counter draft must contain one clear total rate")
    cfg = freight_config(storage)
    sender = get_gmail_sender(str(thread.get("sender_account") or "1"), storage=storage, cfg=cfg)
    if not sender:
        raise ValueError("Gmail sender is unavailable")
    sender_cfg = freight_sender_context(cfg, sender)
    reply_message_id = make_msgid(domain=(sender.get("email") or "gmail.com").split("@")[-1])

    # Standard email client threading requires Re: prefix
    reply_subject = thread.get("subject", "")
    if not re.match(r"^(re|fw|fwd)\s*:\s*", reply_subject, re.IGNORECASE):
        reply_subject = f"Re: {reply_subject}"

    # Build complete RFC references chain in chronological order
    prior_messages = storage.list("freight_messages", {"thread_id": thread["id"]}, order="created_at asc", limit=100)
    history_ids = [str(m["provider_message_id"]) for m in prior_messages if m.get("provider_message_id")]
    parent = draft.get("in_reply_to_message_id") or (history_ids[-1] if history_ids else None) or thread.get("last_message_id") or thread.get("root_message_id")
    ref_list = []
    if thread.get("root_message_id"):
        ref_list.append(str(thread["root_message_id"]))
    for mid in history_ids:
        if mid and mid not in ref_list:
            ref_list.append(mid)
    if parent and parent not in ref_list:
        ref_list.append(parent)
    references = " ".join(dict.fromkeys(ref_list))

    provider = get_provider("gmail", cfg, str(sender["id"]), sender)
    if not storage.claim_status("freight_drafts", draft_id, "pending", "sending"):
        raise ValueError("This draft is already being sent or was sent")
    try:
        result = provider.send(
            to=thread["recipient_email"], subject=reply_subject, body=draft["body_text"], content_type="plain",
            from_address=sender.get("email") or "", from_name=sender_cfg.get("sender_name") or "Dispatch",
            reply_to=sender_cfg.get("reply_to") or sender.get("email") or "",
            headers={"Message-ID": reply_message_id, "In-Reply-To": parent or "", "References": references},
        )
    except Exception as exc:
        storage.update("freight_drafts", draft_id, {"status": "send_uncertain", "updated_at": now_iso()})
        _create_alert(thread["id"], "send_uncertain", "Email transport failed after sending began. Check the Gmail Sent folder before trying again.", storage)
        raise ValueError("Email delivery is uncertain; check Gmail Sent before trying again") from exc
    if not result.ok:
        storage.update("freight_drafts", draft_id, {"status": "send_uncertain" if result.retryable else "send_failed", "updated_at": now_iso()})
        if result.retryable:
            _create_alert(thread["id"], "send_uncertain", "Email delivery may have failed. Check the Gmail Sent folder before trying again.", storage)
        raise ValueError(result.error or "Email delivery failed")
    stamp = now_iso()
    real_message_id = result.provider_id or reply_message_id
    storage.insert("freight_messages", {
        "id": new_id(), "thread_id": thread["id"], "direction": "out", "provider_message_id": real_message_id,
        "from_email": sender.get("email") or "", "to_email": thread["recipient_email"], "subject": reply_subject,
        "body_text": draft["body_text"], "classification": {"kind": "approved_draft", "reason": draft.get("reason")},
        "status": "sent", "created_at": stamp,
    })
    storage.update("freight_drafts", draft_id, {"status": "sent", "updated_at": stamp})
    if counter_amount is not None:
        restate = _counter_send_count(thread["id"], counter_amount, storage) > 0
        _record_negotiation_event(thread["id"], draft_id, "counter", counter_amount, {"reason": draft.get("reason"), "restate": restate}, storage)
    next_state = "negotiating" if counter_amount is not None else "passed" if draft.get("reason") == "pass_below_floor" else "waiting"
    storage.update("freight_threads", thread["id"], {"last_message_id": real_message_id, "state": next_state, "last_activity_at": stamp, "updated_at": stamp})
    storage.update("freight_loads", load["id"], {"status": next_state, "updated_at": stamp})
    _counter_count(thread["id"], load, storage)
    return {"message_id": real_message_id}


def recover_uncertain_freight_sends(storage=None) -> int:
    """A crashed worker must not leave an email eligible for blind resending."""
    storage = storage or store
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    stale = storage.list("freight_drafts", {"status": "sending", "updated_at": ("lte", cutoff)}, order="updated_at asc", limit=100)
    for draft in stale:
        storage.update("freight_drafts", draft["id"], {"status": "send_uncertain", "updated_at": now_iso()})
        _create_alert(draft["thread_id"], "send_uncertain", "Email send was interrupted. Check Gmail Sent before trying again.", storage)
    return len(stale)


def _message_text(message: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace") if isinstance(payload, bytes) else str(payload or "")
        except Exception:
            continue
        (plain if content_type == "text/plain" else html).append(text)
    text = "\n".join(plain).strip() if plain else BeautifulSoup("\n".join(html), "html.parser").get_text("\n", strip=True)
    attachment_names = [part.get_filename() for part in message.walk() if part.get_filename()]
    if attachment_names:
        text = f"{text}\n\nAttachments: {', '.join(attachment_names)}".strip()
    return text


def _pdf_attachments(message: Message) -> list[tuple[str, bytes]]:
    """(filename, bytes) for PDF parts of an inbound email."""
    found: list[tuple[str, bytes]] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_disposition() != "attachment":
            continue
        filename = str(part.get_filename() or "")
        if not (filename.lower().endswith(".pdf") or part.get_content_type() == "application/pdf"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if isinstance(payload, bytes) and payload:
            found.append((filename or "rate-con.pdf", payload))
    return found


def _store_attachments(message: dict, attachments: list[tuple[str, bytes]], storage) -> None:
    """Persist inbound PDF bytes durably, idempotent per message + filename.

    Runs before any processing so a crash between inserting the message and
    parsing its PDF can never lose the document: the reconcile path re-reads
    the stored bytes.
    """
    storage = storage or store
    for filename, payload in attachments:
        if storage.list("freight_attachments", {"message_id": message["id"], "filename": filename}, order="", limit=1):
            continue
        storage.insert("freight_attachments", {
            "id": new_id(), "thread_id": message["thread_id"], "message_id": message["id"],
            "filename": filename, "content_b64": base64.b64encode(payload).decode("ascii"),
            "byte_size": len(payload), "created_at": now_iso()})


def _stored_attachments(message: dict, storage) -> list[tuple[str, bytes, str]]:
    """(filename, bytes, attachment_id) from the durable attachment store."""
    storage = storage or store
    out: list[tuple[str, bytes, str]] = []
    for row in storage.list("freight_attachments", {"message_id": message["id"]}, order="created_at asc", limit=20):
        try:
            payload = base64.b64decode(row.get("content_b64") or "")
        except Exception:
            continue
        if payload:
            out.append((row.get("filename") or "rate-con.pdf", payload, row["id"]))
    return out


def _handle_rate_con_attachments(thread: dict, message: dict, attachments: list[tuple[str, bytes, str]], storage) -> None:
    """Parse a rate-con PDF and compare it against the open booking."""
    from app.core.rate_con import parse_rate_con_pdf

    bookings = [b for b in storage.list("freight_bookings", {"thread_id": thread["id"]}, order="created_at desc", limit=5)
                if b.get("status") in {"agreed", "rate_con_review"}]
    if not bookings:
        _create_alert(thread["id"], "rate_con_no_booking",
                      "Rate confirmation arrived but no agreement is recorded. Review the thread first.", storage)
        return
    booking = bookings[0]
    filename, payload, attachment_id = attachments[0]
    # The source reference is the durable attachment row, so the original PDF
    # stays retrievable for audit at /freight/bookings/<id>/rate-con.pdf.
    source_ref = f"att:{attachment_id}:{filename}"
    if booking.get("rate_con_source_ref") == source_ref:
        return  # already ingested - a crash/retry replay must not reset review
    try:
        fields = parse_rate_con_pdf(payload)
    except Exception:
        logger.warning("Rate-con PDF parse failed for thread %s", thread["id"])
        _create_alert(thread["id"], "rate_con_parse_failed",
                      f"Could not read {filename}. Enter the rate confirmation details manually on the load page.", storage)
        return
    updated = submit_rate_con(booking["id"], fields, storage, source="pdf", source_ref=source_ref)
    diffs = updated.get("rate_con_diffs") or []
    if diffs:
        def _describe(d: dict) -> str:
            if d.get("status") == "unverified":
                return f"{d['field']}: agreed {d['agreed']} but missing on the rate con"
            return f"{d['field']}: agreed {d['agreed']} vs rate con {d['rate_con']}"
        detail = "; ".join(_describe(d) for d in diffs[:5])
        summary = f"Rate confirmation has {len(diffs)} issue(s): {detail}"
    else:
        snap_stops = (booking.get("snapshot") or {}).get("stops") or []
        # "Exact" only when every agreed term was actually compared: the parser
        # reads endpoints and scalar fields, never intermediate stops, facility
        # names, or appointment windows.
        if len(snap_stops) > 2 or any(stop.get("appointment") or stop.get("facility") for stop in snap_stops):
            summary = (f"Rate confirmation matches on the compared fields (first pickup, final delivery, pickup date, "
                       f"equipment, weight, total). The parser reads endpoints only, so verify intermediate stops, "
                       f"facilities and appointments against {filename} before approving.")
        else:
            summary = "Rate confirmation matches the agreement exactly. Review and approve it on the load page."
    if len(fields) < 3:
        summary += (f" Note: the parser read only {len(fields)} field(s) from {filename} (first match, text only) - "
                    "treat this as ambiguous and verify it against the original PDF.")
    _create_alert(thread["id"], "rate_con_compared", summary, storage)


def _normalize_subject(subject: str) -> str:
    value = str(subject or "").strip().lower()
    while re.match(r"^(re|fw|fwd)\s*:", value):
        value = re.sub(r"^(re|fw|fwd)\s*:\s*", "", value)
    return re.sub(r"\s+", " ", value)


def _find_thread(message: Message, sender_account: str, storage=None) -> dict | None:
    storage = storage or store
    message_from = parseaddr(message.get("From", ""))[1].lower()
    references = set(" ".join([message.get("In-Reply-To", ""), message.get("References", "")]).split())
    subject = _normalize_subject(message.get("Subject", ""))
    # Message-ID references do not authenticate the sender. An unrelated party
    # can quote or guess one; never route their reply into a broker's thread.
    candidates = [thread for thread in storage.list("freight_threads", {"sender_account": sender_account}, order="updated_at desc", limit=500)
                  if thread.get("state") != "closed" and message_from == str(thread.get("recipient_email") or "").lower()]
    matched_references = []
    for thread in candidates:
        root = str(thread.get("root_message_id") or "")
        last = str(thread.get("last_message_id") or "")
        if (root and root in references) or (last and last in references):
            matched_references.append(thread)
    if len(matched_references) == 1:
        return matched_references[0]
    if matched_references:
        return None
    fallback = [thread for thread in candidates if message_from == str(thread.get("recipient_email") or "").lower() and subject == _normalize_subject(thread.get("subject", ""))]
    return fallback[0] if len(fallback) == 1 else None


def _process_inbound(thread: dict, message: dict, parsed, storage) -> dict:
    """Evaluate one inbound broker message and mark it processed.

    Split out from the poll loop so the reconcile path runs the exact same
    steps for messages a crash left behind.
    """
    result = evaluate_inbound({**thread, "last_message_id": message.get("provider_message_id") or thread.get("last_message_id")}, message, storage)
    classification = result.get("classification") or {}
    if "rate_confirmation" in (classification.get("protected") or []):
        if parsed is not None:
            # Persist the PDF bytes before any processing so a crash mid-parse
            # can never lose the document.
            _store_attachments(message, _pdf_attachments(parsed), storage)
        attachments = _stored_attachments(message, storage)
        if attachments:
            try:
                _handle_rate_con_attachments(thread, message, attachments, storage)
            except Exception:
                logger.warning("Rate-con handling failed for thread %s", thread["id"], exc_info=True)
    storage.update("freight_messages", message["id"], {"processing_state": "processed", "processing_error": None})
    return result


def reconcile_unprocessed_inbound(storage=None) -> int:
    """Re-run inbound messages a previous poll inserted but never finished.

    A crash between insert and evaluation used to strand the broker's message
    forever: the next poll skipped it on provider_message_id. Anything still
    pending/failed is reprocessed here; _create_draft and record_agreement are
    idempotent per message, so a retried message does not double-reply.

    The pending/failed filter happens in the query (paged) so a long processed
    history can never push unprocessed rows out of the window. A message whose
    thread closed while it waited is surfaced with an alert instead of being
    dropped silently, and a message that fails a repeated attempt escalates to
    the dispatcher.
    """
    storage = storage or store
    # Rows drain out of the pending/failed window: processed on success,
    # failed on a first failure, dead on a repeated failure. A permanent
    # failure can therefore never occupy the first page forever and starve the
    # rows behind it, however many of them there are.
    recovered = 0
    attempted: set[str] = set()
    while True:
        pending = storage.list("freight_messages", {"direction": "in", "processing_state": "pending"}, order="created_at asc", limit=200)
        failed = storage.list("freight_messages", {"direction": "in", "processing_state": "failed"}, order="created_at asc", limit=200)
        batch = [row for row in pending + failed if row["id"] not in attempted]
        if not batch:
            return recovered
        for message in batch:
            attempted.add(message["id"])
            repeated = message.get("processing_state") == "failed"
            thread = storage.get("freight_threads", message["thread_id"])
            if not thread or thread.get("state") == "closed":
                storage.update("freight_messages", message["id"], {"processing_state": "processed", "processing_error": None})
                if thread:
                    _create_alert(thread["id"], "inbound_stranded_closed",
                                  "A broker message was never processed before this thread closed. Review the message before reopening or booking.",
                                  storage)
                continue
            try:
                # The raw RFC822 is gone; attachments were already stored on the
                # first pass, so only the evaluation needs to be replayed.
                _process_inbound(thread, message, None, storage)
                recovered += 1
            except Exception as exc:
                logger.warning("Reconcile failed for message %s: %s", message["id"], exc, exc_info=True)
                if repeated:
                    # Second failure: leave the retry window so later failures
                    # are never starved, and escalate to the dispatcher.
                    storage.update("freight_messages", message["id"], {"processing_state": "dead", "processing_error": str(exc)[:500]})
                    _create_alert(thread["id"], "inbound_reprocess_failed",
                                  f"A broker reply could not be processed after repeated attempts: {str(exc)[:160]}",
                                  storage)
                else:
                    storage.update("freight_messages", message["id"], {"processing_state": "failed", "processing_error": str(exc)[:500]})
                    _create_alert(thread["id"], "inbound_processing_failed",
                                  f"A broker reply could not be processed and will be retried: {str(exc)[:160]}",
                                  storage)


def poll_freight_replies(storage=None) -> dict[str, int]:
    storage = storage or store
    active_threads = storage.list("freight_threads", order="updated_at desc", limit=1)
    if not active_threads:
        return {"accounts": 0, "messages": 0, "matched": 0}
    cfg = storage.get("settings", 1) or {}
    account_ids = {str(row.get("sender_account")) for row in storage.list("freight_threads", order="", limit=1000) if row.get("state") != "closed"}
    senders = [row for row in list_gmail_senders(active_only=True, storage=storage, cfg=cfg) if str(row.get("id")) in account_ids and row.get("provider", "gmail") == "gmail"]
    totals = {"accounts": 0, "messages": 0, "matched": 0}
    totals["recovered"] = reconcile_unprocessed_inbound(storage)
    for sender in senders:
        account_id = str(sender["id"])
        cursor_rows = storage.list("freight_mail_cursors", {"sender_account": account_id}, order="", limit=1)
        cursor = cursor_rows[0] if cursor_rows else None
        last_uid = int((cursor or {}).get("last_imap_uid") or 0)
        try:
            password = sender_password(sender)
            if not password:
                raise ValueError("Gmail app password is missing")
            with imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=15) as mailbox:
                mailbox.login(sender["email"], password)
                mailbox.select("INBOX", readonly=True)

                threads_for_account = [t for t in storage.list("freight_threads", {"sender_account": account_id}, order="updated_at desc", limit=10000) if t.get("state") != "closed"]
                recipients = {str(t.get("recipient_email")).strip().lower() for t in threads_for_account if t.get("recipient_email")}
                candidate_uids = set()

                for recipient in recipients:
                    status, data = mailbox.uid("search", None, f'FROM "{recipient}"')
                    if status == "OK" and data and data[0]:
                        for val in data[0].split():
                            if val:
                                candidate_uids.add(int(val))

                if last_uid:
                    status, data = mailbox.uid("search", None, f"UID {last_uid + 1}:*")
                    if status == "OK" and data and data[0]:
                        for val in data[0].split():
                            if val:
                                candidate_uids.add(int(val))
                elif not candidate_uids:
                    status, data = mailbox.uid("search", None, "ALL")
                    if status == "OK" and data and data[0]:
                        all_uids = [int(v) for v in data[0].split() if v]
                        for val in all_uids[-50:]:
                            candidate_uids.add(val)

                uids = sorted(candidate_uids)
                max_uid = last_uid
                for uid in uids:
                    max_uid = max(max_uid, uid)
                    status, header_parts = mailbox.uid("fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES FROM SUBJECT DATE)])")
                    if status != "OK" or not header_parts:
                        continue
                    raw_header = next((part[1] for part in header_parts if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
                    if not raw_header:
                        continue
                    header_msg = email.message_from_bytes(raw_header)
                    provider_id = header_msg.get("Message-ID") or f"imap:{account_id}:{uid}"
                    if storage.list("freight_messages", {"provider_message_id": provider_id}, order="", limit=1):
                        continue
                    thread = _find_thread(header_msg, account_id, storage)
                    if not thread:
                        continue
                    broker_parent = header_msg.get("In-Reply-To") or (header_msg.get("References", "").split()[0] if header_msg.get("References") else None)
                    if broker_parent and broker_parent != thread.get("root_message_id"):
                        storage.update("freight_threads", thread["id"], {"root_message_id": broker_parent})
                        thread["root_message_id"] = broker_parent

                    status, raw_parts = mailbox.uid("fetch", str(uid), "(RFC822)")
                    if status != "OK" or not raw_parts:
                        continue
                    raw = next((part[1] for part in raw_parts if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
                    if not raw:
                        continue

                    totals["messages"] += 1
                    parsed = email.message_from_bytes(raw)
                    from_email = parseaddr(parsed.get("From", ""))[1]
                    recipients_str = ", ".join(addr for _, addr in getaddresses(parsed.get_all("To", [])))
                    try:
                        created_at = parsedate_to_datetime(parsed.get("Date"))
                        created = created_at.astimezone(timezone.utc).isoformat() if created_at else now_iso()
                    except Exception:
                        created = now_iso()
                    incoming = storage.insert("freight_messages", {
                        "id": new_id(), "thread_id": thread["id"], "direction": "in", "provider_message_id": provider_id,
                        "from_email": from_email, "to_email": recipients_str, "subject": parsed.get("Subject", ""),
                        "body_text": _message_text(parsed), "classification": {}, "status": "received",
                        "processing_state": "pending", "created_at": created,
                    })
                    stamp = now_iso()
                    storage.update("freight_threads", thread["id"], {"last_message_id": provider_id, "last_imap_uid": uid, "last_activity_at": stamp, "updated_at": stamp})
                    # A crash here must never strand the message: it stays
                    # pending/failed and the next poll's reconcile picks it up.
                    try:
                        _process_inbound(thread, incoming, parsed, storage)
                    except Exception as exc:
                        logger.warning("Inbound processing failed for message %s: %s", incoming["id"], exc, exc_info=True)
                        storage.update("freight_messages", incoming["id"], {"processing_state": "failed", "processing_error": str(exc)[:500]})
                        _create_alert(thread["id"], "inbound_processing_failed",
                                      f"A broker reply could not be processed and will be retried: {str(exc)[:160]}",
                                      storage)
                    totals["matched"] += 1
                stamp = now_iso()
                values = {"last_imap_uid": max_uid, "last_checked_at": stamp, "error": None, "updated_at": stamp}
                if cursor:
                    storage.update("freight_mail_cursors", cursor["id"], values)
                else:
                    storage.insert("freight_mail_cursors", {"id": new_id(), "sender_account": account_id, **values})
                totals["accounts"] += 1
        except Exception as exc:
            logger.warning("Freight IMAP poll failed for sender %s: %s", account_id, exc)
            stamp = now_iso()
            values = {"last_checked_at": stamp, "error": str(exc)[:500], "updated_at": stamp}
            if cursor:
                storage.update("freight_mail_cursors", cursor["id"], values)
            else:
                storage.insert("freight_mail_cursors", {"id": new_id(), "sender_account": account_id, "last_imap_uid": 0, **values})
    return totals
