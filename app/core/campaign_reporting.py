"""Pure campaign delivery and template performance summaries."""
from __future__ import annotations

from collections import Counter


def campaign_performance(logs: list[dict], templates: dict[str, dict]) -> dict:
    statuses = Counter(str(log.get("status") or "unknown") for log in logs)
    delivered = sum(1 for log in logs if log.get("sent_at"))
    summary = {
        "total": len(logs),
        "scheduled": statuses["queued"] + statuses["sending"],
        "sent": delivered,
        "replies": statuses["replied"],
        "bounced": statuses["bounced"],
        "failed": statuses["failed"],
        "skipped": statuses["skipped"],
    }
    rows: dict[str, dict] = {}
    for log in logs:
        template_id = str(log.get("template_id") or "unknown")
        row = rows.setdefault(
            template_id,
            {
                "id": template_id,
                "name": (templates.get(template_id) or {}).get("name") or "Deleted template",
                "total": 0,
                "sent": 0,
                "replies": 0,
                "bounced": 0,
                "failed": 0,
            },
        )
        row["total"] += 1
        if log.get("sent_at"):
            row["sent"] += 1
        status = log.get("status")
        if status in {"replied", "bounced", "failed"}:
            row[f"{status if status != 'replied' else 'replies'}"] += 1
    for row in rows.values():
        row["reply_rate"] = round((row["replies"] / row["sent"] * 100), 1) if row["sent"] else 0.0
    return {"summary": summary, "templates": sorted(rows.values(), key=lambda row: (-row["sent"], row["name"]))}

