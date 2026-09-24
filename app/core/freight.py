"""Freight missions, load economics, one-to-one mail, and guarded reply handling."""
from __future__ import annotations

import email
import imaplib
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
        r"\b(full (?:drivers?|drv) info|drivers?(?:'s)? info|drv info|driver details|driver license|cdl|date of birth|dob|social security|ssn)\b",
        re.I,
    ),
    "rate_confirmation": re.compile(r"\b(rate[\s_-]*con(?:firmation)?|confirmation attached|sign(?:ed)? confirmation)\b", re.I),
    "price_accepted": re.compile(r"\b(we accept|accepted|that works|rate works|book it|you got it|agreed|deal|confirmed at)\b", re.I),
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


def parse_destinations(labels: list[str], kinds: list[str], radii: list[str]) -> list[dict[str, Any]]:
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
        unit = "unknown"
        if any(start < date_end and end > date_start for date_start, date_end in dates):
            unit = "date"
        elif any(start < phone_end and end > phone_start for phone_start, phone_end in phones):
            unit = "phone"
        elif re.match(r"\s*(?:am|pm)\b", after, re.I) or (TIME_CUE.search(before) and value <= 2359):
            unit = "time"
        elif weight_suffix:
            unit = "weight"
        elif miles_suffix or loaded_miles_suffix:
            unit = "deadhead_miles" if deadhead_cue else "miles"
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


def _supersede_pending_drafts(thread_id: str, storage=None) -> None:
    storage = storage or store
    for draft in storage.list("freight_drafts", {"thread_id": thread_id, "status": "pending"}, order="", limit=1000):
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
    if state in {"booked", "closed", "passed"}:
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
    count = len(storage.list("freight_negotiation_events", {"thread_id": thread_id, "event_type": "counter"}, order="", limit=10000))
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
    return round(target_rpm * all_in / 25) * 25 if target_rpm and all_in else None


def _auto_send_blockers(load: dict, mission: dict, profile: dict) -> list[str]:
    blockers: list[str] = []
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
    if (mission.get("pickup_start") or mission.get("pickup_end")) and not load.get("pickup_date_verified"):
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
    return storage.insert("freight_drafts", {
        "id": new_id(), "thread_id": thread_id, "in_reply_to_message_id": in_reply_to,
        "subject": subject, "body_text": body, "reason": reason, "policy_snapshot": policy,
        "status": "pending", "created_at": now_iso(), "updated_at": now_iso(),
    })


def _already_sent_reply(thread_id: str, body: str, reason: str, storage=None) -> bool:
    """Avoid repeating a counter or profile answer already sent in this conversation."""
    storage = storage or store
    if reason.startswith("counter_"):
        amount = extract_offer(body)
        if amount is not None:
            counters = storage.list("freight_negotiation_events", {"thread_id": thread_id, "event_type": "counter"}, order="", limit=1000)
            if any(_number(event.get("amount")) == amount for event in counters):
                return True
    normalized = " ".join(body.casefold().split())
    if reason in {"profile_fact_reply", "clarify_load_details", "agent_suggested_reply"}:
        messages = storage.list("freight_messages", {"thread_id": thread_id, "direction": "out"}, order="created_at desc", limit=100)
        return any(normalized in " ".join((message.get("body_text") or "").casefold().split()) for message in messages)
    return False


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
        _record_negotiation_event(thread["id"], draft_id, "counter", counter_amount, {"reason": draft.get("reason"), "transport": "local"}, storage)
    next_state = "negotiating" if counter_amount is not None else "passed" if draft.get("reason") == "pass_below_floor" else "waiting"
    storage.update("freight_threads", thread["id"], {"last_message_id": message_id, "state": next_state, "last_activity_at": stamp, "updated_at": stamp})
    if load.get("id"):
        storage.update("freight_loads", load["id"], {"status": next_state, "updated_at": stamp})
        _counter_count(thread["id"], load, storage)
    return {"message_id": message_id, "transport": "local"}


def _finish_permitted_draft(draft: dict, mission: dict, storage=None, *, transport: str = "gmail") -> dict[str, Any]:
    """Auto-send only the narrow reply types explicitly enabled on a mission."""
    storage = storage or store
    permissions = mission.get("permissions") or {}
    reason = draft.get("reason") or ""
    policy = draft.get("policy_snapshot") or {}
    if not mission.get("active", True) or auto_lane_issue(mission):
        return {"action": "draft", "draft": draft}
    facts_permitted = not policy.get("includes_profile_answers") or permissions.get("auto_profile_reply")
    permitted = (
        (reason == "profile_fact_reply" and permissions.get("auto_profile_reply"))
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
    _supersede_pending_drafts(thread["id"], storage)
    load = storage.get("freight_loads", thread["load_id"]) or {}
    mission = storage.get("freight_missions", load.get("mission_id")) or {}
    profile_id = load.get("truck_profile_id") or mission.get("truck_profile_id")
    profile = storage.get("freight_truck_profiles", profile_id) or {}
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
    storage.update("freight_messages", message["id"], {"classification": classification})
    if classification.get("offer") is not None and not classification["ambiguous_offer"]:
        _record_negotiation_event(thread["id"], message["id"], "offer", classification["offer"], {"source": "total_rate"}, storage)
    protected = classification["protected"]
    if protected:
        labels = {
            "call_requested": "Broker requested a call.",
            "sensitive_driver_info": "Broker requested sensitive driver information.",
            "rate_confirmation": "Broker mentioned or sent a rate confirmation.",
            "price_accepted": "Broker appears to have accepted or confirmed a price.",
        }
        summary = " ".join(labels[item] for item in protected)
        _create_alert(thread["id"], protected[0], summary, storage)
        next_state = "booked" if prior_state == "booked" else "accepted_pending_review" if {"rate_confirmation", "price_accepted"} & set(protected) else "protected_review"
        _set_stage(thread, load, next_state, storage)
        return {"action": "alert", "classification": classification, "summary": summary}

    if classification.get("intent") == "closed" or (classification.get("source") != "gemini" and CLOSED_REPLY.search(text)):
        summary = "Broker says the load is unavailable or this conversation is finished."
        _create_alert(thread["id"], "thread_closed", summary, storage)
        _set_stage(thread, load, "closed", storage)
        return {"action": "closed", "classification": classification, "summary": summary}

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
            draft = _create_draft(thread["id"], thread["subject"], "Can you confirm the loaded miles and total all-in rate?", "clarify_rate", {"manual_only": True}, message.get("provider_message_id") or "", storage)
            _create_alert(thread["id"], "rate_per_mile_unverified", summary, storage)
            _set_stage(thread, load, "needs_attention", storage)
            return {"action": "draft", "draft": draft, "classification": classification, "summary": summary}

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

    shareable = set(profile.get("shareable_fields") or [])
    answers: list[str] = []
    missing: list[str] = []
    for question in classification["questions"]:
        if question == "team_status":
            if profile.get("team_status") and "team_status" in shareable:
                value = str(profile["team_status"]).lower()
                answers.append("Yes, this is a true team." if value in {"team", "true team", "yes"} else f"This is a {profile['team_status']} truck.")
            else:
                missing.append("team status")
        elif question == "equipment_type":
            equipment = profile.get("equipment_type") or mission.get("equipment_type")
            if equipment and "equipment_type" in shareable:
                length = profile.get("trailer_length_ft") or mission.get("trailer_length_ft")
                answers.append(f"We have a {length} ft {equipment}." if length else f"We have a {equipment}.")
            else:
                missing.append("equipment type")
        elif question == "mc_or_dot":
            pieces = []
            if profile.get("mc_number") and "mc_number" in shareable: pieces.append(f"MC {profile['mc_number']}")
            if profile.get("dot_number") and "dot_number" in shareable: pieces.append(f"DOT {profile['dot_number']}")
            if pieces: answers.append(" / ".join(pieces))
            else: missing.append("MC/DOT")
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
            draft = _create_draft(thread["id"], thread["subject"], "What is the delivery city and state for this load?", "clarify_destination", {"manual_only": True}, message.get("provider_message_id") or "", storage)
            _create_alert(thread["id"], "destination_needed", summary, storage)
            _set_stage(thread, load, "needs_attention", storage)
            return {"action": "draft", "draft": draft, "classification": classification, "summary": summary}
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
            summary = f"Broker offered {_money(offer)}, which meets the configured envelope. Review before accepting."
            _create_alert(thread["id"], "price_at_or_above_target", summary, storage)
            _set_stage(thread, load, "offer_review", storage)
            return {"action": "alert", "classification": classification, "summary": summary}
        if reason.startswith("counter_") and max_rounds and current_round >= max_rounds:
            summary = f"Counter limit reached. Broker offered {_money(offer)}."
            _create_alert(thread["id"], "counter_limit", summary, storage)
            _set_stage(thread, load, "needs_attention", storage)
            return {"action": "alert", "classification": classification, "summary": summary}
        if answers:
            body = " ".join(answers + [body])
        blockers = _auto_send_blockers(load, mission, profile)
        policy = {"offer": offer, "floor": floor, "counter": counter, "includes_profile_answers": bool(answers), "safe_to_auto_send": not blockers, "auto_send_blockers": blockers, **economics}
        if blockers and classification.get("source") == "gemini":
            requested = [item for item in blockers if item not in {"active truck profile and mission", "truck weight capacity"}]
            if "pickup and delivery schedule" in requested:
                previous = storage.list("freight_messages", {"thread_id": thread["id"], "direction": "in"}, order="created_at asc", limit=100)
                readings = [row.get("classification") or {} for row in previous if row["id"] != message["id"]] + [classification]
                has_pickup = any(row.get("pickup_schedule_evidence") for row in readings)
                has_delivery = any(row.get("delivery_schedule_evidence") for row in readings)
                requested.remove("pickup and delivery schedule")
                if not has_pickup:
                    requested.append("pickup appointment time or window")
                if not has_delivery:
                    requested.append("delivery appointment time or window")
            if requested:
                groups = {
                    "broker pickup city": "pickup city and state",
                    "broker destination": "delivery city and state",
                    "broker equipment": "required trailer",
                    "pickup and delivery schedule": "pickup and delivery times",
                    "broker pickup date": "pickup date",
                    "broker load weight": "load weight",
                    "broker loaded miles": "loaded miles",
                    "actual deadhead miles": "deadhead miles",
                    "pickup appointment time or window": "pickup appointment time or window",
                    "delivery appointment time or window": "delivery appointment time or window",
                }
                details = [groups.get(item, item) for item in requested]
                body = " ".join(answers + ["Thanks for the rate. Could you send the remaining load details: " + ", ".join(details) + "?"])
                reason = "clarify_load_details"
                policy["manual_only"] = True
        elif reason.startswith("counter_") and classification.get("source") == "gemini":
            try:
                previous = storage.list("freight_messages", {"thread_id": thread["id"]}, order="created_at asc", limit=100)
                phrasing = compose_counter_reply(text, [row for row in previous if row["id"] != message["id"]], counter)
                body = " ".join(answers + [phrasing])
            except Exception as exc:
                logger.warning("Counter phrasing failed for thread %s: %s", thread["id"], type(exc).__name__)
        if _already_sent_reply(thread["id"], body, reason, storage):
            summary = "The agent already sent this response. Review the broker's new message before replying again."
            _create_alert(thread["id"], "repeated_reply", summary, storage)
            _set_stage(thread, load, "needs_attention", storage)
            return {"action": "alert", "classification": classification, "summary": summary}
        draft = _create_draft(thread["id"], thread["subject"], body, reason, policy, message.get("provider_message_id") or "", storage)
        if blockers:
            _create_alert(thread["id"], "verify_load_facts", f"Review before sending: verify {', '.join(blockers)}.", storage)
        _set_stage(thread, load, "draft_ready", storage)
        return {"classification": classification, **_finish_permitted_draft(draft, mission, storage, transport=transport)}

    if answers:
        body = " ".join(answers)
        if _already_sent_reply(thread["id"], body, "profile_fact_reply", storage):
            summary = "The agent already answered this truck question. Review the broker's follow-up before replying again."
            _create_alert(thread["id"], "repeated_reply", summary, storage)
            _set_stage(thread, load, "needs_attention", storage)
            return {"action": "alert", "classification": classification, "summary": summary}
        draft = _create_draft(thread["id"], thread["subject"], body, "profile_fact_reply", {}, message.get("provider_message_id") or "", storage)
        _set_stage(thread, load, "draft_ready", storage)
        return {"classification": classification, **_finish_permitted_draft(draft, mission, storage, transport=transport)}

    suggested = classification.get("suggested_reply") or ""
    if suggested and classification.get("source") == "gemini":
        if _already_sent_reply(thread["id"], suggested, "agent_suggested_reply", storage):
            summary = "The agent already sent this response. Review the broker's follow-up before replying again."
            _create_alert(thread["id"], "repeated_reply", summary, storage)
            _set_stage(thread, load, "needs_attention", storage)
            return {"action": "alert", "classification": classification, "summary": summary}
        draft = _create_draft(thread["id"], thread["subject"], suggested, "agent_suggested_reply", {"manual_only": True, "agent_summary": classification.get("summary")}, message.get("provider_message_id") or "", storage)
        _set_stage(thread, load, "draft_ready", storage)
        return {"action": "draft", "draft": draft, "classification": classification, "summary": classification.get("summary") or "Review the suggested reply."}
    summary = "The broker reply needs review because it did not match a permitted reply type."
    _create_alert(thread["id"], "ambiguous_reply", summary, storage)
    _set_stage(thread, load, "needs_attention", storage)
    return {"action": "alert", "classification": classification, "summary": summary}


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
        _record_negotiation_event(thread["id"], draft_id, "counter", counter_amount, {"reason": draft.get("reason")}, storage)
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
    candidates = [thread for thread in storage.list("freight_threads", {"sender_account": sender_account}, order="updated_at desc", limit=500) if thread.get("state") != "closed"]
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


def poll_freight_replies(storage=None) -> dict[str, int]:
    storage = storage or store
    active_threads = storage.list("freight_threads", order="updated_at desc", limit=1)
    if not active_threads:
        return {"accounts": 0, "messages": 0, "matched": 0}
    cfg = storage.get("settings", 1) or {}
    account_ids = {str(row.get("sender_account")) for row in storage.list("freight_threads", order="", limit=1000) if row.get("state") != "closed"}
    senders = [row for row in list_gmail_senders(active_only=True, storage=storage, cfg=cfg) if str(row.get("id")) in account_ids and row.get("provider", "gmail") == "gmail"]
    totals = {"accounts": 0, "messages": 0, "matched": 0}
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

                threads_for_account = [t for t in storage.list("freight_threads", {"sender_account": account_id}, order="updated_at desc", limit=500) if t.get("state") != "closed"]
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
                        "body_text": _message_text(parsed), "classification": {}, "status": "received", "created_at": created,
                    })
                    stamp = now_iso()
                    storage.update("freight_threads", thread["id"], {"last_message_id": provider_id, "last_imap_uid": uid, "last_activity_at": stamp, "updated_at": stamp})
                    evaluate_inbound({**thread, "last_message_id": provider_id}, incoming, storage)
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
