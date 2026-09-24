from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def calendar_events(logs: list[dict], campaigns: list[dict], *, include_paused: bool = False, include_provider: bool = False) -> list[dict]:
    """Return privacy-safe timestamps for the browser-local overview chart."""
    campaign_states = {row.get("id"): row.get("state") for row in campaigns}
    allowed_states = {"running", "paused"} if include_paused else {"running"}
    events = []
    for log in logs:
        status = log.get("status")
        if status in {"sent", "replied", "bounced"} and log.get("sent_at"):
            timestamp = log["sent_at"]
            display_status = "sent"
        elif status in {"queued", "sending"} and log.get("scheduled_for") and campaign_states.get(log.get("campaign_id")) in allowed_states:
            timestamp = log["scheduled_for"]
            display_status = "scheduled"
        else:
            continue
        ev = {
            "timestamp": timestamp,
            "status": display_status,
            "account": str(log.get("sender_account") or "1"),
        }
        if include_provider:
            ev["provider"] = str(log.get("provider") or ("pingram" if "@contractorops.ai" in str(log.get("sender_email") or "") else "gmail"))
        events.append(ev)
    return events


def parse_days(value) -> list[int]:
    if isinstance(value, list): return [int(x) for x in value]
    return [int(x) for x in str(value or "0,1,2,3,4").replace("[", "").replace("]", "").split(",") if x.strip()]


def next_send_time(after: datetime, config: dict, *, jitter: bool = True) -> datetime:
    zone = ZoneInfo(config.get("timezone") or "UTC")
    if after.tzinfo is None: after = after.replace(tzinfo=timezone.utc)
    minimum = int(config.get("min_delay_minutes") or 3); maximum = max(minimum, int(config.get("max_delay_minutes") or 15))
    delay = random.randint(minimum, maximum) if jitter else minimum
    local = (after + timedelta(minutes=delay)).astimezone(zone)
    start = time.fromisoformat(config.get("send_start") or "09:00"); end = time.fromisoformat(config.get("send_end") or "17:00")
    days = parse_days(config.get("send_days"))
    for _ in range(14):
        if local.weekday() in days and start <= local.time().replace(tzinfo=None) < end: return local.astimezone(timezone.utc)
        if local.weekday() in days and local.time().replace(tzinfo=None) < start:
            return local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0).astimezone(timezone.utc)
        local = (local + timedelta(days=1)).replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def next_send_day_start(after: datetime, config: dict) -> datetime:
    """Return the start of the next configured sending day."""
    zone = ZoneInfo(config.get("timezone") or "UTC")
    if after.tzinfo is None: after = after.replace(tzinfo=timezone.utc)
    local = after.astimezone(zone)
    start = time.fromisoformat(config.get("send_start") or "09:00")
    days = parse_days(config.get("send_days"))
    for offset in range(1, 15):
        candidate = (local + timedelta(days=offset)).replace(
            hour=start.hour, minute=start.minute, second=0, microsecond=0
        )
        if candidate.weekday() in days:
            return candidate.astimezone(timezone.utc)
    return candidate.astimezone(timezone.utc)


def in_send_window(moment: datetime, config: dict) -> bool:
    zone = ZoneInfo(config.get("timezone") or "UTC"); local = moment.astimezone(zone)
    start = time.fromisoformat(config.get("send_start") or "09:00"); end = time.fromisoformat(config.get("send_end") or "17:00")
    return local.weekday() in parse_days(config.get("send_days")) and start <= local.time().replace(tzinfo=None) < end
