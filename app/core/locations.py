"""Small, deterministic helpers for US city/state handling."""
from __future__ import annotations

import re


STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
STATE_CODES = frozenset(STATE_NAMES.values())
STATE_NAME_BY_CODE = {code: name for name, code in STATE_NAMES.items()}


def clean_location_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" ,")


def normalize_state(value: object) -> str | None:
    raw = clean_location_text(value)
    upper = raw.upper()
    if upper in STATE_CODES:
        return upper
    return STATE_NAMES.get(raw.casefold())


def split_city_state(value: object) -> tuple[str, str] | None:
    """Accept `Dallas, TX`, `Dallas TX`, and full state names."""
    raw = clean_location_text(value)
    if not raw:
        return None
    if "," in raw:
        city, state_text = (part.strip() for part in raw.rsplit(",", 1))
        state = normalize_state(state_text)
        return (city, state) if city and state else None
    folded = raw.casefold()
    suffixes = sorted(
        [(name, code) for name, code in STATE_NAMES.items()] + [(code.casefold(), code) for code in STATE_CODES],
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for suffix, code in suffixes:
        marker = " " + suffix
        if folded.endswith(marker):
            city = raw[:-len(marker)].strip(" ,")
            if city:
                return city, code
    return None


def normalize_destination(label: object, kind: object) -> tuple[str, str]:
    """Correct the common city/state selector mismatch without guessing."""
    cleaned = clean_location_text(label)
    normalized_kind = str(kind or "city").strip().lower()
    if normalized_kind not in {"city", "state", "region", "anywhere"}:
        normalized_kind = "city"
    if normalized_kind not in {"city", "state"}:
        return cleaned, normalized_kind
    state = normalize_state(cleaned)
    if state:
        return state, "state"
    city_state = split_city_state(cleaned)
    if city_state:
        city, state = city_state
        return f"{city}, {state}", "city"
    return cleaned, normalized_kind


def location_is_explicit(city: object, state: object, evidence: object) -> bool:
    """Require separate city and state evidence; never accept embedded letters."""
    city_text = clean_location_text(city)
    state_code = normalize_state(state)
    evidence_text = str(evidence or "")
    if not city_text or not state_code or not evidence_text:
        return False
    city_pattern = re.compile(rf"(?<![A-Za-z]){re.escape(city_text)}(?![A-Za-z])", re.I)
    city_spans = [match.span() for match in city_pattern.finditer(evidence_text)]
    if not city_spans:
        return False
    state_terms = [state_code, STATE_NAME_BY_CODE[state_code]]
    state_spans = []
    for term in state_terms:
        pattern = re.compile(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", re.I)
        state_spans.extend(match.span() for match in pattern.finditer(evidence_text))
    return any(span not in city_spans for span in state_spans)
