"""US state -> IANA timezone.

Used to land each email inside the *recipient's* business hours. A handful of
states straddle two zones (TX, FL, TN, KY, IN, MI, ND, SD, NE, KS, OR, ID);
we take the zone the large majority of the population lives in, which is close
enough for "don't email someone at 6am". Zip-level precision would need a
lookup table and is not worth it here.
"""
from zoneinfo import ZoneInfo

ET, CT, MT, PT = "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"

STATE_TZ = {
    "CT": ET, "DE": ET, "DC": ET, "FL": ET, "GA": ET, "IN": ET, "ME": ET, "MD": ET,
    "MA": ET, "MI": ET, "NH": ET, "NJ": ET, "NY": ET, "NC": ET, "OH": ET, "PA": ET,
    "RI": ET, "SC": ET, "VT": ET, "VA": ET, "WV": ET,
    "AL": CT, "AR": CT, "IL": CT, "IA": CT, "KS": CT, "KY": CT, "LA": CT, "MN": CT,
    "MS": CT, "MO": CT, "NE": CT, "ND": CT, "OK": CT, "SD": CT, "TN": CT, "TX": CT,
    "WI": CT,
    "AZ": "America/Phoenix", "CO": MT, "ID": MT, "MT": MT, "NM": MT, "UT": MT, "WY": MT,
    "CA": PT, "NV": PT, "OR": PT, "WA": PT,
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}

_cache: dict[str, ZoneInfo] = {}


def tz_for_state(state: str | None, default: str = CT) -> ZoneInfo:
    key = (state or "").strip().upper()[:2]
    name = STATE_TZ.get(key, default)
    if name not in _cache:
        try:
            _cache[name] = ZoneInfo(name)
        except Exception:
            _cache[name] = ZoneInfo("UTC")
    return _cache[name]


def label_for_state(state: str | None) -> str:
    name = STATE_TZ.get((state or "").strip().upper()[:2], "")
    return {ET: "ET", CT: "CT", MT: "MT", PT: "PT",
            "America/Phoenix": "AZ", "America/Anchorage": "AK",
            "Pacific/Honolulu": "HI"}.get(name, "?")
