"""CSV decoding, mapping, deterministic normalization, preview and confirm."""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from typing import Any

import httpx

from app.config import settings
from app.core.leads import duplicate_reason, infer_name_from_email, normalize_email, normalize_phone, normalize_website, short_name, valid_email
from app.db import new_id, now_iso, store

ALIASES = {
    "business_name": [
        "business_name", "company", "company_name", "legal_name", "name", "business",
        "account", "account_name", "firm", "contractor", "organization", "client", "title"
    ],
    "owner_first_name": [
        "owner_first_name", "first_name", "first", "owner", "contact_name", "contact",
        "given_name", "lead_name", "person", "full_name"
    ],
    "email": [
        "email", "email_address", "primary_email", "contact_email", "work_email",
        "personal_email", "e_mail", "mail", "e-mail"
    ],
    "phone": [
        "phone", "number", "phone_number", "telephone", "mobile", "cell", "work_phone",
        "phone_1", "direct_phone", "tel"
    ],
    "website": [
        "website", "url", "site", "web", "domain", "homepage", "company_website",
        "website_url", "link"
    ],
    "category": [
        "category", "trade", "classification", "industry", "segment", "specialty",
        "service", "services", "line_of_business", "trade_type"
    ],
    "city": ["city", "town", "metro", "municipality"],
    "state": ["state", "st", "province", "region"],
    "zip": ["zip", "zipcode", "zip_code", "postal_code", "postal"],
    "notes": ["notes", "note", "description", "summary", "comments"],
}
FIELDS = list(ALIASES)
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_CSV_ROWS = 5_000
GEMINI_CLEANUP_BATCH_SIZE = 25
EMAIL_SEARCH = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


def decode_csv(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError("CSV exceeds the 5 MB limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")

    dialect = csv.excel
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except Exception:
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        if "\t" in first_line and first_line.count("\t") > first_line.count(","):
            dialect = csv.excel_tab
        elif ";" in first_line and first_line.count(";") > first_line.count(","):
            class SemiDialect(csv.excel):
                delimiter = ";"
            dialect = SemiDialect
        elif "|" in first_line and first_line.count("|") > first_line.count(","):
            class PipeDialect(csv.excel):
                delimiter = "|"
            dialect = PipeDialect

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = [{str(k or "").strip(): str(v or "").strip() for k, v in row.items()} for row in reader if any(row.values())]
    if len(rows) > MAX_CSV_ROWS:
        raise ValueError("CSV exceeds the 5,000 row limit")
    return list(reader.fieldnames or []), rows


def heuristic_mapping(headers: list[str], sample_rows: list[dict[str, str]] | None = None) -> dict[str, str]:
    result: dict[str, str] = {}
    for source in headers:
        key = source.strip().lower().replace(" ", "_").replace("-", "_").replace(".", "_")
        for dest, aliases in ALIASES.items():
            if key in aliases and dest not in result.values():
                result[source] = dest
                break

    # If key fields are still unmapped, inspect sample data values
    if sample_rows:
        unmapped = [h for h in headers if h not in result]
        for h in unmapped:
            values = [str(r.get(h) or "").strip() for r in sample_rows[:30] if str(r.get(h) or "").strip()]
            if not values:
                continue

            if "email" not in result.values():
                email_count = sum(1 for v in values if "@" in v and "." in v.split("@")[-1])
                if email_count >= len(values) * 0.4:
                    result[h] = "email"
                    continue

            if "website" not in result.values():
                web_count = sum(1 for v in values if v.startswith(("http://", "https://", "www.")) or re.search(r"\.(com|net|org|co|io|biz|us)\b", v, re.IGNORECASE))
                if web_count >= len(values) * 0.4:
                    result[h] = "website"
                    continue

            if "phone" not in result.values():
                digits = [re.sub(r"\D", "", v) for v in values]
                phone_count = sum(1 for d in digits if 7 <= len(d) <= 15)
                if phone_count >= len(values) * 0.4:
                    result[h] = "phone"
                    continue

            if "state" not in result.values():
                if all(len(v) == 2 and v.isalpha() for v in values):
                    result[h] = "state"
                    continue

    return result


def _gemini_json(prompt: dict) -> Any | None:
    """Call Gemini with strict JSON output, retrying one malformed/failed response."""
    if not settings.GEMINI_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_MODEL}:generateContent?key={settings.GEMINI_API_KEY}"
    for _attempt in range(2):
        try:
            with httpx.Client(timeout=8) as client:
                res = client.post(
                    url,
                    json={
                        "contents": [{"parts": [{"text": json.dumps(prompt)}]}],
                        "generationConfig": {"responseMimeType": "application/json"},
                    },
                )
            res.raise_for_status()
            text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def gemini_mapping(headers: list[str], rows: list[dict]) -> dict[str, str] | None:
    prompt = {
        "task": "Map CSV source columns to destination fields. Return one JSON object only. Each key must be an exact source header. Each value must be one allowed field or null. Never map two source headers to the same destination field.",
        "allowed_fields": FIELDS,
        "headers": headers,
        "sample_rows": rows[:10],
    }
    data = _gemini_json(prompt)
    if not isinstance(data, dict):
        return None
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for source, destination in data.items():
        if destination is None:
            continue
        if source not in headers or destination not in FIELDS or destination in used:
            return None
        mapping[source] = destination
        used.add(destination)
    return mapping or None


def _mapped_values(source: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    return {destination: str(source.get(source_key) or "").strip() for source_key, destination in mapping.items()}


def _is_messy_row(source: dict[str, str], mapping: dict[str, str]) -> bool:
    values = _mapped_values(source, mapping)
    email = values.get("email", "")
    if email and not valid_email(normalize_email(email)) and EMAIL_SEARCH.search(email):
        return True
    if not email and any(EMAIL_SEARCH.search(str(value or "")) for value in source.values()):
        return True
    city = values.get("city", "")
    if city and "," in city and not values.get("state"):
        return True
    return any("\n" in str(value or "") and len(str(value or "").splitlines()) > 1 for value in source.values())


def gemini_cleanup_rows(source_rows: list[dict[str, str]], mapping: dict[str, str]) -> dict[int, dict[str, str]]:
    """Return validated field overrides for only rows that need interpretation."""
    if not settings.GEMINI_API_KEY:
        return {}
    # Limit AI cleanup to at most 1 fast batch (25 rows) so the web request never times out
    messy = [(index, row) for index, row in enumerate(source_rows) if _is_messy_row(row, mapping)][:GEMINI_CLEANUP_BATCH_SIZE]
    cleaned: dict[int, dict[str, str]] = {}
    for start in range(0, len(messy), GEMINI_CLEANUP_BATCH_SIZE):
        batch = messy[start:start + GEMINI_CLEANUP_BATCH_SIZE]
        prompt = {
            "task": "Clean only the supplied messy CSV rows. Return a JSON array only. Each item must be {\"index\": integer, \"fields\": object}. The fields object may contain only allowed_fields. Preserve facts; do not invent missing values.",
            "allowed_fields": FIELDS,
            "column_mapping": mapping,
            "rows": [{"index": index, "source": row} for index, row in batch],
        }
        data = _gemini_json(prompt)
        if not isinstance(data, list):
            continue
        allowed_indices = {index for index, _row in batch}
        for item in data:
            if not isinstance(item, dict) or item.get("index") not in allowed_indices or not isinstance(item.get("fields"), dict):
                continue
            fields = item["fields"]
            if any(key not in FIELDS or not isinstance(value, (str, int, float)) for key, value in fields.items()):
                continue
            cleaned[int(item["index"])] = {key: str(value).strip() for key, value in fields.items()}
    return cleaned


def select_mapping(headers: list[str], source_rows: list[dict[str, str]], mapping_override: dict | None = None) -> dict[str, str]:
    if mapping_override is not None:
        return {key: value for key, value in mapping_override.items() if key in headers and value in FIELDS}
    if settings.GEMINI_API_KEY:
        mapped = gemini_mapping(headers, source_rows)
        if mapped:
            return mapped
    return heuristic_mapping(headers, source_rows)


def clean_row(source: dict[str, str], mapping: dict[str, str], overrides: dict[str, str] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {dest: source.get(src, "").strip() for src, dest in mapping.items() if dest in FIELDS}
    if overrides:
        row.update({key: str(value).strip() for key, value in overrides.items() if key in FIELDS})
    email_match = EMAIL_SEARCH.search(str(row.get("email") or ""))
    if not email_match and not row.get("email"):
        email_match = next((match for value in source.values() if (match := EMAIL_SEARCH.search(str(value or "")))), None)
    if email_match:
        row["email"] = email_match.group(0)
    row["email"] = normalize_email(row.get("email"))
    row["phone"] = normalize_phone(row.get("phone"))
    website, domain = normalize_website(row.get("website") or row.get("domain"))
    row["website"] = website
    row["domain"] = domain

    # Extract first name if full name was captured, or infer from email
    owner = row.get("owner_first_name", "").strip()
    if owner and " " in owner:
        row["owner_first_name"] = owner.split()[0].title()
    elif not owner and row.get("email"):
        inferred = infer_name_from_email(row.get("email"), row.get("business_name", ""), domain)
        if inferred:
            row["owner_first_name"] = inferred
    city = str(row.get("city") or "").strip()
    if city and not row.get("state") and "," in city:
        possible_city, possible_state = [part.strip() for part in city.rsplit(",", 1)]
        if len(possible_state) == 2 and possible_state.isalpha():
            row["city"] = possible_city
            row["state"] = possible_state.upper()

    # Intelligent business name fallback
    if not row.get("business_name"):
        row["business_name"] = domain or (row.get("owner_first_name") and f"{row['owner_first_name']}'s Company") or (row.get("email") and row["email"].split("@")[0].title()) or "Valued Partner"
    row["short_name"] = short_name(row.get("business_name", ""))
    used = set(mapping)
    row["extra"] = {k: v for k, v in source.items() if k not in used and v}
    return row


def _categorize(headers: list[str], source_rows: list[dict], mapping: dict[str, str], cleanup: dict[int, dict[str, str]] | None = None):
    existing = store.list("clients", order="", limit=20000, select="email,domain")
    seen_emails = {normalize_email(item.get("email")) for item in existing if item.get("email")}
    seen_domains = {(item.get("domain") or "").lower() for item in existing if item.get("domain")}
    required = (store.get("settings", 1) or {}).get("csv_required_fields", ["email"])

    ready, rejected = [], []
    for index, source in enumerate(source_rows):
        row = clean_row(source, mapping, (cleanup or {}).get(index))
        email = row.get("email", "")
        phone = row.get("phone", "")
        domain = row.get("domain", "")

        reason = ""
        if "email" in required and not valid_email(email):
            reason = "invalid_email"
        elif "phone" in required and not phone:
            reason = "invalid_phone"
        elif email and email in seen_emails:
            reason = "duplicate_email"
        elif domain and domain in seen_domains:
            reason = "duplicate_domain"

        if reason:
            rejected.append({**row, "reason": reason})
        else:
            ready.append(row)
            if email: seen_emails.add(email)
            if domain: seen_domains.add(domain)

    return ready, rejected


def build_preview(raw: bytes, filename: str, mapping_override: dict | None = None) -> dict:
    headers, source_rows = decode_csv(raw)
    mapping = select_mapping(headers, source_rows, mapping_override)
    cleanup = gemini_cleanup_rows(source_rows, mapping)
    ready, rejected = _categorize(headers, source_rows, mapping, cleanup)
    batch = {"id": new_id(), "filename": filename, "uploaded_at": now_iso(), "total_rows": len(source_rows), "imported_rows": 0,
             "duplicate_rows": sum(1 for r in rejected if r["reason"].startswith("duplicate")), "rejected_rows": len(rejected),
             "column_mapping": mapping, "rejected_csv": json.dumps({"headers": headers, "source": source_rows, "ready": ready, "rejected": rejected}), "status": "preview"}
    store.insert("import_batches", batch)
    return {**batch, "ready": ready[:20], "ready_count": len(ready), "rejected": rejected[:20], "headers": headers}


def remap_preview(batch_id: str, mapping: dict[str, str]) -> dict:
    batch = store.get("import_batches", batch_id)
    if not batch or batch.get("status") != "preview": raise ValueError("Import preview is no longer available")
    payload = json.loads(batch.get("rejected_csv") or "{}"); headers = payload.get("headers", []); source_rows = payload.get("source", [])
    mapping = {k: v for k, v in mapping.items() if k in headers and v in FIELDS}
    cleanup = gemini_cleanup_rows(source_rows, mapping)
    ready, rejected = _categorize(headers, source_rows, mapping, cleanup)
    values = {"duplicate_rows": sum(1 for r in rejected if r["reason"].startswith("duplicate")), "rejected_rows": len(rejected), "column_mapping": mapping,
              "rejected_csv": json.dumps({"headers": headers, "source": source_rows, "ready": ready, "rejected": rejected})}
    store.update("import_batches", batch_id, values)
    return {**batch, **values, "headers": headers, "ready": ready[:20], "ready_count": len(ready), "rejected": rejected[:20]}


def confirm_import(batch_id: str) -> dict:
    batch = store.get("import_batches", batch_id)
    if not batch or batch["status"] != "preview": raise ValueError("Import preview is no longer available")
    payload = json.loads(batch.get("rejected_csv") or "{}"); added = 0
    rejected = list(payload.get("rejected", []))
    ready_rows = payload.get("ready", [])

    candidates = []
    stamp = now_iso()
    for row in ready_rows:
        candidates.append({**row, "id": new_id(), "source": "csv_import", "import_batch_id": batch_id, "status": "new", "created_at": stamp, "updated_at": stamp})

    chunk_size = 100
    for i in range(0, len(candidates), chunk_size):
        chunk = candidates[i:i + chunk_size]
        if hasattr(store, "insert_many"):
            try:
                store.insert_many("clients", chunk)
                added += len(chunk)
                continue
            except Exception:
                pass
        for c in chunk:
            try:
                store.insert("clients", c)
                added += 1
            except sqlite3.IntegrityError:
                rejected.append({**c, "reason": "insert_conflict"})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
                rejected.append({**c, "reason": "insert_conflict"})
            except Exception:
                rejected.append({**c, "reason": "insert_conflict"})

    output = io.StringIO(); cols = sorted({k for r in rejected for k in r if k != "extra"})
    writer = csv.DictWriter(output, fieldnames=cols)
    if cols: writer.writeheader(); writer.writerows([{k: r.get(k, "") for k in cols} for r in rejected])
    store.update("import_batches", batch_id, {"imported_rows": added, "rejected_rows": len(rejected), "rejected_csv": output.getvalue(), "status": "done"})
    return {"added": added, "rejected": len(rejected)}


def undo_import(batch_id: str) -> int:
    batch = store.get("import_batches", batch_id)
    if not batch or batch.get("status") != "done":
        raise ValueError("Only a completed import can be undone")
    leads = store.list("clients", {"import_batch_id": batch_id}, order="", limit=10000)
    lead_ids = [str(lead["id"]) for lead in leads]
    for offset in range(0, len(lead_ids), 100):
        if store.list(
            "email_log",
            {"client_id": ("in", lead_ids[offset:offset + 100])},
            order="",
            limit=1,
        ):
            raise ValueError("This import has campaign email history and cannot be undone safely")
    deleted = store.delete("clients", {"import_batch_id": batch_id})
    store.update("import_batches", batch_id, {"status": "undone"})
    return deleted
