"""
The clock: what runs, and how often.

FOUR JOBS
=========
    sync                every N minutes (setting, default 60)
                        the main pipeline: fetch, decide, send

    catalog_refresh     once a day at a quiet hour (setting, default 03:00 ET)
                        download the All Listings Report so we know what
                        Amazon currently shows

    daily_digest        once a day, an hour after the catalogue refresh
                        the summary email

    housekeeping        hourly
                        flush queued alerts, delete expired report files

WHY APSCHEDULER AND NOT CELERY
==============================
This is one job at a time on one machine. Celery would add a broker, a worker
process and a beat process -- three more things to install, monitor and restart
-- to solve a distribution problem that does not exist here. APScheduler runs
inside the web process and needs nothing.

CONCURRENCY IS NOT THE SCHEDULER'S JOB
======================================
Overlap is prevented by a PostgreSQL advisory lock (:func:`app.db.run_lock`),
not by the scheduler's own ``max_instances``. That matters because the lock also
holds across processes: if somebody starts a second container, or clicks "Run
now" while a scheduled run is going, the second one still steps aside. A
scheduler-level guard would only protect against overlap inside one process.

CHANGING THE INTERVAL
=====================
The sync interval is a dashboard setting, so it is re-read on every fire and
the job reschedules itself when it changes. The client can go from hourly to
every 15 minutes without a restart, which was the point.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import notifier, services
from app.amazon.reports import ReportError, fetch_all_listings
from app.config import settings
from app.core import settings_store
from app.db import run_lock, session_scope
from app.engine.pipeline import execute_run
from app.engine.report_builder import prune_old_reports
from app.models import (
    AmazonListing,
    CatalogSync,
    RunTrigger,
    utcnow,
)

log = logging.getLogger(__name__)

JOB_SYNC = "sync"
JOB_CATALOG = "catalog_refresh"
JOB_DIGEST = "daily_digest"
JOB_HOUSEKEEPING = "housekeeping"

_scheduler: BackgroundScheduler | None = None


# ===========================================================================
# Jobs
# ===========================================================================

def job_sync() -> None:
    """
    The main pipeline.

    Takes the exclusive lock; if another run holds it, this one steps aside
    quietly. That is the correct behaviour for a job firing every 15 minutes --
    queueing would build a backlog of identical work.
    """
    with run_lock() as acquired:
        if not acquired:
            log.info("a run is already in progress; skipping this cycle")
            return

        with session_scope() as session:
            vendor = services.vendor_credentials(session)
            if vendor is None:
                log.warning(
                    "sync skipped: the vendor connection is not configured. "
                    "See the banner on the dashboard."
                )
                return

            client = services.amazon_client(session)
            outcome = execute_run(
                session,
                client=client,
                vendor_credentials=vendor,
                trigger=RunTrigger.SCHEDULE,
                triggered_by="scheduler",
            )
            log.info("run %d finished: %s -- %s", outcome.run_id, outcome.status.value, outcome.message)

            if client is not None:
                client.close()

            cfg = settings_store.get_all(session)
            if cfg.get("alert_on_every_run") and outcome.ok:
                notifier.queue(
                    session,
                    kind="run_summary",
                    severity="info",
                    subject=f"Inventory sync run {outcome.run_id}: {outcome.status.value}",
                    body=outcome.message,
                    run_id=outcome.run_id,
                )
            notifier.flush_queue(session)

    _reschedule_sync_if_needed()


def job_catalog_refresh() -> None:
    """
    Download the All Listings Report and update our picture of Amazon.

    Everything depends on this. Without it the decision engine has no idea what
    Amazon currently shows, so it cannot tell a real difference from a stale
    one -- and the whole self-healing property of the design comes from
    comparing against Amazon's own numbers.

    Takes the same lock as the sync: they both write ``amazon_listings``, and a
    refresh landing halfway through a decision pass would produce a run based
    on two different snapshots.
    """
    with run_lock() as acquired:
        if not acquired:
            log.info("catalogue refresh skipped: a run is in progress")
            return

        with session_scope() as session:
            client = services.amazon_client(session, force_dry_run=True)  # read-only work
            if client is None:
                # Alerted, not just logged. This is a dead end the operator has
                # to act on, and pressing the dashboard button and receiving
                # absolutely nothing back is the worst possible response to it.
                log.warning("catalogue refresh skipped: Amazon is not configured")
                notifier.queue(
                    session,
                    kind="auth_failure",
                    severity="warning",
                    subject="Could not refresh the Amazon catalogue",
                    body=(
                        "Amazon is not fully configured, so there was nothing to ask. "
                        "It needs the Client ID and Seller ID in the .env file, and the "
                        "Client Secret and Refresh Token saved in Settings under "
                        "Credentials.\n\n"
                        "Press 'Test Amazon' on the Settings page to see which part is "
                        "missing."
                    ),
                )
                notifier.flush_queue(session)
                return

            sync = CatalogSync(status="running")
            session.add(sync)
            session.flush()

            try:
                records, stats, meta = fetch_all_listings(
                    client, snapshot_dir=settings.backups_dir
                )
            except Exception as exc:
                # DELIBERATELY BROAD. This was `except ReportError`, so a
                # ReportError produced a clear dashboard alert and anything else
                # -- an expired refresh token, a 403 from a missing role, a
                # permissions problem writing the snapshot -- escaped into
                # APScheduler, which logs it and moves on. On a Windows
                # Scheduled Task that log went to a discarded stdout, so the
                # button did nothing, said nothing, and left no trace anywhere.
                #
                # The operator's question is always "why did nothing happen?",
                # and the type of the exception is not what decides whether they
                # deserve an answer. The class name is included because the
                # message alone is often not enough to tell an authentication
                # failure from a disk failure.
                sync.status = "failed"
                sync.error = f"{type(exc).__name__}: {exc}"
                sync.finished_at = utcnow()
                log.exception("catalogue refresh failed")
                detail = str(exc) if isinstance(exc, ReportError) else f"{type(exc).__name__}: {exc}"
                notifier.queue(
                    session,
                    kind="auth_failure",
                    severity="warning",
                    subject="Could not refresh the Amazon catalogue",
                    body=(
                        f"{detail}\n\n"
                        "The system is still using the previous snapshot, so nothing is "
                        "broken - the figures are just older. It will try again "
                        "tomorrow, or you can retry now from the dashboard."
                    ),
                )
                notifier.flush_queue(session)
                return
            finally:
                client.close()

            sync.report_id = meta.get("report_id")
            sync.report_document_id = meta.get("report_document_id")
            sync.snapshot_path = meta.get("snapshot_path")
            sync.listing_count = stats.parsed_rows

            cfg = settings_store.get_all(session)
            prefixes = {p.upper() for p in cfg["sku_prefixes_in_scope"]}

            # Everything currently known, so listings that have disappeared can
            # be marked rather than silently left behind with a stale quantity.
            existing = {
                lst.seller_sku: lst
                for lst in session.query(AmazonListing).all()
            }
            seen: set[str] = set()
            new_count = 0
            in_scope = 0

            for rec in records:
                seen.add(rec.seller_sku)
                if (rec.sku_prefix or "").upper() in prefixes:
                    in_scope += 1

                row = existing.get(rec.seller_sku)
                if row is None:
                    row = AmazonListing(seller_sku=rec.seller_sku)
                    session.add(row)
                    new_count += 1

                row.sku_prefix = rec.sku_prefix
                row.barcode = rec.barcode or None
                row.asin = rec.asin
                row.quantity = rec.quantity
                row.price = rec.price
                row.status = rec.status
                # The sample report this was built against had no
                # fulfillment-channel column and the client confirmed everything
                # is merchant-fulfilled, so DEFAULT is the fallback. The live
                # report does carry the column, which is how the smuggled
                # attribute this used to read was finally found to be
                # unassignable. Read straight off the record now.
                row.fulfillment_channel = rec.fulfillment_channel or (
                    row.fulfillment_channel or "DEFAULT"
                )
                row.blacklisted = rec.seller_sku in set(cfg.get("blacklisted_skus") or [])
                row.present_in_last_sync = True
                row.synced_at = utcnow()

            disappeared = 0
            for sku, row in existing.items():
                if sku not in seen and row.present_in_last_sync:
                    row.present_in_last_sync = False
                    disappeared += 1

            sync.in_scope_count = in_scope
            sync.new_count = new_count
            sync.disappeared_count = disappeared
            sync.status = "completed"
            sync.finished_at = utcnow()

            log.info(
                "catalogue refreshed: %d listings (%d in scope), %d new, %d gone",
                stats.parsed_rows, in_scope, new_count, disappeared,
            )
            notifier.flush_queue(session)


def job_daily_digest() -> None:
    """Send the daily summary."""
    with session_scope() as session:
        notifier.send_daily_digest(session)
        notifier.flush_queue(session)


def job_housekeeping() -> None:
    """
    Flush queued alerts and delete expired report files.

    Pruning matters more than it sounds: five reports per run at a 15-minute
    cycle is 480 files a day, and the Current In Stock report is several
    megabytes. Without this a small VPS fills up in weeks, and a full disk
    stops the sync entirely.
    """
    with session_scope() as session:
        notifier.flush_queue(session)
        keep = int(settings_store.get(session, "report_retention_days") or 90)

    removed = 0
    if settings.reports_dir.exists():
        for directory in settings.reports_dir.iterdir():
            if directory.is_dir():
                removed += prune_old_reports(directory, keep_days=keep)
            elif directory.suffix == ".xlsx":
                removed += prune_old_reports(settings.reports_dir, keep_days=keep)
                break
    if removed:
        log.info("housekeeping removed %d expired report files", removed)


# ===========================================================================
# Wiring
# ===========================================================================

def _timezone() -> ZoneInfo:
    """The configured timezone, falling back rather than crashing on a typo."""
    with session_scope() as session:
        name = str(settings_store.get(session, "timezone") or "America/New_York")
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover
        log.warning("timezone %r is not recognised; using America/New_York", name)
        return ZoneInfo("America/New_York")


def _sync_trigger(interval_minutes: int) -> IntervalTrigger:
    """
    The sync trigger, anchored so restarts cannot move the timetable.

    WHY AN ANCHOR AT ALL
    ====================
    An interval trigger with no start date begins counting from the moment the
    process starts. Every restart therefore re-phased the whole schedule: checks
    that had been landing at 17 past moved to 31 past, then to 09 past, then
    wherever the next restart happened to fall. During a week of updates the
    timetable wandered right around the clock.

    That is worse than untidy. The vendor publishes on a fixed clock -- the full
    feed at about 8 PM their time -- so when our checks happen decides how long
    a new file waits before anyone sees it, and a schedule that moves cannot be
    reasoned about at all. It also made the deployment harder to read: an
    operator who restarts to pick up a change should not have to work out
    whether a run that just appeared was caused by the restart.

    Anchoring to midnight plus an offset makes the times deterministic and
    identical after every restart, and it works for any interval rather than
    only for hour-divisible ones: 60 minutes with an offset of 10 gives 10 past
    every hour, 15 minutes gives 10, 25, 40 and 55 past.

    APScheduler computes the next fire time forward from the anchor, so a
    restart lands on the same grid it was already on instead of starting a new
    one -- and lands on the NEXT slot, which is why restarting no longer fires
    a run of its own.
    """
    tz = _timezone()
    with session_scope() as session:
        offset = int(settings_store.get(session, "sync_offset_minutes") or 0)

    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    anchor = midnight + timedelta(minutes=max(0, min(59, offset)))
    return IntervalTrigger(minutes=interval_minutes, start_date=anchor, timezone=tz)


def _current_interval_minutes() -> int:
    with session_scope() as session:
        return int(settings_store.get(session, "sync_interval_minutes") or 60)


def _reschedule_sync_if_needed() -> None:
    """
    Re-read the interval and reschedule if the client changed it.

    Done after each run rather than on a timer, so a change takes effect from
    the next cycle without a restart.
    """
    global _scheduler
    if _scheduler is None:
        return

    job = _scheduler.get_job(JOB_SYNC)
    if job is None:
        return

    wanted = _current_interval_minutes()
    current = getattr(job.trigger, "interval", None)
    current_minutes = int(current.total_seconds() // 60) if current else None

    if current_minutes != wanted:
        log.info("sync interval changed from %s to %s minutes; rescheduling", current_minutes, wanted)
        _scheduler.reschedule_job(JOB_SYNC, trigger=_sync_trigger(wanted))


def start() -> BackgroundScheduler | None:
    """
    Start the scheduler. Returns it, or None when scheduling is disabled.

    Called from the FastAPI lifespan handler. Disabled by ``ENABLE_SCHEDULER``
    so a second container can serve the dashboard without also running the
    jobs -- exactly one process must own them.
    """
    global _scheduler

    if not settings.enable_scheduler:
        log.info("scheduler disabled by configuration")
        return None
    if _scheduler is not None:  # pragma: no cover
        return _scheduler

    tz = _timezone()
    interval = _current_interval_minutes()

    with session_scope() as session:
        catalog_hour = int(settings_store.get(session, "catalog_refresh_hour") or 3)

    scheduler = BackgroundScheduler(
        timezone=tz,
        job_defaults={
            # The advisory lock already prevents overlap; this stops a slow run
            # from stacking up scheduler threads behind it.
            "max_instances": 1,
            # If the process was down, run once on return rather than firing
            # every missed cycle in a burst.
            "coalesce": True,
            # A run may legitimately take a while: a 1.15-million-row full feed
            # plus 5,000 Amazon writes. Being late is not a reason to skip.
            "misfire_grace_time": 3600,
        },
    )

    scheduler.add_job(
        job_sync,
        trigger=_sync_trigger(interval),
        id=JOB_SYNC,
        name="Check the vendor and sync quantities",
        replace_existing=True,
        # NO next_run_time HERE, DELIBERATELY. It used to be set to
        # datetime.now(), directly under a comment claiming the opposite --
        # "not immediately on boot". It fired a run the instant the process
        # started, and that is only half the damage: once APScheduler has a
        # previous fire time it computes the next one as previous + interval
        # and stops consulting the trigger's anchor at all. So the entire
        # timetable re-based itself on whatever minute the server happened to
        # be restarted, and the "minutes past the hour" setting did nothing
        # whatsoever. Restarting six times in an evening moved the schedule
        # six times, and an operator restarting to pick up a change could not
        # tell that run apart from a real one.
        #
        # Left to the trigger, the first fire is the next slot on the anchored
        # grid -- which also satisfies what that comment was reaching for far
        # better than firing instantly did: the web process gets time to
        # finish starting, and there is a window in which to pause the system
        # mid-deployment.
    )

    scheduler.add_job(
        job_catalog_refresh,
        trigger=CronTrigger(hour=catalog_hour, minute=0, timezone=tz),
        id=JOB_CATALOG,
        name="Refresh the Amazon catalogue",
        replace_existing=True,
    )

    scheduler.add_job(
        job_daily_digest,
        trigger=CronTrigger(hour=(catalog_hour + 1) % 24, minute=30, timezone=tz),
        id=JOB_DIGEST,
        name="Send the daily summary",
        replace_existing=True,
    )

    scheduler.add_job(
        job_housekeeping,
        trigger=IntervalTrigger(hours=1),
        id=JOB_HOUSEKEEPING,
        name="Send queued alerts and tidy up old reports",
        replace_existing=True,
    )

    scheduler.start()
    _scheduler = scheduler

    log.info(
        "scheduler started: sync every %d min, catalogue at %02d:00 %s",
        interval, catalog_hour, tz.key,
    )
    return scheduler


def shutdown() -> None:
    """Stop the scheduler, letting a running job finish."""
    global _scheduler
    if _scheduler is not None:
        log.info("stopping the scheduler; waiting for any running job")
        _scheduler.shutdown(wait=True)
        _scheduler = None


def status() -> list[dict]:
    """
    What is scheduled and when it next fires, for the dashboard.

    The "next run" time is one of the first things an operator looks for when
    something seems stuck, so it is worth surfacing prominently.
    """
    if _scheduler is None:
        return []
    return [
        {
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
        }
        for job in _scheduler.get_jobs()
    ]


def trigger_now(job_id: str) -> bool:
    """
    Run a job immediately, from the dashboard's "Run now" button.

    Does not bypass anything: the job still takes the lock, still checks the
    pause switch, and still honours the mode.
    """
    if _scheduler is None:
        return False
    job = _scheduler.get_job(job_id)
    if job is None:
        return False
    job.modify(next_run_time=datetime.now(_timezone()))
    log.info("job %s triggered manually", job_id)
    return True
