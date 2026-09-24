from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from app.adapters.public_records import ADAPTERS
from app.config import settings
from app.core.leads import duplicate_reason, normalize_email, normalize_phone, normalize_website, short_name, valid_email
from app.db import new_id, now_iso, store

EMAIL_FIND = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_FIND = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
SKIP_DOMAINS = {
    "angi.com", "bbb.org", "facebook.com", "google.com", "homeadvisor.com",
    "houzz.com", "indeed.com", "instagram.com", "linkedin.com", "mapquest.com",
    "maps.google.com", "nextdoor.com", "porch.com", "reddit.com", "realtor.com",
    "thumbtack.com", "tiktok.com", "wikipedia.org", "yellowpages.com", "yelp.com",
    "youtube.com", "zillow.com",
}


def serp_candidates(category: str, city: str, state: str, limit: int) -> tuple[list[dict], int]:
    adapter = ADAPTERS.get(state)
    records = adapter.search(category, city, limit) if adapter and adapter.available else []
    candidates: list[dict] = []
    calls = 0
    if settings.SERP_API_KEY:
        if settings.SERP_PROVIDER != "serper":
            raise ValueError(f"Unsupported SERP_PROVIDER: {settings.SERP_PROVIDER}")
        with httpx.Client(timeout=30) as client:
            response = client.post(
                "https://google.serper.dev/maps",
                headers={"X-API-KEY": settings.SERP_API_KEY, "Content-Type": "application/json"},
                json={"q": f"{category} in {city}, {state}", "num": min(limit, 100)},
            )
        calls = 1
        if response.status_code == 429:
            raise RuntimeError("SERP quota or rate limit reached")
        response.raise_for_status()
        for place in response.json().get("places", [])[:limit]:
            candidates.append(
                {
                    "business_name": place.get("title", ""),
                    "website": place.get("website", ""),
                    "phone": place.get("phoneNumber", ""),
                    "city": city,
                    "state": state,
                    "source_detail": "serper_maps",
                }
            )
    candidates.extend(vars(record) for record in records)
    deduped: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = re.sub(r"\W+", "", str(candidate.get("business_name") or "").lower())
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
        if len(deduped) >= limit:
            break
    return deduped, calls


def _serper_search(query: str, *, count: int = 4) -> list[dict]:
    if not settings.SERP_API_KEY:
        return []
    try:
        with httpx.Client(timeout=10) as client:
            res = client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": settings.SERP_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "num": count},
            )
        if res.status_code == 429:
            raise RuntimeError("SERP quota or rate limit reached")
        res.raise_for_status()
        return res.json().get("organic", [])
    except RuntimeError:
        raise
    except Exception:
        return []


def find_company_website(company: str, city: str, state: str) -> tuple[str, int]:
    if not settings.SERP_API_KEY:
        return "", 0
    organic = _serper_search(f"{company} {city} {state}")
    for result in organic:
        link = result.get("link", "")
        _, domain = normalize_website(link)
        if domain and not any(skipped in domain for skipped in SKIP_DOMAINS):
            return link, 1
    return "", 1


def fallback_contact_search(company: str, city: str, state: str) -> tuple[str, str, str, int]:
    """Use one fallback SERP query to recover public email/phone details."""
    if not settings.SERP_API_KEY:
        return "", "", "", 0
    organic = _serper_search(f'"{company}" email phone {city} {state}', count=6)
    text = " ".join(
        f"{result.get('title', '')} {result.get('snippet', '')}"
        for result in organic
    )
    email = next((normalize_email(value) for value in EMAIL_FIND.findall(text) if valid_email(value)), "")
    phone = next((normalize_phone(value) for value in PHONE_FIND.findall(text) if normalize_phone(value)), "")
    website = ""
    for result in organic:
        link = result.get("link", "")
        _, domain = normalize_website(link)
        if domain and not any(skipped in domain for skipped in SKIP_DOMAINS):
            website = link
            break
    return email, phone, website, 1


def crawl_contact(website: str) -> tuple[str, str]:
    website, domain = normalize_website(website)
    if not website or any(sd in domain for sd in SKIP_DOMAINS):
        return "", ""
    origin = f"{urlparse(website).scheme}://{urlparse(website).netloc}"
    robot = RobotFileParser()
    robot.set_url(urljoin(origin, "/robots.txt"))
    emails, phones = [], []
    with httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": settings.CRAWLER_USER_AGENT}) as client:
        try:
            robots_response = client.get(urljoin(origin, "/robots.txt"))
            robot.parse(robots_response.text.splitlines() if robots_response.status_code == 200 else [])
        except Exception:
            robot.parse([])
        paths = list(dict.fromkeys((website, urljoin(origin, "/contact"), urljoin(origin, "/about"))))
        for index, path in enumerate(paths):
            if not robot.can_fetch(settings.CRAWLER_USER_AGENT, path):
                continue
            try:
                response = client.get(path)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if content_type and "html" not in content_type:
                    continue
                soup = BeautifulSoup(response.text[:1_000_000], "html.parser")
                text = soup.get_text(" ")
                emails.extend(EMAIL_FIND.findall(text))
                phones.extend(PHONE_FIND.findall(text))
                emails.extend(anchor.get("href", "")[7:].split("?", 1)[0] for anchor in soup.select('a[href^="mailto:"]'))
                phones.extend(anchor.get("href", "")[4:] for anchor in soup.select('a[href^="tel:"]'))
                if index < len(paths) - 1:
                    time.sleep(0.35)
            except Exception:
                continue
    email = next((normalize_email(e) for e in emails if valid_email(e)), "")
    phone = next((normalize_phone(p) for p in phones if normalize_phone(p)), "")
    return email, phone


def process_scrape_job(job_id: str) -> dict:
    job = store.get("scrape_jobs", job_id)
    if not job: raise ValueError("Scrape job not found")
    store.update("scrape_jobs", job_id, {"status": "running", "started_at": now_iso(), "updated_at": now_iso()})
    found = saved = discarded = calls = 0
    try:
        candidates, calls = serp_candidates(job["category"], job["city"], job["state"], int(job["result_limit"])); found = len(candidates)
        existing = store.list("clients", order="", limit=10000); required = (store.get("settings", 1) or {}).get("scrape_required_fields", ["email", "phone"])
        for candidate in candidates:
            cand_site = candidate.get("website")
            if not cand_site and settings.SERP_API_KEY:
                cand_site, lookup_calls = find_company_website(candidate.get("business_name", ""), candidate.get("city", job["city"]), candidate.get("state", job["state"]))
                calls += lookup_calls
            website, domain = normalize_website(cand_site); email, crawled_phone = crawl_contact(website)
            phone = normalize_phone(candidate.get("phone")) or crawled_phone
            needs_email = "email" in required and not valid_email(email, check_mx=True)
            needs_phone = "phone" in required and not phone
            if settings.SERP_API_KEY and (needs_email or needs_phone):
                fallback_email, fallback_phone, fallback_site, fallback_calls = fallback_contact_search(
                    candidate.get("business_name", ""),
                    candidate.get("city", job["city"]),
                    candidate.get("state", job["state"]),
                )
                calls += fallback_calls
                if needs_email and fallback_email:
                    email = fallback_email
                if needs_phone and fallback_phone:
                    phone = fallback_phone
                if not website and fallback_site:
                    website, domain = normalize_website(fallback_site)
            row = {"business_name": candidate.get("business_name", ""), "short_name": short_name(candidate.get("business_name", "")), "category": job["category"],
                   "city": job["city"], "state": job["state"], "website": website, "domain": domain, "email": email, "phone": phone,
                   "source": "scrape", "source_detail": candidate.get("source_detail", "serp"), "scrape_job_id": job_id,
                   "outreach_angle": f"{job['category']} in {job['city']}, {job['state']}", "extra": {}, "status": "new"}
            reason = duplicate_reason(row, existing)
            if not reason and "email" in required and not valid_email(email, check_mx=True): reason = "no_email_or_invalid_mx"
            if not reason and "phone" in required and not phone: reason = "no_phone"
            if reason:
                store.insert("scrape_discards", {"id": new_id(), "scrape_job_id": job_id, "business_name": row["business_name"], "reason": reason, "created_at": now_iso()}); discarded += 1; continue
            stamp = now_iso(); row.update({"id": new_id(), "created_at": stamp, "updated_at": stamp}); store.insert("clients", row); existing.append(row); saved += 1
        store.update("scrape_jobs", job_id, {"status": "done", "found_count": found, "saved_count": saved, "discarded_count": discarded, "serp_calls_used": calls, "completed_at": now_iso(), "updated_at": now_iso()})
        return {"found": found, "saved": saved, "discarded": discarded}
    except Exception as exc:
        store.update("scrape_jobs", job_id, {"status": "failed", "found_count": found, "saved_count": saved, "discarded_count": discarded, "serp_calls_used": calls, "error": str(exc)[:1000], "completed_at": now_iso(), "updated_at": now_iso()})
        raise
