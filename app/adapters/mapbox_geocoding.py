"""Mapbox Geocoding helper for city/state/zip autocomplete in discovery."""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def search_locations(query: str, limit: int = 6) -> list[dict[str, str]]:
    """Searches Mapbox Geocoding API for US places, localities, or postcodes."""
    query = (query or "").strip()
    if not query or len(query) < 2:
        return []

    token = settings.MAPBOX_ACCESS_TOKEN
    if not token:
        logger.warning("MAPBOX_ACCESS_TOKEN is not configured")
        return []

    url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{quote(query)}.json"
    params = {
        "access_token": token,
        "autocomplete": "true",
        "types": "place,locality,postcode",
        "country": "us",
        "limit": min(max(limit, 1), 10),
    }
    # Referer required by token's domain-restriction policy
    headers = {
        "Referer": "https://www.contractorops.ai",
        "User-Agent": settings.CRAWLER_USER_AGENT,
    }

    try:
        with httpx.Client(timeout=10) as client:
            res = client.get(url, params=params, headers=headers)
            if res.status_code != 200:
                logger.warning("Mapbox API returned status %s: %s", res.status_code, res.text)
                return []
            data = res.json()

        results: list[dict[str, str]] = []
        for f in data.get("features", []):
            city = f.get("text", "")
            state = ""
            for ctx in f.get("context", []):
                cid = ctx.get("id", "")
                if cid.startswith("region."):
                    code = str(ctx.get("short_code") or "")
                    state = code.split("-")[1] if "-" in code else ctx.get("text", "")

            # If it's a postcode, place_name often has city/state
            place_type = f.get("place_type", [""])[0]
            if place_type == "postcode":
                formatted = f"{city}, {state}" if state else city
            elif state:
                formatted = f"{city}, {state}"
            else:
                formatted = city

            results.append({
                "label": f.get("place_name", formatted),
                "value": formatted,
                "city": city,
                "state": state,
            })
        return results
    except Exception as exc:
        logger.error("Mapbox search failed for %s: %s", query, exc)
        return []
