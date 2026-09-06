"""
The pages the client actually looks at.

INFORMATION DESIGN
==================
A dashboard is operated, not read. So every page answers its question in the
first screenful and puts the detail below:

    /                  Is it working, and does anything need me?
    /runs              What has it been doing?
    /runs/{id}         Exactly what did this run change, and why?
    /products          What does the vendor have, and what does Amazon show?
    /unmatched         Which products could we not match, and what should I do?
    /reports           Give me the five files.
    /audit             Who changed what, and when?

Numbers are grouped, statuses carry a semantic colour, and anything needing a
decision is at the top. "Nothing to change" is presented as a good outcome
rather than a null result, because on a healthy day that is what most runs
report and it should not look like a failure.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import Integer, desc, func, select
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import settings_store
from app.db import get_session
from app.engine.report_builder import REPORT_DESCRIPTIONS, REPORT_TITLES
from app.models import (
    AmazonListing,
    AuditEvent,
    BatchStatus,
    CatalogSync,
    FeedFile,
    ItemResult,
    Notification,
    PushBatch,
    PushItem,
    ReportFile,
    Run,
    RunStatus,
    UnmappedBarcode,
    VendorProduct,
    utcnow,
)
from app.routers.helpers import render, require_login
from app.security.auth import SessionData

log = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])

PAGE_SIZE = 50


# ===========================================================================
# Home
# ===========================================================================

@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    flash: str = "",
    tone: str = "good",
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> HTMLResponse:
    """
    The status page.

    Ordered by urgency: anything waiting for a human first, then the live
    state, then the numbers, then recent history. Somebody opening this at 6am
    should be able to tell in three seconds whether they need to do anything.
    """
    # -- things that need a person -----------------------------------------
    awaiting = list(
        session.execute(
            select(PushBatch)
            .where(PushBatch.status == BatchStatus.AWAITING_APPROVAL)
            .order_by(desc(PushBatch.created_at))
            .limit(10)
        ).scalars()
    )
    halted = list(
        session.execute(
            select(Run)
            .where(Run.status == RunStatus.HALTED_BY_GUARDRAIL)
            .order_by(desc(Run.started_at))
            .limit(5)
        ).scalars()
    )
    unread_alerts = list(
        session.execute(
            select(Notification)
            .where(Notification.acknowledged.is_(False), Notification.severity != "info")
            .order_by(desc(Notification.at))
            .limit(10)
        ).scalars()
    )

    # -- live state ---------------------------------------------------------
    last_run = session.execute(
        select(Run).order_by(desc(Run.started_at)).limit(1)
    ).scalar_one_or_none()

    recent_runs = list(
        session.execute(select(Run).order_by(desc(Run.started_at)).limit(12)).scalars()
    )

    last_catalog = session.execute(
        select(CatalogSync)
        .where(CatalogSync.status == "completed")
        .order_by(desc(CatalogSync.finished_at))
        .limit(1)
    ).scalar_one_or_none()

    recent_files = list(
        session.execute(
            select(FeedFile).order_by(desc(FeedFile.discovered_at)).limit(10)
        ).scalars()
    )

    # -- the numbers --------------------------------------------------------
    cfg = settings_store.get_all(session)
    prefixes = [p.upper() for p in cfg["sku_prefixes_in_scope"]]

    vendor_total = session.execute(select(func.count(VendorProduct.barcode))).scalar() or 0
    vendor_in_stock = (
        session.execute(
            select(func.count(VendorProduct.barcode)).where(VendorProduct.stock > 0)
        ).scalar()
        or 0
    )
    listings_total = (
        session.execute(
            select(func.count(AmazonListing.seller_sku)).where(
                AmazonListing.present_in_last_sync.is_(True)
            )
        ).scalar()
        or 0
    )
    listings_in_scope = (
        session.execute(
            select(func.count(AmazonListing.seller_sku)).where(
                AmazonListing.present_in_last_sync.is_(True),
                func.upper(AmazonListing.sku_prefix).in_(prefixes),
            )
        ).scalar()
        or 0
    )
    unmatched_open = (
        session.execute(
            select(func.count(UnmappedBarcode.barcode)).where(
                UnmappedBarcode.resolved.is_(False)
            )
        ).scalar()
        or 0
    )

    since = utcnow() - timedelta(hours=24)
    day = session.execute(
        select(
            func.count(Run.id),
            func.coalesce(func.sum(Run.pushed_changes), 0),
            func.coalesce(func.sum(Run.proposed_changes), 0),
            func.coalesce(func.sum(Run.rows_read), 0),
            func.coalesce(func.sum(Run.files_processed), 0),
        ).where(Run.started_at >= since)
    ).one()

    # -- drift: how far apart are Amazon and the vendor right now? ---------
    # The single most useful number on the page, because it is the thing this
    # system exists to reduce. Computed from the two tables directly rather
    # than from run history, so it is current even if no run has happened yet.
    drift = _measure_drift(session, prefixes, int(cfg["max_quantity"]))

    return render(
        request,
        session,
        "dashboard.html",
        flash=flash,
        flash_tone=tone,
        awaiting=awaiting,
        halted=halted,
        alerts=unread_alerts,
        last_run=last_run,
        recent_runs=recent_runs,
        last_catalog=last_catalog,
        recent_files=recent_files,
        stats={
            "vendor_total": vendor_total,
            "vendor_in_stock": vendor_in_stock,
            "listings_total": listings_total,
            "listings_in_scope": listings_in_scope,
            "unmatched_open": unmatched_open,
            "runs_24h": day[0],
            "pushed_24h": day[1],
            "proposed_24h": day[2],
            "rows_24h": day[3],
            "files_24h": day[4],
        },
        drift=drift,
    )


def _measure_drift(session: Session, prefixes: list[str], cap: int) -> dict:
    """
    How far Amazon currently is from what the vendor says.

    Done as one SQL statement rather than in Python: it joins about 45,500
    in-scope listings against 1.15 million vendor rows, and pulling either side
    into memory to compare would be slow and pointless.

    ``need_zero`` is the number that matters most -- products Amazon is still
    selling that the vendor has none of. Those are the cancelled orders and the
    account-health damage.
    """
    if not prefixes:
        return {"measured": False}

    joined = (
        select(
            func.count().label("total"),
            func.sum(
                func.cast(
                    (AmazonListing.quantity > 0) & (VendorProduct.stock == 0),
                    Integer,
                )
            ).label("need_zero"),
            func.sum(
                func.cast(
                    AmazonListing.quantity
                    != func.least(VendorProduct.stock, cap),
                    Integer,
                )
            ).label("differs"),
        )
        .select_from(AmazonListing)
        .join(VendorProduct, VendorProduct.barcode == AmazonListing.barcode)
        .where(
            AmazonListing.present_in_last_sync.is_(True),
            AmazonListing.status == "Active",
            func.upper(AmazonListing.sku_prefix).in_(prefixes),
            AmazonListing.quantity.isnot(None),
        )
    )

    try:
        row = session.execute(joined).one()
    except Exception as exc:  # noqa: BLE001
        # func.least is not available on every backend (SQLite in tests). The
        # dashboard must still render, so degrade rather than 500.
        log.debug("drift measurement unavailable on this backend: %s", exc)
        return {"measured": False}

    total = int(row.total or 0)
    return {
        "measured": total > 0,
        "matched": total,
        "need_zero": int(row.need_zero or 0),
        "differs": int(row.differs or 0),
        "in_step": total - int(row.differs or 0),
        "percent_in_step": round(100.0 * (total - int(row.differs or 0)) / total, 1) if total else 0.0,
    }


# ===========================================================================
# Runs
# ===========================================================================

@router.get("/runs", response_class=HTMLResponse)
async def runs(
    request: Request,
    page: int = Query(1, ge=1),
    status: str = "",
    flash: str = "",
    tone: str = "good",
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> HTMLResponse:
    """Run history, newest first."""
    query = select(Run)
    if status:
        # An unrecognised status in the query string is ignored rather than
        # erroring: it means somebody edited the URL or followed a stale link,
        # and showing all runs is a better answer than a 500.
        with suppress(ValueError):
            query = query.where(Run.status == RunStatus(status))

    total = session.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar() or 0

    items = list(
        session.execute(
            query.order_by(desc(Run.started_at))
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).scalars()
    )

    return render(
        request,
        session,
        "runs.html",
        flash=flash,
        flash_tone=tone,
        runs=items,
        page=page,
        pages=max(1, -(-total // PAGE_SIZE)),
        total=total,
        status_filter=status,
        statuses=[s.value for s in RunStatus],
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(
    request: Request,
    run_id: int,
    page: int = Query(1, ge=1),
    result: str = "",
    flash: str = "",
    tone: str = "good",
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> HTMLResponse:
    """
    One run in full: every file, every change, every result.

    This is the page that makes practice mode worth having. It shows exactly
    what *would* have been sent, per SKU, with the reason in words -- so the
    client can compare it against what they would have done by hand before
    trusting the system with the real thing.
    """
    run = session.get(Run, run_id)
    if run is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404)

    batches = list(
        session.execute(
            select(PushBatch).where(PushBatch.run_id == run_id).order_by(PushBatch.id)
        ).scalars()
    )

    item_query = select(PushItem).where(
        PushItem.batch_id.in_([b.id for b in batches] or [-1])
    )
    if result:
        # As above: an unknown filter value falls back to showing everything.
        with suppress(ValueError):
            item_query = item_query.where(PushItem.result == ItemResult(result))

    item_total = session.execute(
        select(func.count()).select_from(item_query.subquery())
    ).scalar() or 0

    items = list(
        session.execute(
            item_query.order_by(PushItem.new_quantity, PushItem.seller_sku)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).scalars()
    )

    files = list(
        session.execute(
            select(FeedFile)
            .where(FeedFile.processed_at.isnot(None))
            .order_by(desc(FeedFile.processed_at))
            .limit(10)
        ).scalars()
    )

    reports = list(
        session.execute(
            select(ReportFile).where(ReportFile.run_id == run_id).order_by(ReportFile.kind)
        ).scalars()
    )

    # Rejections grouped by Amazon's code. A recurring code is a catalogue
    # problem to fix once, not 400 individual problems to look at.
    codes = list(
        session.execute(
            select(PushItem.amazon_code, func.count(), func.min(PushItem.amazon_message))
            .where(
                PushItem.batch_id.in_([b.id for b in batches] or [-1]),
                PushItem.amazon_code.isnot(None),
            )
            .group_by(PushItem.amazon_code)
            .order_by(desc(func.count()))
        ).all()
    )

    return render(
        request,
        session,
        "run_detail.html",
        flash=flash,
        flash_tone=tone,
        run=run,
        batches=batches,
        items=items,
        item_total=item_total,
        page=page,
        pages=max(1, -(-item_total // PAGE_SIZE)),
        result_filter=result,
        results=[r.value for r in ItemResult],
        files=files,
        reports=reports,
        report_titles=REPORT_TITLES,
        error_codes=codes,
    )


# ===========================================================================
# Products
# ===========================================================================

@router.get("/products", response_class=HTMLResponse)
async def products(
    request: Request,
    q: str = "",
    page: int = Query(1, ge=1),
    only: str = "",
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> HTMLResponse:
    """
    Search the vendor's catalogue side by side with Amazon.

    The search box takes a barcode in any form -- with or without leading
    zeros -- because the vendor writes them one way and Amazon the other, and
    expecting the operator to know which is which would be a poor joke given
    that this mismatch is the whole problem.
    """
    cfg = settings_store.get_all(session)
    prefix = str(cfg["sku_prefix_for_new"])

    query = select(VendorProduct)

    if q:
        from app.core.barcode import match_candidates

        term = q.strip()
        candidates = match_candidates(term)
        if candidates:
            query = query.where(VendorProduct.barcode.in_(candidates))
        else:
            like = f"%{term}%"
            query = query.where(
                (VendorProduct.title.ilike(like)) | (VendorProduct.artist.ilike(like))
            )

    if only == "in_stock":
        query = query.where(VendorProduct.stock > 0)
    elif only == "out_of_stock":
        query = query.where(VendorProduct.stock == 0)
    elif only == "dropped":
        query = query.where(VendorProduct.missing_from_full_feeds > 0)

    total = session.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar() or 0

    rows = list(
        session.execute(
            query.order_by(desc(VendorProduct.last_changed_at))
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).scalars()
    )

    listings = {
        lst.seller_sku: lst
        for lst in session.execute(
            select(AmazonListing).where(
                AmazonListing.seller_sku.in_([f"{prefix}{r.barcode}" for r in rows] or ["-"])
            )
        ).scalars()
    }

    return render(
        request,
        session,
        "products.html",
        products=rows,
        listings=listings,
        prefix=prefix,
        q=q,
        only=only,
        page=page,
        pages=max(1, -(-total // PAGE_SIZE)),
        total=total,
    )


@router.get("/unmatched", response_class=HTMLResponse)
async def unmatched(
    request: Request,
    page: int = Query(1, ge=1),
    show_resolved: bool = False,
    flash: str = "",
    tone: str = "good",
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> HTMLResponse:
    """
    Products the vendor has in stock that we could not match to a listing.

    The actionable queue, and also the "new products" opportunity list the
    client's team already uses. Nothing here has been sent to Amazon -- that is
    the point of the queue.
    """
    query = select(UnmappedBarcode)
    if not show_resolved:
        query = query.where(UnmappedBarcode.resolved.is_(False))

    total = session.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar() or 0

    rows = list(
        session.execute(
            query.order_by(desc(UnmappedBarcode.vendor_stock), desc(UnmappedBarcode.last_seen_at))
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).scalars()
    )

    by_reason = list(
        session.execute(
            select(UnmappedBarcode.reason, func.count())
            .where(UnmappedBarcode.resolved.is_(False))
            .group_by(UnmappedBarcode.reason)
            .order_by(desc(func.count()))
        ).all()
    )

    return render(
        request,
        session,
        "unmatched.html",
        flash=flash,
        flash_tone=tone,
        rows=rows,
        by_reason=by_reason,
        page=page,
        pages=max(1, -(-total // PAGE_SIZE)),
        total=total,
        show_resolved=show_resolved,
    )


# ===========================================================================
# Reports
# ===========================================================================

@router.get("/reports", response_class=HTMLResponse)
async def reports(
    request: Request,
    page: int = Query(1, ge=1),
    kind: str = "",
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> HTMLResponse:
    """
    The five files, with a history rather than only the newest copy.

    The client's team uses In Stock and New Products daily, and wanted the
    other three produced too -- separately for each delta run and each
    full-feed run, which the filename makes unambiguous.
    """
    query = select(ReportFile)
    if kind:
        query = query.where(ReportFile.kind == kind)

    total = session.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar() or 0

    rows = list(
        session.execute(
            query.order_by(desc(ReportFile.created_at))
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).scalars()
    )

    newest = {}
    for r in session.execute(
        select(ReportFile).order_by(desc(ReportFile.created_at)).limit(60)
    ).scalars():
        newest.setdefault(r.kind, r)

    return render(
        request,
        session,
        "reports.html",
        rows=rows,
        newest=newest,
        titles=REPORT_TITLES,
        descriptions=REPORT_DESCRIPTIONS,
        kind=kind,
        page=page,
        pages=max(1, -(-total // PAGE_SIZE)),
        total=total,
    )


@router.get("/reports/{report_id}/download")
async def download_report(
    report_id: int,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> FileResponse:
    """
    Serve a report file.

    The path is read from the database rather than taken from the URL, so a
    crafted path cannot reach anything else on disk. It is then checked to be
    inside the reports directory as a second line of defence, in case a bad row
    ever gets written.
    """
    from pathlib import Path

    from fastapi import HTTPException

    record = session.get(ReportFile, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No such report.")

    path = Path(record.path).resolve()
    reports_root = app_settings.reports_dir.resolve()

    if not path.is_relative_to(reports_root):
        log.error("report %d points outside the reports directory: %s", report_id, path)
        raise HTTPException(status_code=400, detail="That report is not available.")

    if not path.exists():
        raise HTTPException(
            status_code=410,
            detail=(
                "This report file has been deleted to save disk space. Its details "
                "are still in the run history. Change 'Keep report files for' in "
                "Settings to hold them for longer."
            ),
        )

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=record.filename,
    )


# ===========================================================================
# Audit
# ===========================================================================

@router.get("/audit", response_class=HTMLResponse)
async def audit(
    request: Request,
    page: int = Query(1, ge=1),
    action: str = "",
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> HTMLResponse:
    """
    Who changed what, and when.

    Append-only. This is the page that answers "who set the cap to zero on
    Tuesday" -- and with a single shared login, knowing that a change happened
    at all is most of the value.
    """
    query = select(AuditEvent)
    if action:
        query = query.where(AuditEvent.action == action)

    total = session.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar() or 0

    rows = list(
        session.execute(
            query.order_by(desc(AuditEvent.at))
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).scalars()
    )

    actions = [
        a for (a,) in session.execute(
            select(AuditEvent.action).distinct().order_by(AuditEvent.action)
        ).all()
    ]

    return render(
        request,
        session,
        "audit.html",
        rows=rows,
        actions=actions,
        action=action,
        page=page,
        pages=max(1, -(-total // PAGE_SIZE)),
        total=total,
    )
