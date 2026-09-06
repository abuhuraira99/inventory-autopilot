"""
A small JSON API.

WHAT IT IS FOR
==============
Two things, and deliberately not more:

  1. **The dashboard's live tiles.** ``/api/status`` is polled every 20 seconds
     so the status page updates without a full reload. Keeping that as one
     endpoint returning one small object means the page makes one request, not
     six.

  2. **The client's own tooling, later.** The agency runs other systems. Being
     able to ask "is the inventory sync healthy" from somewhere else is worth
     the twenty lines it costs.

WHAT IT IS NOT FOR
==================
There is no API route that changes anything. Every mutation lives in
:mod:`app.routers.actions` behind a form POST with a session cookie and, where
it matters, a typed confirmation. An API key that could zero 45,000 listings
would be a liability with no compensating benefit -- the only consumer is a
page served from the same origin.

Authentication is the same session cookie as the dashboard, so these endpoints
are exactly as protected as the pages and there is no second credential to
manage or leak.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app import __version__, scheduler
from app.core import settings_store
from app.db import get_session, healthcheck
from app.models import (
    AmazonListing,
    BatchStatus,
    CatalogSync,
    FeedFile,
    FileStatus,
    Notification,
    PushBatch,
    Run,
    RunStatus,
    UnmappedBarcode,
    VendorProduct,
    utcnow,
)
from app.routers.helpers import require_login
from app.security.auth import SessionData

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/status")
async def status(
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> dict:
    """
    Everything the dashboard's live tiles need, in one object.

    One endpoint rather than six, so the polling costs one request. Kept small
    on purpose -- it is fetched every 20 seconds.
    """
    cfg = settings_store.get_all(session)
    prefixes = [p.upper() for p in cfg["sku_prefixes_in_scope"]]

    last_run = session.execute(
        select(Run).order_by(desc(Run.started_at)).limit(1)
    ).scalar_one_or_none()

    since = utcnow() - timedelta(hours=24)
    day = session.execute(
        select(
            func.count(Run.id),
            func.coalesce(func.sum(Run.pushed_changes), 0),
            func.coalesce(func.sum(Run.proposed_changes), 0),
        ).where(Run.started_at >= since)
    ).one()

    awaiting = session.execute(
        select(func.count(PushBatch.id)).where(
            PushBatch.status == BatchStatus.AWAITING_APPROVAL
        )
    ).scalar() or 0

    alerts = session.execute(
        select(func.count(Notification.id)).where(
            Notification.acknowledged.is_(False), Notification.severity != "info"
        )
    ).scalar() or 0

    unmatched = session.execute(
        select(func.count(UnmappedBarcode.barcode)).where(
            UnmappedBarcode.resolved.is_(False)
        )
    ).scalar() or 0

    last_catalog = session.execute(
        select(CatalogSync)
        .where(CatalogSync.status == "completed")
        .order_by(desc(CatalogSync.finished_at))
        .limit(1)
    ).scalar_one_or_none()

    jobs = scheduler.status()
    next_sync = next(
        (j["next_run"] for j in jobs if j["id"] == scheduler.JOB_SYNC), None
    )

    return {
        "version": __version__,
        # -- the two things somebody glances at ---------------------------
        "paused": bool(cfg["paused"]),
        "mode": cfg["sync_mode"],
        # -- attention ----------------------------------------------------
        "needs_attention": {
            "batches_awaiting_approval": awaiting,
            "unacknowledged_alerts": alerts,
            "unmatched_in_stock": unmatched,
        },
        # -- last run -----------------------------------------------------
        "last_run": (
            {
                "id": last_run.id,
                "status": last_run.status.value,
                "started_at": last_run.started_at.isoformat(),
                "finished_at": last_run.finished_at.isoformat() if last_run.finished_at else None,
                "duration_seconds": last_run.duration_seconds,
                "files_processed": last_run.files_processed,
                "rows_read": last_run.rows_read,
                "proposed": last_run.proposed_changes,
                "pushed": last_run.pushed_changes,
                "message": last_run.guardrail_message,
            }
            if last_run
            else None
        ),
        "next_sync": next_sync,
        "sync_interval_minutes": cfg["sync_interval_minutes"],
        # -- 24 hours ------------------------------------------------------
        "last_24h": {"runs": day[0], "pushed": day[1], "proposed": day[2]},
        # -- catalogue -----------------------------------------------------
        "catalog": (
            {
                "synced_at": last_catalog.finished_at.isoformat() if last_catalog.finished_at else None,
                "listings": last_catalog.listing_count,
                "in_scope": last_catalog.in_scope_count,
            }
            if last_catalog
            else None
        ),
        "counts": {
            "vendor_products": session.execute(
                select(func.count(VendorProduct.barcode))
            ).scalar() or 0,
            "vendor_in_stock": session.execute(
                select(func.count(VendorProduct.barcode)).where(VendorProduct.stock > 0)
            ).scalar() or 0,
            "listings_in_scope": session.execute(
                select(func.count(AmazonListing.seller_sku)).where(
                    AmazonListing.present_in_last_sync.is_(True),
                    func.upper(AmazonListing.sku_prefix).in_(prefixes),
                )
            ).scalar() or 0,
        },
    }


@router.get("/runs")
async def recent_runs(
    limit: int = 20,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> dict:
    """Recent runs, newest first."""
    limit = max(1, min(limit, 200))
    rows = list(
        session.execute(select(Run).order_by(desc(Run.started_at)).limit(limit)).scalars()
    )
    return {
        "runs": [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat(),
                "status": r.status.value,
                "mode": r.mode.value,
                "trigger": r.trigger.value,
                "triggered_by": r.triggered_by,
                "files_processed": r.files_processed,
                "rows_read": r.rows_read,
                "proposed": r.proposed_changes,
                "pushed": r.pushed_changes,
                "unmapped": r.unmapped_count,
                "duration_seconds": r.duration_seconds,
                "message": r.guardrail_message,
                "error": r.error,
            }
            for r in rows
        ]
    }


@router.get("/feed-files")
async def feed_files(
    limit: int = 30,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> dict:
    """
    Recent vendor files and what happened to each.

    Useful for answering "did today's full feed arrive?" without reading the
    log -- which is the first question when the numbers look stale.
    """
    limit = max(1, min(limit, 200))
    rows = list(
        session.execute(
            select(FeedFile).order_by(desc(FeedFile.discovered_at)).limit(limit)
        ).scalars()
    )
    return {
        "files": [
            {
                "filename": f.filename,
                "kind": f.kind.value,
                "feed_date": f.feed_date.date().isoformat() if f.feed_date else None,
                "sequence": f.sequence,
                "status": f.status.value,
                "rows": f.row_count,
                "rejected": f.rejected_row_count,
                "size_bytes": f.size_bytes,
                "processed_at": f.processed_at.isoformat() if f.processed_at else None,
                "quarantine_reason": f.quarantine_reason,
            }
            for f in rows
        ]
    }


@router.get("/health/detail")
async def health_detail(
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> dict:
    """
    A fuller health check, for whoever is on call.

    Authenticated, unlike ``/health``: this one names what is misconfigured,
    which is exactly the sort of thing an unauthenticated endpoint should not
    hand out.
    """
    from app.services import readiness

    db_ok, db_detail = healthcheck()
    ready = readiness(session)
    cfg = settings_store.get_all(session)

    quarantined = session.execute(
        select(func.count(FeedFile.id)).where(FeedFile.status == FileStatus.QUARANTINED)
    ).scalar() or 0

    stuck = session.execute(
        select(func.count(Run.id)).where(
            Run.status == RunStatus.RUNNING,
            Run.started_at < utcnow() - timedelta(hours=2),
        )
    ).scalar() or 0

    concerns: list[str] = []
    if not db_ok:
        concerns.append(f"database: {db_detail}")
    concerns.extend(ready.problems)
    if quarantined:
        concerns.append(f"{quarantined} vendor file(s) were quarantined and need attention")
    if stuck:
        concerns.append(
            f"{stuck} run(s) have been marked RUNNING for over two hours - the process "
            "may have been killed mid-run"
        )
    if cfg["paused"]:
        concerns.append("the system is paused, so nothing is being synced")

    return {
        "healthy": db_ok and not ready.problems and not stuck,
        "database": "ok" if db_ok else db_detail,
        "vendor_configured": ready.vendor_ready,
        "amazon_configured": ready.amazon_ready,
        "paused": cfg["paused"],
        "mode": cfg["sync_mode"],
        "concerns": concerns,
        "warnings": ready.warnings,
        "scheduler_jobs": scheduler.status(),
    }


@router.get("/coverage")
async def coverage(
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> dict:
    """
    The Stage 0 coverage number, live.

    How much of the in-scope Amazon catalogue can be matched to a vendor row.
    This was the first question the project answered (99.5% on 4 September
    2026) and it is worth being able to re-ask it at any time: a sudden drop
    means the vendor changed their barcodes or somebody renamed SKUs.
    """
    cfg = settings_store.get_all(session)
    prefixes = [p.upper() for p in cfg["sku_prefixes_in_scope"]]
    if not prefixes:
        return {"measured": False, "reason": "no SKU prefixes are in scope"}

    in_scope = session.execute(
        select(func.count(AmazonListing.seller_sku)).where(
            AmazonListing.present_in_last_sync.is_(True),
            func.upper(AmazonListing.sku_prefix).in_(prefixes),
        )
    ).scalar() or 0

    matched = session.execute(
        select(func.count(AmazonListing.seller_sku))
        .select_from(AmazonListing)
        .join(VendorProduct, VendorProduct.barcode == AmazonListing.barcode)
        .where(
            AmazonListing.present_in_last_sync.is_(True),
            func.upper(AmazonListing.sku_prefix).in_(prefixes),
        )
    ).scalar() or 0

    by_prefix = [
        {"prefix": p, "listings": n}
        for p, n in session.execute(
            select(AmazonListing.sku_prefix, func.count())
            .where(AmazonListing.present_in_last_sync.is_(True))
            .group_by(AmazonListing.sku_prefix)
            .order_by(desc(func.count()))
            .limit(20)
        ).all()
    ]

    return {
        "measured": in_scope > 0,
        "prefixes_in_scope": cfg["sku_prefixes_in_scope"],
        "in_scope_listings": in_scope,
        "matched_to_vendor": matched,
        "not_in_vendor_feed": in_scope - matched,
        "coverage_percent": round(100.0 * matched / in_scope, 2) if in_scope else 0.0,
        "all_prefixes_on_account": by_prefix,
    }
