from __future__ import annotations

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..config import get_settings
from ..services import templates
from . import customer_sync, order_refresh, session_cleanup, workflow_timer

log = structlog.get_logger(__name__)
scheduler = AsyncIOScheduler()


def start() -> None:
    s = get_settings()
    scheduler.add_job(customer_sync.run, CronTrigger.from_crontab(s.customer_sync_cron), id="customer_sync", replace_existing=True, misfire_grace_time=3600)
    scheduler.add_job(order_refresh.run, IntervalTrigger(minutes=s.order_refresh_minutes), id="order_refresh", replace_existing=True, misfire_grace_time=120)
    scheduler.add_job(session_cleanup.run, IntervalTrigger(minutes=5), id="session_cleanup", replace_existing=True)
    scheduler.add_job(order_refresh.check_stale, IntervalTrigger(minutes=5), id="stale_check", replace_existing=True)
    scheduler.add_job(templates.load_from_db, IntervalTrigger(seconds=60), id="templates_reload", replace_existing=True)
    # Wait steps in live workflows: WATI caps a pause at 10 minutes, so a few seconds late is fine.
    scheduler.add_job(workflow_timer.run, IntervalTrigger(seconds=5), id="workflow_timer", replace_existing=True,
                      max_instances=1, coalesce=True)
    scheduler.start()
    log.info("scheduler_started", customer_sync_cron=s.customer_sync_cron, order_refresh_minutes=s.order_refresh_minutes)


def reschedule() -> None:
    """Re-read the schedule from the current settings (called after the dashboard saves them)."""
    if not scheduler.running:
        return
    s = get_settings()
    try:
        scheduler.reschedule_job("customer_sync", trigger=CronTrigger.from_crontab(s.customer_sync_cron))
        scheduler.reschedule_job("order_refresh", trigger=IntervalTrigger(minutes=s.order_refresh_minutes))
        log.info("scheduler_rescheduled", customer_sync_cron=s.customer_sync_cron, order_refresh_minutes=s.order_refresh_minutes)
    except Exception as e:  # noqa: BLE001 - a bad cron must not take the scheduler down
        log.error("reschedule_failed", error=str(e))


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def jobs_info() -> list[dict]:
    out = []
    for j in scheduler.get_jobs():
        out.append({"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else None, "trigger": str(j.trigger)})
    return out
