"""Durable database-backed worker for email and scrape jobs."""
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.core.scraper import process_scrape_job
from app.core.freight import poll_freight_replies, recover_uncertain_freight_sends
from app.core.sender import reschedule_queued_emails, send_due
from app.db import now_iso, store

logger = logging.getLogger(__name__)
_scheduler = None
WORK_LEASE_MINUTES = 15


def recover_stale_jobs(now: datetime | None = None) -> int:
    """Return work abandoned by a stopped process to the durable queue."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=WORK_LEASE_MINUTES)).isoformat()
    stale = store.list(
        "jobs",
        {"status": "running", "locked_at": ("lte", cutoff)},
        order="created_at asc",
        limit=100,
    )
    for work in stale:
        attempts = int(work.get("attempts") or 0) + 1
        terminal = attempts >= 3
        error = "Worker stopped before this job completed."
        store.update(
            "jobs",
            work["id"],
            {
                "status": "failed" if terminal else "queued",
                "attempts": attempts,
                "run_after": (now + timedelta(minutes=2 ** attempts * 5)).isoformat(),
                "locked_at": None,
                "error": error,
                "updated_at": now.isoformat(),
            },
        )
        if terminal and work.get("kind") == "scrape" and (work.get("payload") or {}).get("scrape_job_id"):
            store.update(
                "scrape_jobs",
                work["payload"]["scrape_job_id"],
                {"status": "failed", "error": error, "completed_at": now.isoformat(), "updated_at": now.isoformat()},
            )
    return len(stale)


def tick():
    try: recover_stale_jobs()
    except Exception: logger.exception("stale job recovery failed")
    try: send_due(limit=10)
    except Exception: logger.exception("email worker tick failed")
    try: recover_uncertain_freight_sends()
    except Exception: logger.exception("freight send recovery tick failed")
    try: poll_freight_replies()
    except Exception: logger.exception("freight reply monitor tick failed")
    try:
        jobs = store.list("jobs", {"status": "queued", "run_after": ("lte", now_iso())}, order="created_at asc", limit=1)
        if not jobs: return
        work = jobs[0]; store.update("jobs", work["id"], {"status": "running", "locked_at": now_iso(), "updated_at": now_iso()})
        if work["kind"] == "scrape": process_scrape_job(work["payload"]["scrape_job_id"])
        elif work["kind"] == "reschedule_email_queue": reschedule_queued_emails()
        else: raise ValueError(f"Unknown job kind: {work['kind']}")
        store.update("jobs", work["id"], {"status": "done", "updated_at": now_iso()})
    except Exception as exc:
        logger.exception("background job failed")
        if 'work' in locals():
            attempts = int(work.get("attempts") or 0) + 1
            retry_at = (datetime.now(timezone.utc) + timedelta(minutes=2 ** attempts * 5)).isoformat()
            store.update("jobs", work["id"], {"status": "failed" if attempts >= 3 else "queued", "attempts": attempts, "run_after": retry_at, "locked_at": None, "error": str(exc)[:1000], "updated_at": now_iso()})


def start():
    global _scheduler
    if _scheduler or not settings.SCHEDULER_ENABLED: return _scheduler
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(tick, "interval", seconds=max(settings.SCHEDULER_INTERVAL_SECONDS, 15), id="worker", max_instances=1, coalesce=True)
    _scheduler.start(); return _scheduler
