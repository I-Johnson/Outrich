"""Extension point for state public-record sources.

Texas uses the Texas Comptroller public Active Sales Tax Permit Holders open data set (data.texas.gov, jrea-zgmq).
Filtered to construction sector NAICS codes (23xxxx) organized as LLC (org type CL).
"""
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class RecordCandidate:
    business_name: str
    website: str = ""
    phone: str = ""
    city: str = ""
    state: str = ""
    source_detail: str = "public_records"


class PublicRecordsAdapter:
    state: str = ""
    available: bool = False
    def search(self, category: str, city: str, limit: int) -> list[RecordCandidate]:
        return []


CATEGORY_TO_NAICS = {
    "roofing": "238160", "roofers": "238160", "roofer": "238160",
    "plumbing": "238220", "plumbers": "238220", "plumber": "238220",
    "hvac": "238220", "heating": "238220", "air conditioning": "238220",
    "electrical": "238210", "electricians": "238210", "electrician": "238210",
    "concrete": "238110", "masonry": "238140", "foundations": "238110",
    "framing": "238130", "carpentry": "238350",
    "drywall": "238310", "insulation": "238310",
    "painting": "238320", "painters": "238320",
    "flooring": "238330", "tile": "238340",
    "remodeling": "236118", "remodelers": "236118",
    "general contractor": "236118", "general contractors": "236118",
    "home builder": "236115", "builders": "236115",
    "landscaping": "238990", "landscapers": "238990",
}


class TexasAdapter(PublicRecordsAdapter):
    state = "TX"
    available = True
    SODA_URL = "https://data.texas.gov/resource/jrea-zgmq.json"

    def search(self, category: str, city: str, limit: int) -> list[RecordCandidate]:
        clean_cat = category.strip().lower()
        naics = CATEGORY_TO_NAICS.get(clean_cat)
        city_clean = city.strip().upper()

        where_clauses = [
            "taxpayer_organization_type='CL'",  # Texas LLC
            "outlet_state='TX'",
        ]
        if city_clean:
            where_clauses.append(f"upper(outlet_city)='{city_clean}'")

        if naics:
            where_clauses.append(f"outlet_naics_code='{naics}'")
        else:
            where_clauses.append("outlet_naics_code >= 230000 AND outlet_naics_code <= 239999")

        params = {
            "$select": "taxpayer_number,taxpayer_name,outlet_name,outlet_address,outlet_city,outlet_state,outlet_zip_code,outlet_permit_issue_date,outlet_naics_code",
            "$where": " AND ".join(where_clauses),
            "$order": "outlet_permit_issue_date DESC",
            "$limit": min(max(limit, 1), 100),
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                res = client.get(self.SODA_URL, params=params)
                if res.status_code != 200:
                    logger.warning("Texas Comptroller SODA returned %s: %s", res.status_code, res.text[:200])
                    return []
                data = res.json()
        except Exception as exc:
            logger.warning("Failed querying Texas Comptroller SODA: %s", exc)
            return []

        candidates = []
        seen = set()
        for row in data:
            name = (row.get("outlet_name") or row.get("taxpayer_name") or "").strip()
            tn = row.get("taxpayer_number")
            if not name or (tn and tn in seen):
                continue
            seen.add(tn)
            candidates.append(
                RecordCandidate(
                    business_name=name,
                    city=row.get("outlet_city", city).title(),
                    state="TX",
                    source_detail="texas_comptroller_llc",
                )
            )
        return candidates


class CaliforniaAdapter(PublicRecordsAdapter): state = "CA"

ADAPTERS = {"TX": TexasAdapter(), "CA": CaliforniaAdapter()}


def label_for_state(state: str) -> str:
    adapter = ADAPTERS.get(state)
    return "records + search" if adapter and adapter.available else "search only"

