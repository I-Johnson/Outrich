from __future__ import annotations

import json
import logging
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from app.adapters.public_records import ADAPTERS
from app.config import settings
from app.core.leads import duplicate_reason, normalize_email, normalize_phone, normalize_website, short_name, valid_email
from app.db import new_id, now_iso, store

logger = logging.getLogger(__name__)

EMAIL_FIND = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_FIND = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
SKIP_DOMAINS = {
    "angi.com", "bbb.org", "facebook.com", "google.com", "homeadvisor.com",
    "houzz.com", "indeed.com", "instagram.com", "linkedin.com", "mapquest.com",
    "maps.google.com", "nextdoor.com", "porch.com", "reddit.com", "realtor.com",
    "thumbtack.com", "tiktok.com", "wikipedia.org", "yellowpages.com", "yelp.com",
    "youtube.com", "zillow.com",
}
GENERIC_NAME_TOKENS = {
    "llc", "inc", "corp", "company", "services", "service", "group",
    "solutions", "consulting", "enterprises", "industries", "the", "and",
}
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"


def _serpapi_request(params: dict) -> dict:
    """Call SerpApi without allowing its query-string API key into logs.

    SerpApi authenticates searches with an ``api_key`` query parameter.  We
    use urllib here because the application's INFO-level httpx logger records
    full request URLs, which would otherwise expose the key in Railway logs.
    Every raised error is deliberately sanitized for the same reason.
    """
    query = urlencode({**params, "api_key": settings.SERP_API_KEY})
    request = Request(
        f"{SERPAPI_SEARCH_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": settings.CRAWLER_USER_AGENT},
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise RuntimeError("SerpApi rejected SERP_API_KEY; verify the key and account access.") from None
        if exc.code == 429:
            raise RuntimeError("SerpApi quota or rate limit reached.") from None
        raise RuntimeError(f"SerpApi request failed with HTTP {exc.code}.") from None
    except URLError:
        raise RuntimeError("SerpApi could not be reached.") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("SerpApi returned an invalid response.") from None
    if not isinstance(payload, dict):
        raise RuntimeError("SerpApi returned an invalid response.")
    if payload.get("error"):
        message = str(payload["error"])
        lowered = message.lower()
        if "api key" in lowered or "unauthorized" in lowered or "invalid key" in lowered:
            raise RuntimeError("SerpApi rejected SERP_API_KEY; verify the key and account access.")
        if "credit" in lowered or "quota" in lowered or "rate limit" in lowered:
            raise RuntimeError("SerpApi quota or rate limit reached.")
        raise RuntimeError(f"SerpApi search failed: {message[:300]}")
    return payload


def serp_candidates(category: str, city: str, state: str, limit: int) -> tuple[list[dict], int]:
    if not settings.SERP_API_KEY:
        raise ValueError("Discovery search is not configured: set SERP_API_KEY on the Railway service.")
    adapter = ADAPTERS.get(state)
    records = adapter.search(category, city, limit) if adapter and adapter.available else []
    candidates: list[dict] = []
    calls = 0
    if settings.SERP_PROVIDER != "serpapi":
        raise ValueError(f"Unsupported SERP_PROVIDER: {settings.SERP_PROVIDER}")
    # Google Maps returns about 20 places per page. ``location`` plus ``z``
    # gives SerpApi the map origin required for pagination, so a requested
    # limit of 30 really can collect 30 businesses instead of stopping at 20.
    start = 0
    while len(candidates) < limit:
        payload = _serpapi_request({
            "engine": "google_maps",
            "type": "search",
            "q": category,
            "location": f"{city}, {state}, United States",
            "z": 14,
            "hl": "en",
            "start": start,
        })
        calls += 1
        places = payload.get("local_results") or []
        for place in places:
            candidates.append(
                {
                    "business_name": place.get("title", ""),
                    "website": place.get("website", ""),
                    "phone": place.get("phone", ""),
                    "city": city,
                    "state": state,
                    "source_detail": "serpapi_google_maps",
                }
            )
            if len(candidates) >= limit:
                break
        if len(candidates) >= limit or not places or not (payload.get("serpapi_pagination") or {}).get("next"):
            break
        start += 20
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


def _serpapi_organic_search(query: str, *, count: int = 4) -> list[dict]:
    """One SerpApi organic query. Provider errors raise:
    a dead key or an outage must fail loudly, not masquerade as an empty market
    while still burning the job's call counter."""
    if not settings.SERP_API_KEY:
        return []
    payload = _serpapi_request({"engine": "google", "q": query, "num": count, "hl": "en", "gl": "us"})
    return (payload.get("organic_results") or [])[:count]


def find_company_website(company: str, city: str, state: str) -> tuple[str, int]:
    if not settings.SERP_API_KEY:
        return "", 0
    organic = _serpapi_organic_search(f"{company} {city} {state}")
    for result in organic:
        link = result.get("link", "")
        if _result_belongs_to_candidate(result, company):
            return link, 1
    return "", 1


def _company_tokens(company: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", company.lower())
        if len(token) >= 4 and token not in GENERIC_NAME_TOKENS
    }


def _result_belongs_to_candidate(result: dict, company: str) -> bool:
    """Require the result domain to carry a distinctive company token.

    Search rank alone is not ownership evidence: directories and similarly
    named businesses often outrank the official site.  This intentionally
    favors precision over saving a candidate with somebody else's contact.
    """
    _, domain = normalize_website(result.get("link", ""))
    if not domain or any(skipped in domain for skipped in SKIP_DOMAINS):
        return False
    tokens = _company_tokens(company)
    if not tokens:
        return False
    domain_label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    return any(token in domain_label for token in tokens)


def _label_matches_tokens(label: str, tokens: set[str]) -> bool:
    """True when the whole label is a concatenation of company tokens
    ('acmeroofing' for 'Acme Roofing'). A stray word like 'directory' or
    'list' ('roofingdirectory.com') fails, which is what keeps directory
    emails from passing as the candidate's own address."""
    remaining = label
    while remaining:
        for token in sorted(tokens, key=len, reverse=True):
            if remaining.startswith(token):
                remaining = remaining[len(token):]
                break
        else:
            return False
    return True


def _email_belongs_to_candidate(email: str, company: str, *, known_domain: str = "") -> bool:
    """Evidence check tying an email to this candidate, not to a directory or
    a same-named stranger. Accepts when the email domain is the candidate's
    known website domain, or when the domain label or local part is composed
    entirely of distinctive company-name tokens."""
    local, _, domain = email.partition("@")
    if not domain or any(skipped in domain for skipped in SKIP_DOMAINS):
        return False
    if known_domain and (domain == known_domain or domain.endswith(f".{known_domain}")):
        return True
    tokens = _company_tokens(company)
    if not tokens:
        return False
    return _label_matches_tokens(domain.split(".")[0], tokens) or _label_matches_tokens(local, tokens)


def fallback_contact_search(company: str, city: str, state: str, known_domain: str = "") -> tuple[str, str, str, int]:
    """Use one fallback SERP query to recover public contact details.

    An email is accepted only with evidence it belongs to this candidate
    (domain match or name-token composition); a bare regex hit from a
    directory page would leak other people's addresses into campaigns.
    """
    if not settings.SERP_API_KEY:
        return "", "", "", 0
    organic = _serpapi_organic_search(f'"{company}" email phone {city} {state}', count=6)
    email = ""
    for result in organic:
        text = f"{result.get('title', '')} {result.get('snippet', '')}"
        for value in EMAIL_FIND.findall(text):
            candidate_email = normalize_email(value)
            if valid_email(candidate_email, check_mx=True) and _email_belongs_to_candidate(candidate_email, company, known_domain=known_domain):
                email = candidate_email
                break
        if email:
            break
    text = " ".join(f"{result.get('title', '')} {result.get('snippet', '')}" for result in organic)
    phone = next((normalize_phone(value) for value in PHONE_FIND.findall(text) if normalize_phone(value)), "")
    website = ""
    for result in organic:
        link = result.get("link", "")
        if _result_belongs_to_candidate(result, company):
            website = link
            break
    return email, phone, website, 1


def _same_origin(url: str, origin: str) -> bool:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" == origin


def crawl_contact(website: str, company: str = "") -> tuple[str, str]:
    website, domain = normalize_website(website)
    if not website or any(sd in domain for sd in SKIP_DOMAINS):
        return "", ""
    origin = f"{urlparse(website).scheme}://{urlparse(website).netloc}"
    robot = RobotFileParser()
    robot.set_url(urljoin(origin, "/robots.txt"))
    emails, phones = [], []
    # Redirects are followed manually and only within the original origin: a
    # candidate's site must not be able to bounce the crawler onto an
    # unrelated host whose robots rules were never consulted.
    with httpx.Client(timeout=8, follow_redirects=False, headers={"User-Agent": settings.CRAWLER_USER_AGENT}) as client:
        # Robots policy per RFC 9309: 2xx = parsed rules, 4xx = unrestricted,
        # 5xx or unreachable = full disallow.
        current = urljoin(origin, "/robots.txt")
        robots_response = None
        try:
            for _ in range(4):
                robots_response = client.get(current)
                if robots_response.status_code in REDIRECT_STATUSES:
                    target = urljoin(current, robots_response.headers.get("location", ""))
                    if not _same_origin(target, origin):
                        return "", ""
                    current = target
                    continue
                break
            else:
                return "", ""
        except Exception:
            return "", ""
        if robots_response.status_code == 200:
            robot.parse(robots_response.text.splitlines())
        elif 400 <= robots_response.status_code < 500:
            robot.parse([])
        else:
            return "", ""
        paths = list(dict.fromkeys((website, urljoin(origin, "/contact"), urljoin(origin, "/about"))))
        for index, path in enumerate(paths):
            current, response = path, None
            try:
                for _ in range(4):
                    if not robot.can_fetch(settings.CRAWLER_USER_AGENT, current):
                        break
                    response = client.get(current)
                    if response.status_code in REDIRECT_STATUSES:
                        target = urljoin(current, response.headers.get("location", ""))
                        if not _same_origin(target, origin):
                            response = None
                            break
                        current = target
                        continue
                    break
                else:
                    response = None
                if response is None:
                    continue
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
    email = next(
        (
            normalize_email(e)
            for e in emails
            if valid_email(e, check_mx=True)
            and _email_belongs_to_candidate(normalize_email(e), company, known_domain=domain)
        ),
        "",
    )
    phone = next((normalize_phone(p) for p in phones if normalize_phone(p)), "")
    return email, phone


def _email_discard_reason(email: str) -> str:
    if not normalize_email(email):
        return "no_email_found"
    if not valid_email(email):
        return "invalid_email_syntax"
    if not valid_email(email, check_mx=True):
        return "email_domain_has_no_mx"
    return ""


def process_scrape_job(job_id: str, storage=None) -> dict:
    s = storage or store
    job = s.get("scrape_jobs", job_id)
    if not job: raise ValueError("Scrape job not found")
    s.update("scrape_jobs", job_id, {"status": "running", "started_at": now_iso(), "updated_at": now_iso()})
    found = saved = discarded = calls = 0
    try:
        candidates, calls = serp_candidates(job["category"], job["city"], job["state"], int(job["result_limit"])); found = len(candidates)
        existing = s.list("clients", order="", limit=10000)
        required = (s.get("settings", 1) or {}).get("scrape_required_fields")
        if required is None:
            required = ["email", "phone"]
        for candidate in candidates:
            company = candidate.get("business_name", "")
            cand_site = candidate.get("website")
            if not cand_site and settings.SERP_API_KEY:
                cand_site, lookup_calls = find_company_website(company, candidate.get("city", job["city"]), candidate.get("state", job["state"]))
                calls += lookup_calls
            website, domain = normalize_website(cand_site); email, crawled_phone = crawl_contact(website, company)
            phone = normalize_phone(candidate.get("phone")) or crawled_phone
            needs_email = "email" in required and bool(_email_discard_reason(email))
            needs_phone = "phone" in required and not phone
            if settings.SERP_API_KEY and (needs_email or needs_phone):
                fallback_email, fallback_phone, fallback_site, fallback_calls = fallback_contact_search(
                    company,
                    candidate.get("city", job["city"]),
                    candidate.get("state", job["state"]),
                    known_domain=domain,
                )
                calls += fallback_calls
                if needs_email and fallback_email:
                    email = fallback_email
                if needs_phone and fallback_phone:
                    phone = fallback_phone
                if not website and fallback_site:
                    website, domain = normalize_website(fallback_site)
            row = {"business_name": company, "short_name": short_name(company), "category": job["category"],
                   "city": job["city"], "state": job["state"], "website": website, "domain": domain, "email": email, "phone": phone,
                   "source": "scrape", "source_detail": candidate.get("source_detail", "serp"), "scrape_job_id": job_id,
                   "outreach_angle": f"{job['category']} in {job['city']}, {job['state']}", "extra": {}, "status": "new"}
            reason = duplicate_reason(row, existing)
            if not reason and "email" in required: reason = _email_discard_reason(email)
            if not reason and "phone" in required and not phone: reason = "no_phone"
            if reason:
                s.insert("scrape_discards", {"id": new_id(), "scrape_job_id": job_id, "business_name": row["business_name"], "reason": reason, "created_at": now_iso()}); discarded += 1; continue
            stamp = now_iso(); row.update({"id": new_id(), "created_at": stamp, "updated_at": stamp}); s.insert("clients", row); existing.append(row); saved += 1
        s.update("scrape_jobs", job_id, {"status": "done", "found_count": found, "saved_count": saved, "discarded_count": discarded, "serp_calls_used": calls, "completed_at": now_iso(), "updated_at": now_iso()})
        return {"found": found, "saved": saved, "discarded": discarded}
    except Exception as exc:
        s.update("scrape_jobs", job_id, {"status": "failed", "found_count": found, "saved_count": saved, "discarded_count": discarded, "serp_calls_used": calls, "error": str(exc)[:1000], "completed_at": now_iso(), "updated_at": now_iso()})
        raise
