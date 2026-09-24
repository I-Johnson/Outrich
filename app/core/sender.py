"""Campaign queueing and restart-safe delivery backed by email_log."""
from __future__ import annotations

import logging
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
SEND_LEASE_MINUTES = 15

from app.adapters import get_provider
from app.adapters.base import SendResult
from app.config import settings as env
from app.core.gmail_senders import get_gmail_sender, list_gmail_senders, sender_context
from app.core.lead_status import set_client_status
from app.core.schedule import in_send_window, next_send_day_start, next_send_time
from app.core.template_engine import render_template
from app.db import new_id, now_iso, store


def app_settings() -> dict:
    return store.get("settings", 1) or {}


def delete_campaign(campaign_id: str) -> None:
    """Delete only an unused campaign; preserve every campaign with send history."""
    campaign = store.get("campaigns", campaign_id)
    if not campaign:
        raise ValueError("Campaign not found")
    if campaign.get("state") in {"running", "paused"}:
        raise ValueError("Stop the campaign before deleting it")
    if store.list("email_log", {"campaign_id": campaign_id}, order="", limit=1):
        raise ValueError("Campaigns with email history cannot be deleted")
    store.delete("campaigns", {"id": campaign_id})


def cancel_campaign_queue(campaign_id: str) -> int:
    pending = store.list(
        "email_log",
        {"campaign_id": campaign_id, "status": ("in", ["queued", "sending"])},
        order="",
        limit=20000,
    )
    for log in pending:
        store.update(
            "email_log",
            log["id"],
            {"status": "skipped", "next_attempt_at": None, "error": "Campaign stopped."},
        )
    affected_clients = {str(log.get("client_id")) for log in pending if log.get("client_id")}
    remaining = store.list("email_log", {"status": ("in", ["queued", "sending"])}, order="", limit=20000)
    still_queued = {str(log.get("client_id")) for log in remaining if log.get("client_id")}
    for client_id in affected_clients - still_queued:
        client = store.get("clients", client_id)
        if client and client.get("status") == "queued":
            store.update("clients", client_id, {"status": "new", "updated_at": now_iso()})
    return len(pending)


def refresh_campaign_states(campaign_ids: set[str] | None = None) -> int:
    campaigns = store.list("campaigns", order="", limit=10000)
    if campaign_ids is not None:
        campaigns = [campaign for campaign in campaigns if str(campaign.get("id")) in campaign_ids]
    else:
        campaigns = [campaign for campaign in campaigns if campaign.get("state") == "running"]
    logs = store.list("email_log", order="", limit=20000)
    changed = 0
    for campaign in campaigns:
        campaign_logs = [log for log in logs if str(log.get("campaign_id")) == str(campaign["id"])]
        if campaign.get("state") == "running" and campaign_logs and not any(log.get("status") in {"queued", "sending"} for log in campaign_logs):
            store.update("campaigns", campaign["id"], {"state": "done", "updated_at": now_iso()})
            changed += 1
    return changed


def _matches(client: dict, target: dict) -> bool:
    for field in ("category", "state", "city", "status", "source", "import_batch_id"):
        wanted = target.get(field)
        if wanted and client.get(field) not in (wanted if isinstance(wanted, list) else [wanted]): return False
    return True


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _schedule_config(cfg: dict) -> dict:
    return {
        "timezone": cfg.get("timezone") or "UTC",
        "send_days": cfg.get("send_days") or [0, 1, 2, 3, 4],
        "send_start": cfg.get("send_start") or "09:00",
        "send_end": cfg.get("send_end") or "17:00",
        "min_delay_minutes": int(cfg.get("min_delay_minutes") or 3),
        "max_delay_minutes": int(cfg.get("max_delay_minutes") or 15),
    }


def _log_counting_time(log: dict) -> datetime | None:
    if log.get("status") in {"sent", "bounced"}:
        return _parse_datetime(log.get("sent_at") or log.get("scheduled_for"))
    if log.get("status") in {"queued", "sending"}:
        return _parse_datetime(log.get("scheduled_for"))
    return None


def _global_schedule_state(logs: list[dict], cfg: dict, now: datetime, *, exclude_queued: bool = False):
    zone = ZoneInfo(cfg["timezone"])
    counts: Counter[tuple[str, str]] = Counter()
    cursors: dict[tuple[str, str], datetime] = {}
    for log in logs:
        if exclude_queued and log.get("status") == "queued":
            continue
        moment = _log_counting_time(log)
        if not moment:
            continue
        account = str(log.get("sender_account") or "1")
        day = moment.astimezone(zone).date().isoformat()
        counts[(account, day)] += 1
        cursors[(account, day)] = max(cursors.get((account, day), moment), moment)
    return counts, cursors


def _next_global_slot(account: str, search_after: datetime, cfg: dict, counts: Counter, cursors: dict, cap: int) -> datetime:
    """Find the earliest unused per-account slot, including gaps before later mail."""
    zone = ZoneInfo(cfg["timezone"])
    candidate = next_send_time(search_after, cfg)
    for _ in range(5000):
        day = candidate.astimezone(zone).date().isoformat()
        if counts[(account, day)] >= cap:
            candidate = next_send_day_start(candidate, cfg)
            continue
        latest = cursors.get((account, day))
        if latest:
            candidate = next_send_time(max(latest, search_after), cfg)
            candidate_day = candidate.astimezone(zone).date().isoformat()
            if candidate_day != day:
                continue
        return candidate
    raise ValueError("Unable to find an available Gmail send slot")


def _reserve_global_slot(account: str, slot: datetime, cfg: dict, counts: Counter, cursors: dict) -> None:
    day = slot.astimezone(ZoneInfo(cfg["timezone"])).date().isoformat()
    counts[(account, day)] += 1
    cursors[(account, day)] = max(cursors.get((account, day), slot), slot)


def _choose_account_slot(accounts: list[str], search_after: datetime, cfg: dict, counts: Counter, cursors: dict, cap: int, assigned: Counter) -> tuple[str, datetime]:
    """Use the earliest available day, then the least-loaded sender that day."""
    zone = ZoneInfo(cfg["timezone"])
    choices = []
    for index, account in enumerate(accounts):
        slot = _next_global_slot(account, search_after, cfg, counts, cursors, cap)
        day = slot.astimezone(zone).date().isoformat()
        choices.append((day, counts[(account, day)], assigned[account], slot, index, account))
    _, _, _, slot, _, account = min(choices)
    return account, slot


def _choose_pingram_conveyor_slot(
    accounts: list[str],
    domain_cursor: datetime,
    counts: Counter,
    assigned: Counter,
    daily_cap: int,
    min_delay_minutes: int,
    max_delay_minutes: int,
) -> tuple[str, datetime, datetime]:
    """Find the next slot along the single domain conveyor belt and pick the next representative.

    Returns (chosen_account, slot_datetime, new_domain_cursor).
    """
    if not accounts:
        accounts = ["1"]
    if domain_cursor.tzinfo is None:
        domain_cursor = domain_cursor.replace(tzinfo=timezone.utc)
    min_delay = max(1, int(min_delay_minutes))
    max_delay = max(min_delay, int(max_delay_minutes))
    delay_sec = random.randint(min_delay * 60, max_delay * 60)
    candidate = domain_cursor + timedelta(seconds=delay_sec)
    day = candidate.astimezone(timezone.utc).date().isoformat()

    available = [acc for acc in accounts if counts[(acc, day)] < daily_cap]
    if not available:
        # All accounts hit cap for this UTC day; jump to next UTC day 00:00:00
        next_day = candidate.astimezone(timezone.utc).date() + timedelta(days=1)
        next_day_start = datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=timezone.utc)
        delay_sec = random.randint(min_delay * 60, max_delay * 60)
        candidate = next_day_start + timedelta(seconds=delay_sec)
        day = candidate.astimezone(timezone.utc).date().isoformat()
        available = [acc for acc in accounts if counts[(acc, day)] < daily_cap]
        if not available:
            available = accounts

    chosen = min(
        available,
        key=lambda a: (counts[(a, day)], assigned[a], accounts.index(a) if a in accounts else 0)
    )
    counts[(chosen, day)] += 1
    assigned[chosen] += 1
    return chosen, candidate, candidate


def reschedule_queued_emails(now: datetime | None = None) -> dict:
    """Rebuild every queued timestamp against global limits.

    Gmail accounts use per-account business-window schedules.
    Pingram accounts share a unified domain conveyor belt (24/7, serialized 5-20 min gaps).
    """
    now = now or datetime.now(timezone.utc)
    raw_cfg = app_settings()
    cfg = _schedule_config(raw_cfg)
    cap = max(1, int(raw_cfg.get("daily_cap") or 20))
    pingram_min = max(1, int(raw_cfg.get("pingram_min_delay") or 5))
    pingram_max = max(pingram_min, int(raw_cfg.get("pingram_max_delay") or 20))
    pingram_cap = max(1, int(raw_cfg.get("pingram_daily_cap") or 30))

    logs = store.list("email_log", order="scheduled_for asc", limit=20000)
    queued = [log for log in logs if log.get("status") == "queued"]
    queued_gmail = [log for log in queued if log.get("provider") != "pingram"]
    queued_pingram = [log for log in queued if log.get("provider") == "pingram"]

    updated = 0
    by_account: Counter[str] = Counter()

    # 1. Reschedule Gmail queued emails (per-account independent schedule)
    counts, cursors = _global_schedule_state(logs, cfg, now, exclude_queued=True)
    for log in sorted(queued_gmail, key=lambda item: (item.get("scheduled_for") or "", item.get("created_at") or "", item.get("id") or "")):
        account = str(log.get("sender_account") or "1")
        slot = _next_global_slot(account, now, cfg, counts, cursors, cap)
        _reserve_global_slot(account, slot, cfg, counts, cursors)
        by_account[account] += 1
        if log.get("scheduled_for") != slot.isoformat():
            store.update("email_log", log["id"], {"scheduled_for": slot.isoformat(), "next_attempt_at": None})
            updated += 1

    # 2. Reschedule Pingram queued emails (Unified Domain Conveyor Belt)
    if queued_pingram:
        all_senders = list_gmail_senders(active_only=True, storage=store, cfg=raw_cfg)
        pingram_senders = [s for s in all_senders if s.get("provider") == "pingram"]
        if not pingram_senders:
            pingram_senders = all_senders
        pingram_accounts = [str(s["id"]) for s in pingram_senders]
        senders_by_id = {str(s["id"]): s for s in all_senders}

        # Track existing non-queued sends/sending for Pingram
        pingram_counts: Counter[tuple[str, str]] = Counter()
        pingram_assigned: Counter[str] = Counter()
        pingram_cursor = now
        for log in logs:
            if log.get("provider") == "pingram" and log.get("status") in {"sent", "sending"}:
                m = _log_counting_time(log)
                if m:
                    acc = str(log.get("sender_account") or "")
                    d = m.astimezone(timezone.utc).date().isoformat()
                    pingram_counts[(acc, d)] += 1
                    if m > pingram_cursor:
                        pingram_cursor = m

        # Pre-cache clients and templates for fast re-rendering
        client_cache = {str(c["id"]): c for c in store.list("clients", limit=20000)}
        template_cache = {str(t["id"]): t for t in store.list("email_templates", limit=1000)}

        for log in sorted(queued_pingram, key=lambda item: (item.get("scheduled_for") or "", item.get("created_at") or "", item.get("id") or "")):
            chosen_acc, slot, pingram_cursor = _choose_pingram_conveyor_slot(
                pingram_accounts, pingram_cursor, pingram_counts, pingram_assigned,
                pingram_cap, pingram_min, pingram_max
            )
            by_account[chosen_acc] += 1

            client = client_cache.get(str(log.get("client_id")))
            template = template_cache.get(str(log.get("template_id")))
            sender_row = senders_by_id.get(chosen_acc)

            new_subject = log.get("subject_sent")
            new_body = log.get("body_sent")
            if template and client and sender_row:
                render_cfg = sender_context(raw_cfg, sender_row)
                new_subject, _ = render_template(template["subject"], client, render_cfg)
                new_body, _ = render_template(template["body"], client, render_cfg, html_escape=template.get("type") == "html")

            needs_update = (
                log.get("scheduled_for") != slot.isoformat()
                or str(log.get("sender_account")) != chosen_acc
                or log.get("body_sent") != new_body
                or log.get("subject_sent") != new_subject
            )
            if needs_update:
                update_vals = {
                    "scheduled_for": slot.isoformat(),
                    "sender_account": chosen_acc,
                    "next_attempt_at": None,
                }
                if new_subject is not None:
                    update_vals["subject_sent"] = new_subject
                if new_body is not None:
                    update_vals["body_sent"] = new_body
                store.update("email_log", log["id"], update_vals)
                updated += 1

    return {"queued": len(queued), "updated": updated, "by_account": dict(by_account)}


def _sent_today_by_account(logs: list[dict], cfg: dict, now: datetime) -> Counter[str]:
    zone = ZoneInfo(cfg["timezone"])
    today = now.astimezone(zone).date()
    counts: Counter[str] = Counter()
    for log in logs:
        if log.get("status") != "sent":
            continue
        sent_at = _parse_datetime(log.get("sent_at"))
        if sent_at and sent_at.astimezone(zone).date() == today:
            counts[str(log.get("sender_account") or "1")] += 1
    return counts


def recover_interrupted_sends(now: datetime | None = None) -> int:
    """Release expired send leases without risking an automatic duplicate send.

    A process can stop after Gmail accepted a message but before the durable row
    was marked sent. That outcome is unknowable, so stale rows become `failed`
    for explicit review/retry rather than being delivered again automatically.
    """
    now = now or datetime.now(timezone.utc)
    expired = store.list(
        "email_log",
        {"status": "sending", "next_attempt_at": ("lte", now.isoformat())},
        order="created_at asc",
        limit=1000,
    )
    legacy_unleased = store.list(
        "email_log",
        {"status": "sending", "next_attempt_at": None},
        order="created_at asc",
        limit=1000,
    )
    stale_by_id = {str(log["id"]): log for log in [*expired, *legacy_unleased]}
    stale = list(stale_by_id.values())
    for log in stale:
        store.update(
            "email_log",
            log["id"],
            {
                "status": "failed",
                "next_attempt_at": None,
                "error": "Delivery was interrupted before confirmation; review before retrying to avoid a duplicate.",
            },
        )
    return len(stale)


def queue_campaign(campaign_id: str, client_ids: set[str] | None = None) -> dict:
    campaign = store.get("campaigns", campaign_id)
    if not campaign: raise ValueError("Campaign not found")
    templates = [store.get("email_templates", tid) for tid in campaign.get("template_ids", [])]
    templates = [t for t in templates if t and t.get("active")]
    if not templates: raise ValueError("Select at least one active template")
    cfg = app_settings()
    all_clients = store.list("clients", order="created_at asc", limit=10000)
    if client_ids is None:
        clients = [c for c in all_clients if _matches(c, campaign.get("target_filter") or {})]
    else:
        selected = {str(value) for value in client_ids}
        clients = [c for c in all_clients if str(c.get("id")) in selected]
    suppressed = store.list("suppression", order="", limit=10000); logs = store.list("email_log", order="", limit=20000)
    blocked_emails = {str(s.get("email") or "").lower() for s in suppressed}; blocked_domains = {str(s.get("domain") or "").lower() for s in suppressed}
    queued = skipped = 0
    schedule_cfg = _schedule_config(cfg)
    available_senders = {str(row["id"]): row for row in list_gmail_senders(active_only=True, storage=store, cfg=cfg)}
    gmail_accounts = [str(x) for x in (campaign.get("gmail_accounts") or ["1"]) if str(x) in available_senders]
    if campaign.get("provider") == "gmail" and not gmail_accounts:
        raise ValueError("Select at least one active Gmail sender")
    
    global_cap = max(1, int(cfg.get("daily_cap") or 20))
    is_pingram = campaign.get("provider") == "pingram"
    if is_pingram:
        pingram_senders = [str(row["id"]) for row in list_gmail_senders(active_only=True, storage=store, cfg=cfg) if row.get("provider") == "pingram"]
        if not pingram_senders:
            pingram_senders = list(available_senders.keys())
        selected = [str(x) for x in (campaign.get("gmail_accounts") or []) if str(x) in pingram_senders]
        gmail_accounts = selected or pingram_senders
        p_min = max(1, int(cfg.get("pingram_min_delay") or 5))
        p_max = max(p_min, int(cfg.get("pingram_max_delay") or 20))
        global_cap = max(1, int(cfg.get("pingram_daily_cap") or 30))
    elif campaign.get("provider") != "gmail":
        gmail_accounts = ["1"]
        
    now = datetime.now(timezone.utc)
    global_counts, daily_cursors = _global_schedule_state(logs, schedule_cfg, now)
    assigned_accounts: Counter[str] = Counter()

    if is_pingram:
        pingram_counts: Counter[tuple[str, str]] = Counter()
        pingram_assigned: Counter[str] = Counter()
        pingram_cursor = now
        for l in logs:
            if l.get("provider") == "pingram":
                m = _log_counting_time(l)
                if m:
                    acc = str(l.get("sender_account") or "")
                    d = m.astimezone(timezone.utc).date().isoformat()
                    pingram_counts[(acc, d)] += 1
                    if m > pingram_cursor:
                        pingram_cursor = m

    for client in clients:
        if client.get("status") in {"replied", "do_not_contact", "bounced"} or str(client.get("email") or "").lower() in blocked_emails or str(client.get("domain") or "").lower() in blocked_domains:
            skipped += 1; continue
        prior = [l for l in logs if l.get("client_id") == client["id"] and l.get("status") in {"queued", "sending", "sent", "replied"}]
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(campaign.get("resend_block_days") or 90))
        if any(datetime.fromisoformat((l.get("sent_at") or l.get("created_at")).replace("Z", "+00:00")) >= cutoff for l in prior): skipped += 1; continue

        if is_pingram:
            account, cursor, pingram_cursor = _choose_pingram_conveyor_slot(
                gmail_accounts, pingram_cursor, pingram_counts, pingram_assigned,
                global_cap, p_min, p_max
            )
        else:
            account, cursor = _choose_account_slot(gmail_accounts, now, schedule_cfg, global_counts, daily_cursors, global_cap, assigned_accounts)
            _reserve_global_slot(account, cursor, schedule_cfg, global_counts, daily_cursors)
            assigned_accounts[account] += 1

        render_cfg = sender_context(cfg, available_senders.get(account))
        template = random.choice(templates); subject, smissing = render_template(template["subject"], client, render_cfg)
        body, bmissing = render_template(template["body"], client, render_cfg, html_escape=template.get("type") == "html")
        missing = sorted(set(smissing + bmissing))
        if missing:
            store.insert("email_log", {"id": new_id(), "client_id": client["id"], "template_id": template["id"], "campaign_id": campaign_id,
                "provider": campaign["provider"], "subject_sent": subject, "body_sent": body, "status": "skipped", "error": "Missing variables: " + ", ".join(missing), "created_at": now_iso()})
            skipped += 1; continue
        store.insert("email_log", {"id": new_id(), "client_id": client["id"], "template_id": template["id"], "campaign_id": campaign_id,
            "provider": campaign["provider"], "sender_account": account, "subject_sent": subject, "body_sent": body, "scheduled_for": cursor.isoformat(), "status": "queued", "attempt_count": 0, "created_at": now_iso()})
        store.update("clients", client["id"], {"status": "queued", "updated_at": now_iso()}); queued += 1
    previous_state = str(campaign.get("state") or "draft")
    if queued:
        next_state = "paused" if previous_state == "paused" else "running"
    elif previous_state in {"running", "paused"} and any(
        log.get("campaign_id") == campaign_id and log.get("status") in {"queued", "sending"}
        for log in logs
    ):
        next_state = previous_state
    else:
        next_state = "done"
    store.update("campaigns", campaign_id, {"state": next_state, "updated_at": now_iso()})
    return {"queued": queued, "skipped": skipped}


def send_due(limit: int = 25, only_id: str | None = None) -> dict:
    stamp = now_iso(); now = datetime.now(timezone.utc)
    recover_interrupted_sends(now)
    if only_id:
        target = store.get("email_log", only_id)
        logs = [target] if target and target.get("status") in {"queued", "failed"} else []
    else:
        logs = store.list("email_log", {"status": "queued"}, order="scheduled_for asc", limit=1000)
        logs = [x for x in logs if (not x.get("scheduled_for") or x["scheduled_for"] <= stamp) and (not x.get("next_attempt_at") or x["next_attempt_at"] <= stamp)][:limit]
    cfg = app_settings(); schedule_cfg = _schedule_config(cfg); sent = failed = deferred = 0
    all_logs = store.list("email_log", order="", limit=20000)
    sent_today = _sent_today_by_account(all_logs, schedule_cfg, now)
    global_cap = max(1, int(cfg.get("daily_cap") or 20))
    if logs and not only_id:
        all_gmail = all(l.get("provider") != "pingram" for l in logs)
        if all_gmail and not in_send_window(now, schedule_cfg):
            reschedule_queued_emails(now)
            return {"sent": 0, "failed": 0, "deferred": len(logs), "dry_run": env.DRY_RUN}
    needs_reschedule = False
    touched_campaigns: set[str] = set()

    last_pingram_sent_time: datetime | None = None
    for l in all_logs:
        if l.get("provider") == "pingram" and l.get("status") == "sent" and l.get("sent_at"):
            st = _parse_datetime(l.get("sent_at"))
            if st and (last_pingram_sent_time is None or st > last_pingram_sent_time):
                last_pingram_sent_time = st

    for log in logs:
        campaign = store.get("campaigns", log["campaign_id"]); client = store.get("clients", log["client_id"]); template = store.get("email_templates", log["template_id"])
        if not campaign or (campaign.get("state") != "running" and not (only_id and campaign.get("state") == "done")):
            continue
        touched_campaigns.add(str(log["campaign_id"]))
        if not client or client.get("status") in {"replied", "demo_booked", "do_not_contact", "bounced"}:
            store.update("email_log", log["id"], {"status": "skipped", "next_attempt_at": None, "error": "Lead is no longer eligible for outreach."})
            continue
        sender_account = str(log.get("sender_account") or "1")
        is_pingram = log.get("provider") == "pingram"
        active_cap = max(1, int(cfg.get("pingram_daily_cap") or 30)) if is_pingram else global_cap
        
        if not is_pingram and not in_send_window(now, schedule_cfg):
            deferred += 1; needs_reschedule = True; continue
        if sent_today[sender_account] >= active_cap:
            deferred += 1; needs_reschedule = True; continue

        if is_pingram and not only_id:
            if last_pingram_sent_time and (datetime.now(timezone.utc) - last_pingram_sent_time).total_seconds() < 60:
                deferred += 1
                continue
            
        store.update(
            "email_log",
            log["id"],
            {
                "status": "sending",
                "attempt_count": int(log.get("attempt_count") or 0) + 1,
                "next_attempt_at": (now + timedelta(minutes=SEND_LEASE_MINUTES)).isoformat(),
            },
        )
        sender_row = get_gmail_sender(sender_account, storage=store, cfg=cfg)
        identity = sender_context(cfg, sender_row)
        provider = get_provider(log["provider"], cfg, sender_account, gmail_sender=sender_row)
        logger.info(f"Attempting delivery of log {log['id']} to {client.get('email')} via {log['provider']} account {sender_account}")
        try:
            from_name = identity.get("sender_name") or ""
            from_email = getattr(provider, "user", "") or identity.get("sender_email") or env.PINGRAM_FROM_EMAIL
            reply_to = identity.get("reply_to") or identity.get("sender_email") or getattr(provider, "user", "")
                
            result = provider.send(to=client["email"], subject=log["subject_sent"], body=log.get("body_sent") or "", content_type=(template or {}).get("type", "plain"),
                from_address=from_email, from_name=from_name, reply_to=reply_to)
        except Exception as exc:
            # A provider bug or unexpected socket failure must never strand a
            # durable log row in the transient `sending` state.
            logger.exception("Unexpected provider failure for email log %s", log["id"])
            result = SendResult(False, error=f"{type(exc).__name__}: {exc}")
        if result.ok:
            logger.info(f"Successfully sent email {log['id']} to {client.get('email')}, msg_id={result.provider_id}")
            store.update("email_log", log["id"], {"status": "sent", "sent_at": stamp, "provider_message_id": result.provider_id, "next_attempt_at": None, "error": None})
            store.update("clients", client["id"], {"status": "contacted", "last_contacted_at": stamp, "updated_at": stamp}); sent += 1; sent_today[sender_account] += 1
            if is_pingram:
                last_pingram_sent_time = datetime.now(timezone.utc)
        else:
            logger.error(f"Failed delivery of log {log['id']} to {client.get('email')}: {result.error}")
            attempts = int(log.get("attempt_count") or 0) + 1; hard = result.hard_bounce
            terminal = bool(only_id) or hard or not result.retryable or attempts >= 3
            values = {
                "status": "bounced" if hard else ("failed" if terminal else "queued"),
                "error": (result.error or "Unknown delivery failure")[:1000],
                "next_attempt_at": None if terminal else (datetime.now(timezone.utc) + timedelta(minutes=2 ** attempts * 5)).isoformat(),
            }
            store.update("email_log", log["id"], values)
            if hard:
                set_client_status(client["id"], "bounced", storage=store)
            failed += 1
    if needs_reschedule:
        reschedule_queued_emails(now)
    if touched_campaigns:
        refresh_campaign_states(touched_campaigns)
    return {"sent": sent, "failed": failed, "deferred": deferred, "dry_run": env.DRY_RUN}
