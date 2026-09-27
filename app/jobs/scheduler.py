"""Durable database-backed worker for email and scrape jobs."""
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.core import billing
from app.core.scraper import process_scrape_job
from app.core.freight import poll_freight_replies, recover_uncertain_freight_sends
from app.core.sender import reschedule_queued_emails, send_due
from app.core.tenancy import ADMIN_OWNER_ID, OwnerStore, freight_owner_ids, outreach_owner_ids
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


# In-process worker liveness. The scheduler lives in this process, so this is
# the truthful source for "is the worker ticking right now"; /health exposes it.
_heartbeat: dict = {}


def heartbeat() -> dict:
    return {
        "enabled": settings.SCHEDULER_ENABLED,
        "interval_seconds": max(settings.SCHEDULER_INTERVAL_SECONDS, 10),
        "scheduler_running": bool(_scheduler and getattr(_scheduler, "running", False)),
        **_heartbeat,
    }


def tick():
    started_at = now_iso()
    logger.info("worker tick started")
    error = None
    try:
        _tick()
    except Exception as exc:
        error = str(exc)[:500]
        logger.exception("worker tick failed")
    _heartbeat.update({
        "last_tick_started_at": started_at,
        "last_tick_finished_at": now_iso(),
        "last_error": error,
    })
    logger.info("worker tick finished")


def _tick():
    try: recover_stale_jobs()
    except Exception: logger.exception("stale job recovery failed")
    try: owners = outreach_owner_ids(store)
    except Exception: logger.exception("outreach owner lookup failed"); owners = []
    for owner in owners:
        # Each owner's campaign queue runs in its own scope. A lapsed
        # subscription pauses its queued sends (preserved, never dropped)
        # until Stripe confirms the account active again.
        if not billing.owner_has_access(store, owner):
            continue
        try: send_due(limit=10, storage=OwnerStore(store, owner))
        except Exception: logger.exception("email worker tick failed for %s", owner)
    try: owners = freight_owner_ids(store)
    except Exception: logger.exception("freight owner lookup failed"); owners = []
    for owner in owners:
        # Each customer's drafts, threads and inboxes are processed in their own scope.
        # Lapsed accounts pause freight work the same way.
        if not billing.owner_has_access(store, owner):
            continue
        scoped = OwnerStore(store, owner)
        try: recover_uncertain_freight_sends(scoped)
        except Exception: logger.exception("freight send recovery tick failed for %s", owner)
        try: poll_freight_replies(scoped)
        except Exception: logger.exception("freight reply monitor tick failed for %s", owner)
    # Drain up to WORKER_JOBS_PER_TICK queued jobs so discovery queues do not
    # back up behind the tick interval (one job per tick capped throughput at
    # 6 jobs/minute on the fastest allowed interval).
    for _ in range(max(int(settings.WORKER_JOBS_PER_TICK), 1)):
        work = None
        try:
            jobs = store.list("jobs", {"status": "queued", "run_after": ("lte", now_iso())}, order="created_at asc", limit=1)
            if not jobs: break
            work = jobs[0]; store.update("jobs", work["id"], {"status": "running", "locked_at": now_iso(), "updated_at": now_iso()})
            scoped = OwnerStore(store, str(work.get("owner_id") or ADMIN_OWNER_ID))
            if work["kind"] == "scrape": process_scrape_job(work["payload"]["scrape_job_id"], storage=scoped)
            elif work["kind"] == "reschedule_email_queue": reschedule_queued_emails(storage=scoped)
            else: raise ValueError(f"Unknown job kind: {work['kind']}")
            store.update("jobs", work["id"], {"status": "done", "updated_at": now_iso()})
        except Exception as exc:
            logger.exception("background job failed")
            if work is not None:
                attempts = int(work.get("attempts") or 0) + 1
                retry_at = (datetime.now(timezone.utc) + timedelta(minutes=2 ** attempts * 5)).isoformat()
                store.update("jobs", work["id"], {"status": "failed" if attempts >= 3 else "queued", "attempts": attempts, "run_after": retry_at, "locked_at": None, "error": str(exc)[:1000], "updated_at": now_iso()})


def start():
    global _scheduler
    if _scheduler or not settings.SCHEDULER_ENABLED:
        logger.info("worker scheduler not started (already running=%s, SCHEDULER_ENABLED=%s)", bool(_scheduler), settings.SCHEDULER_ENABLED)
        return _scheduler
    interval = max(settings.SCHEDULER_INTERVAL_SECONDS, 10)
    _scheduler = BackgroundScheduler(daemon=True)
    # First tick runs immediately so a startup failure surfaces now, not one
    # interval later; subsequent ticks follow the configured interval.
    _scheduler.add_job(tick, "interval", seconds=interval, id="worker", max_instances=1, coalesce=True,
                       next_run_time=datetime.now(timezone.utc))
    _scheduler.start()
    logger.info("worker scheduler started: interval=%ss (SCHEDULER_INTERVAL_SECONDS=%s)", interval, settings.SCHEDULER_INTERVAL_SECONDS)
    return _scheduler
