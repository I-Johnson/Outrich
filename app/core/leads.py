from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import dns.resolver
import phonenumbers

LEGAL_SUFFIX = re.compile(
    r"(?:[\s,.-]+(?:L\.?L\.?C\.?|INC(?:ORPORATED)?\.?|CORP(?:ORATION)?\.?|CO\.?|COMPANY|LTD\.?|L\.?L\.?P\.?|P\.?L\.?L\.?C\.?))+$",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$", re.I)


def short_name(value: str, dba: str | None = None) -> str:
    name = (dba or value or "").strip()
    name = re.sub(r"^.*?\s+(?:d/?b/?a|doing business as)\s+(.+)$", r"\1", name, flags=re.I)
    name = LEGAL_SUFFIX.sub("", name).rstrip(" ,.")
    name = re.sub(r"\s+", " ", name)
    if name and name == name.upper():
        name = name.title().replace("Hvac", "HVAC").replace("Hvac", "HVAC")
    return name


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def valid_email(value: str | None, check_mx: bool = False) -> bool:
    email = normalize_email(value)
    if not EMAIL_RE.fullmatch(email): return False
    if not check_mx: return True
    try:
        return bool(dns.resolver.resolve(email.rsplit("@", 1)[1], "MX", lifetime=3))
    except Exception:
        return False


def normalize_phone(value: str | None, region: str = "US") -> str:
    if not value: return ""
    try:
        number = phonenumbers.parse(value, region)
        return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164) if phonenumbers.is_valid_number(number) else ""
    except phonenumbers.NumberParseException:
        return ""


def normalize_website(value: str | None) -> tuple[str, str]:
    raw = (value or "").strip()
    if not raw: return "", ""
    url = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(url); domain = (parsed.hostname or "").lower().removeprefix("www.")
    return (f"{parsed.scheme or 'https'}://{parsed.netloc}{parsed.path.rstrip('/')}", domain) if domain else ("", "")


def duplicate_reason(row: dict, existing: list[dict]) -> str:
    email = normalize_email(row.get("email")); domain = (row.get("domain") or "").lower()
    for item in existing:
        if email and normalize_email(item.get("email")) == email: return "duplicate_email"
        if domain and (item.get("domain") or "").lower() == domain: return "duplicate_domain"
    return ""


GENERIC_EMAIL_PREFIXES = {
    "info", "contact", "contactus", "sales", "support", "admin", "administrator",
    "office", "hello", "mail", "help", "helpdesk", "team", "service", "services",
    "billing", "estimate", "estimates", "quote", "quotes", "bid", "bids",
    "lead", "leads", "marketing", "inquiry", "inquiries", "general", "customercare",
    "customer", "customerservice", "jobs", "careers", "career", "press", "media",
    "reception", "dispatch", "scheduling", "schedule", "frontdesk", "frontoffice",
    "accounting", "accounts", "accountspayable", "inbox", "feedback", "operations",
    "ops", "webmaster", "postmaster", "hostmaster", "noreply", "no-reply", "root",
    "privacy", "legal", "security", "contractor", "contractors", "management",
    "manager", "hr", "humanresources", "staff", "desk", "crew", "pro", "pros",
    "business", "mailroom", "client", "clients", "orders", "order", "shop", "store",
    "tech", "work", "corp", "commercial", "residential", "booking", "bookings",
    "hi", "hey", "web", "online", "inquire", "ask", "generalinfo", "user", "users",
}


_FIRST_NAMES_SET: set[str] | None = None


def _get_first_names() -> set[str]:
    global _FIRST_NAMES_SET
    if _FIRST_NAMES_SET is None:
        names_file = Path(__file__).parent / "first_names.json"
        if names_file.exists():
            try:
                _FIRST_NAMES_SET = set(json.loads(names_file.read_text()))
            except Exception:
                _FIRST_NAMES_SET = set()
        else:
            _FIRST_NAMES_SET = set()
    return _FIRST_NAMES_SET


def infer_name_from_email(email: str | None, business_name: str = "", domain: str = "") -> str | None:
    if not email or "@" not in email:
        return None
    local = email.split("@")[0].strip().lower()
    if not local:
        return None
    base_local = re.sub(r"[\d_-]+$", "", local)
    if local in GENERIC_EMAIL_PREFIXES or base_local in GENERIC_EMAIL_PREFIXES:
        return None
    tokens = [t for t in re.split(r"[._\-+]", local) if t]
    if not tokens:
        return None
    cand = re.sub(r"\d+$", "", tokens[0])
    if not cand.isalpha() or len(cand) < 2 or len(cand) > 20:
        return None
    if cand in GENERIC_EMAIL_PREFIXES:
        return None
    domain_stem = domain.split(".")[0].lower() if domain else ""
    if domain_stem and cand == domain_stem:
        return None
    biz_words = {w.lower() for w in re.split(r"[\s,._\-]+", business_name) if len(w) > 2}
    if len(biz_words) == 1 and cand in biz_words and len(cand) > 5:
        return None
    names_set = _get_first_names()
    if names_set and cand not in names_set:
        return None
    return cand.capitalize()

