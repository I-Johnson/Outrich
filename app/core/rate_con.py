"""Deterministic rate confirmation PDF parsing.

Conservative by design: a field is only filled when a labeled pattern matches.
Anything unreadable stays absent and the dispatcher enters it by hand.
"""
from __future__ import annotations

import io
import re

from pypdf import PdfReader

_AMOUNT = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?\b")
_DATE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})\b", re.I)
_CITY_STATE = re.compile(r"\b([A-Z][A-Za-z .'-]{1,40}?),\s*([A-Z]{2})\b")
_WEIGHT = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d+)\s*(?:lbs?|pounds)\b", re.I)
_EQUIPMENT = re.compile(r"\b(dry van|reefer|refrigerated|flatbed|step\s*deck|power only|box truck|hotshot|conestoga|van)\b", re.I)


def _pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for page in reader.pages[:5]:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _labeled(text: str, labels: str) -> str:
    match = re.search(labels + r"[^\n:]*[:\-]?\s*(.+)", text, re.I)
    return match.group(1).strip() if match else ""


def _section(text: str, start: str, stops: tuple[str, ...]) -> str:
    match = re.search(start, text, re.I)
    if not match:
        return ""
    rest = text[match.start():]
    cut = len(rest)
    for stop in stops:
        stop_match = re.search(stop, rest[3:], re.I)
        if stop_match:
            cut = min(cut, 3 + stop_match.start())
    return rest[:cut][:400]


def parse_rate_con_pdf(pdf_bytes: bytes) -> dict:
    """Extract booking fields from a rate confirmation PDF."""
    try:
        text = _pdf_text(pdf_bytes)
    except Exception as exc:
        raise ValueError("Unreadable PDF") from exc
    if len(text.strip()) < 20:
        raise ValueError("No readable text in the PDF")

    result: dict = {}

    total_line = _labeled(text, r"(?:total(?:\s+rate|\s+amount|\s+pay)?|rate\s+confirmation\s+amount|all[\s-]*in\s+rate|line\s*haul)")
    amounts = _AMOUNT.findall(total_line) if total_line else []
    if not amounts:
        labeled_amounts = re.findall(r"(?:total|rate|amount|pay)[^\n$]{0,30}(\$\s*\d{1,3}(?:,\d{3})+|\$\s*\d+)", text, re.I)
        amounts = [re.sub(r"[^0-9,]", "", item) for item in labeled_amounts]
    if amounts:
        result["total_rate"] = float(amounts[0].replace(",", ""))

    pickup = _section(text, r"\b(?:shipper|pick\s*up|pickup|origin)\b", (r"\bdeliver", r"\bconsignee", r"\bdrop\b"))
    delivery = _section(text, r"\b(?:consignee|deliver\w*|destination|drop(?:\s*off)?)\b", (r"\bpick\s*up", r"\bshipper\b", r"\bequipment", r"\brate\b"))
    for key, section in (("pickup", pickup), ("delivery", delivery)):
        city = _CITY_STATE.search(section)
        if city:
            result[f"{key}_city"] = city.group(1).strip()
            result[f"{key}_state"] = city.group(2)
    pickup_date = _DATE.search(pickup)
    if pickup_date:
        result["pickup_date"] = pickup_date.group(1)

    equipment = _EQUIPMENT.search(text)
    if equipment:
        result["equipment"] = equipment.group(1).lower()
    weight = _WEIGHT.search(text)
    if weight:
        result["weight_lbs"] = float(weight.group(1).replace(",", ""))

    if not result:
        raise ValueError("Could not read any booking fields from the PDF")
    return result
