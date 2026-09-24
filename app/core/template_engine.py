from __future__ import annotations

import html
import re

from app.core.leads import infer_name_from_email

VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
ALLOWED_VARIABLES = {
    "short_name", "owner_first_name", "person_name", "category", "city", "state", "sender_name", "sender_email",
    "sender_business", "sender_business_url", "product_name", "product_url", "booking_link", "calendar_link",
    "callback_number", "client_count", "client_noun", "email_signature", "signature",
    "origin", "destination", "pickup_date", "equipment", "truck_location", "mc_number", "dot_number",
    "broker_company", "dat_reference", "loaded_miles", "deadhead_miles", "posted_rate", "all_in_rpm",
}
ALLOWED_VARIABLES_LOWER = {v.lower() for v in ALLOWED_VARIABLES}


def render_template(template: str, lead: dict, business: dict, *, html_escape: bool = False) -> tuple[str, list[str]]:
    context = {**business, **lead}
    sig = business.get("email_signature") or business.get("signature") or lead.get("signature") or lead.get("email_signature") or ""
    context["email_signature"] = sig
    context["signature"] = sig
    book_link = context.get("booking_link") or context.get("calendar_link") or ""
    context["booking_link"] = book_link
    context["calendar_link"] = book_link
    short = context.get("short_name") or context.get("business_name") or ""
    inferred_person = (
        context.get("owner_first_name")
        or infer_name_from_email(context.get("email"), context.get("business_name", ""), context.get("domain", ""))
    )
    if short:
        company_team = short if short.lower().endswith(("team", "crew", "staff")) else f"{short} Team"
    else:
        company_team = "there"

    context["person_name"] = inferred_person or company_team
    context["owner_first_name"] = inferred_person or company_team
    context["short_name"] = short or "there"
    context["category"] = context.get("category") or "contractor"
    context["city"] = context.get("city") or "your area"
    context["state"] = context.get("state") or ""
    missing: list[str] = []
    def replace(match: re.Match) -> str:
        raw_key = match.group(1)
        value = context.get(raw_key)
        if value is None or str(value).strip() == "":
            value = context.get(raw_key.lower())
        if value is None or str(value).strip() == "":
            if raw_key.lower() in ("state", "zip", "notes", "outreach_angle"):
                return ""
            missing.append(raw_key); return ""
        return html.escape(str(value)) if html_escape else str(value)
    rendered = VAR_RE.sub(replace, template)
    rendered = re.sub(r"\s*[\u2014\u2013]\s*", ", ", rendered)
    return rendered, sorted(set(missing))


def unknown_variables(template: str) -> list[str]:
    return sorted({v for v in VAR_RE.findall(template) if v.lower() not in ALLOWED_VARIABLES_LOWER})
