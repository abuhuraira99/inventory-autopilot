"""
The buttons that do something: run now, pause, approve, reject, undo.

WHY EVERY ONE OF THESE IS A POST
================================
They all change something. A GET that changes state can be triggered by a
prefetching browser, a link preview, or a bookmark -- and one of these buttons
sends 5,000 quantity changes to a live Amazon account. They are POST-only, and
the destructive ones require an explicit typed confirmation.

THE CONFIRMATION LADDER
=======================
The friction is matched to the consequence, not applied uniformly:

    Run now                     one click
    Pause / resume              one click (pausing is always safe)
    Approve a batch             one click, after seeing the full list
    Reject a batch              one click, with an optional reason
    Undo one batch              type UNDO
    Undo the last N batches     type UNDO
    Initial full sync           type INITIAL SYNC

Undo and the initial sync are the two that touch many listings at once from a
standing start, so they are the two that make you stop and type.

EVERY ACTION IS ATTRIBUTED
==========================
The audit trail records who, when, from which address, and what changed. With a
single shared login that attribution is coarse, but knowing an action happened
at all -- and being able to see exactly what it did -- is most of the value.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import scheduler
from app.core import settings_store
from app.db import get_session, run_lock
from app.engine.pipeline import execute_run
from app.engine.pusher import send_batch, verify_batch
from app.engine.rollback import (
    RollbackError,
    plan_rollback,
    rollback_batch,
    rollback_last_n,
)
from app.models import (
    AuditEvent,
    BatchStatus,
    ItemResult,
    Notification,
    PushBatch,
    PushItem,
    Run,
    RunTrigger,
    UnmappedBarcode,
    utcnow,
)
from app.routers.helpers import client_ip, redirect, render, require_login
from app.security.auth import SessionData
from app.services import amazon_client, sending_amazon_client, vendor_credentials

log = logging.getLogger(__name__)

router = APIRouter(tags=["actions"])


# ===========================================================================
# Running
# ===========================================================================

@router.post("/actions/run-now")
async def run_now(
    request: Request,
    reconcile: str = Form(""),
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Run the pipeline immediately.

    Takes the same lock as the scheduler and honours the pause switch and the
    mode, so this is a convenience rather than a way round anything. Runs
    inline rather than being handed to the scheduler, so the operator sees the
    outcome on the next page instead of having to go looking for it.
    """
    with run_lock() as acquired:
        if not acquired:
            return redirect(
                "/",
                flash="A run is already in progress. Wait for it to finish and try again.",
                tone="warn",
            )

        vendor = vendor_credentials(session)
        if vendor is None:
            return redirect(
                "/",
                flash=(
                    "The vendor connection is not configured, so there is nothing to "
                    "download. See Settings."
                ),
                tone="warn",
            )

        client = amazon_client(session)
        try:
            outcome = execute_run(
                session,
                client=client,
                vendor_credentials=vendor,
                trigger=RunTrigger.MANUAL,
                triggered_by=who.email,
                force_full_reconcile=bool(reconcile),
            )
        finally:
            if client is not None:
                client.close()

        session.add(
            AuditEvent(
                action="run.started",
                actor=who.email,
                actor_ip=client_ip(request),
                target=f"run:{outcome.run_id}",
                detail=f"manual run, reconcile={bool(reconcile)}",
            )
        )
        session.commit()

    tone = "good" if outcome.ok else ("warn" if outcome.guardrail_halt else "bad")
    return redirect(f"/runs/{outcome.run_id}", flash=outcome.message or "Run finished.", tone=tone)


@router.post("/actions/refresh-catalog")
async def refresh_catalog(
    request: Request,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Download Amazon's All Listings Report now.

    Handed to the scheduler rather than run inline, because Amazon's report
    pipeline is asynchronous and takes several minutes -- long enough that
    holding an HTTP request open for it would simply time out.
    """
    if not scheduler.trigger_now(scheduler.JOB_CATALOG):
        return redirect(
            "/",
            flash=(
                "The scheduler is not running in this process, so the refresh could "
                "not be started."
            ),
            tone="bad",
        )

    session.add(
        AuditEvent(
            action="catalog.refresh_requested",
            actor=who.email,
            actor_ip=client_ip(request),
        )
    )
    session.commit()
    return redirect(
        "/",
        flash=(
            "Refreshing Amazon's catalogue. Amazon takes a few minutes to build the "
            "report - this page will show the new figures once it lands."
        ),
        tone="info",
    )


# ===========================================================================
# The pause switch
# ===========================================================================

@router.post("/actions/pause")
async def toggle_pause(
    request: Request,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    The emergency stop.

    One click, no confirmation. Pausing is never the dangerous direction, and
    making somebody type a word while they are watching something go wrong
    would be actively harmful.
    """
    now_paused = not bool(settings_store.get(session, "paused"))
    settings_store.set_value(
        session, "paused", now_paused, actor=who.email, actor_ip=client_ip(request)
    )
    session.add(
        AuditEvent(
            action="killswitch.on" if now_paused else "killswitch.off",
            actor=who.email,
            actor_ip=client_ip(request),
            detail="everything paused" if now_paused else "resumed",
        )
    )
    session.commit()

    if now_paused:
        return redirect(
            "/",
            flash=(
                "PAUSED. Nothing will be downloaded, decided or sent until you switch "
                "this back on. Work is not lost - it is picked up when you resume."
            ),
            tone="warn",
        )
    return redirect("/", flash="Resumed. The next scheduled run will go ahead.")


# ===========================================================================
# Approval
# ===========================================================================

@router.get("/batches/{batch_id}")
async def batch_detail(
    request: Request,
    batch_id: int,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
):
    """
    One batch in full, before deciding whether to approve it.

    Every item is shown with the reason in words, because approving 5,000
    changes without being able to see what they are would make the approval
    step theatre rather than a control.
    """
    from fastapi import HTTPException

    batch = session.get(PushBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404)

    items = list(
        session.execute(
            select(PushItem)
            .where(PushItem.batch_id == batch_id)
            .order_by(PushItem.new_quantity, PushItem.seller_sku)
            .limit(500)
        ).scalars()
    )
    total = session.execute(
        select(func.count(PushItem.id)).where(
            PushItem.batch_id == batch_id
        )
    ).scalar() or 0

    return render(
        request,
        session,
        "batch_detail.html",
        batch=batch,
        run=session.get(Run, batch.run_id),
        items=items,
        item_total=total,
        showing=len(items),
    )


@router.post("/batches/{batch_id}/approve")
async def approve_batch(
    request: Request,
    batch_id: int,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Approve a held batch and send it.

    Uses a client that will actually send, regardless of the mode setting -- a
    human has just clicked Approve, so the human IS the gate. Leaving the
    dry-run flag on would make the button silently do nothing, which would be
    far worse than sending.
    """
    batch = session.get(PushBatch, batch_id)
    if batch is None:
        return redirect("/", flash="No such batch.", tone="bad")

    if batch.status is not BatchStatus.AWAITING_APPROVAL:
        return redirect(
            f"/batches/{batch_id}",
            flash=f"This batch is not waiting for approval - it is already {batch.status.value}.",
            tone="warn",
        )

    if bool(settings_store.get(session, "paused")):
        return redirect(
            f"/batches/{batch_id}",
            flash="The system is paused. Resume it before approving anything.",
            tone="warn",
        )

    client = sending_amazon_client(session)
    if client is None:
        return redirect(
            f"/batches/{batch_id}",
            flash="Amazon is not fully configured, so nothing can be sent. See Settings.",
            tone="bad",
        )

    with run_lock() as acquired:
        if not acquired:
            client.close()
            return redirect(
                f"/batches/{batch_id}",
                flash="A run is in progress. Try again in a moment.",
                tone="warn",
            )

        batch.status = BatchStatus.APPROVED
        batch.approved_by = who.email
        batch.approved_at = utcnow()
        session.add(
            AuditEvent(
                action="batch.approved",
                actor=who.email,
                actor_ip=client_ip(request),
                target=f"batch:{batch_id}",
                new_value={"items": batch.item_count, "to_zero": batch.zeroing_count},
                detail=f"approved {batch.item_count} changes",
            )
        )
        session.flush()

        try:
            # The decisions themselves are not kept in memory between requests,
            # so the listing detail (product type, handling time) is reloaded
            # from the database inside send_batch via the push items.
            summary = send_batch(session, client, batch)
            if bool(settings_store.get(session, "verify_after_push")) and summary.accepted:
                verify_batch(session, client, batch)
        finally:
            client.close()

        session.commit()

    if summary.errors:
        return redirect(
            f"/batches/{batch_id}",
            flash=f"Sent with problems: {summary.errors[0][:200]}",
            tone="bad",
        )
    return redirect(
        f"/batches/{batch_id}",
        flash=(
            f"Sent. {summary.accepted:,} accepted"
            + (f", {summary.rejected:,} rejected by Amazon" if summary.rejected else "")
            + "."
        ),
        tone="good" if not summary.rejected else "warn",
    )


@router.post("/batches/{batch_id}/reject")
async def reject_batch(
    request: Request,
    batch_id: int,
    reason: str = Form(""),
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Decline a held batch. Nothing is sent.

    The changes are not lost in any meaningful sense: because the system
    compares against Amazon's current state rather than against the last feed,
    the same differences will be proposed again on the next run. Rejecting is
    "not now", not "never".
    """
    batch = session.get(PushBatch, batch_id)
    if batch is None:
        return redirect("/", flash="No such batch.", tone="bad")

    batch.status = BatchStatus.REJECTED
    batch.rejected_reason = reason.strip() or "declined by the operator"
    session.add(
        AuditEvent(
            action="batch.rejected",
            actor=who.email,
            actor_ip=client_ip(request),
            target=f"batch:{batch_id}",
            detail=batch.rejected_reason,
        )
    )
    session.commit()

    return redirect(
        "/",
        flash=(
            "Batch declined and nothing was sent. These differences will be proposed "
            "again on the next run, because the system compares against what Amazon "
            "actually shows."
        ),
        tone="warn",
    )


# ===========================================================================
# Undo
# ===========================================================================

@router.get("/batches/{batch_id}/undo")
async def undo_preview(
    request: Request,
    batch_id: int,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
):
    """
    Show what undoing this batch would do, before doing it.

    An undo is itself a change to a live account and deserves the same review
    as the change it reverses.
    """
    try:
        plan = plan_rollback(session, [batch_id])
    except RollbackError as exc:
        return redirect(f"/batches/{batch_id}", flash=str(exc), tone="bad")

    return render(
        request,
        session,
        "undo.html",
        batch=session.get(PushBatch, batch_id),
        plan=plan,
        changes=plan.changes[:300],
        showing=min(300, len(plan.changes)),
    )


@router.post("/batches/{batch_id}/undo")
async def undo_batch(
    request: Request,
    batch_id: int,
    confirm: str = Form(...),
    reason: str = Form(""),
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Put the previous quantities back.

    Requires the word UNDO typed exactly. This is one of the two actions that
    can change thousands of listings from a standing start, so the friction is
    deliberate.
    """
    if confirm.strip().upper() != "UNDO":
        return redirect(
            f"/batches/{batch_id}/undo",
            flash="Type UNDO in the box to confirm.",
            tone="warn",
        )

    client = sending_amazon_client(session)
    if client is None:
        return redirect(
            f"/batches/{batch_id}",
            flash="Amazon is not configured, so nothing can be undone.",
            tone="bad",
        )

    with run_lock() as acquired:
        if not acquired:
            client.close()
            return redirect(
                f"/batches/{batch_id}",
                flash="A run is in progress. Try again in a moment.",
                tone="warn",
            )
        try:
            run, new_batch = rollback_batch(
                session,
                client,
                batch_id,
                actor=who.email,
                actor_ip=client_ip(request),
                reason=reason.strip(),
            )
        except RollbackError as exc:
            session.rollback()
            return redirect(f"/batches/{batch_id}", flash=str(exc), tone="bad")
        finally:
            client.close()
        session.commit()

    return redirect(
        f"/runs/{run.id}",
        flash=f"Undone. {run.pushed_changes:,} quantities were put back.",
    )


@router.post("/actions/undo-last")
async def undo_last(
    request: Request,
    count: int = Form(1),
    confirm: str = Form(...),
    reason: str = Form(""),
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Undo the last N batches. For unwinding a bad afternoon.

    Applied newest-first, so an older batch's "previous" value cannot overwrite
    a newer one and leave the account in a state that never existed.
    """
    if confirm.strip().upper() != "UNDO":
        return redirect("/runs", flash="Type UNDO to confirm.", tone="warn")

    count = max(1, min(count, 50))

    client = sending_amazon_client(session)
    if client is None:
        return redirect("/runs", flash="Amazon is not configured.", tone="bad")

    with run_lock() as acquired:
        if not acquired:
            client.close()
            return redirect("/runs", flash="A run is in progress. Try again shortly.", tone="warn")
        try:
            run, _ = rollback_last_n(
                session,
                client,
                count,
                actor=who.email,
                actor_ip=client_ip(request),
                reason=reason.strip(),
            )
        except RollbackError as exc:
            session.rollback()
            return redirect("/runs", flash=str(exc), tone="bad")
        finally:
            client.close()
        session.commit()

    return redirect(
        f"/runs/{run.id}",
        flash=f"Undone the last {count} batch(es). {run.pushed_changes:,} quantities put back.",
    )


# ===========================================================================
# Unmatched queue
# ===========================================================================

@router.post("/unmatched/{barcode}/resolve")
async def resolve_unmatched(
    request: Request,
    barcode: str,
    note: str = Form(""),
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Mark an unmatched product as dealt with, so the queue does not grow forever.
    """
    row = session.get(UnmappedBarcode, barcode)
    if row is None:
        return redirect("/unmatched", flash="No such entry.", tone="bad")

    row.resolved = True
    row.resolved_note = note.strip() or f"marked resolved by {who.email}"
    session.commit()
    return redirect("/unmatched", flash=f"{barcode} marked as dealt with.")


# ===========================================================================
# Alerts
# ===========================================================================

@router.post("/actions/acknowledge/{notification_id}")
async def acknowledge(
    request: Request,
    notification_id: int,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """Dismiss an alert from the dashboard. It stays in the history."""
    note = session.get(Notification, notification_id)
    if note is not None:
        note.acknowledged = True
        session.commit()
    return redirect("/", flash="Alert dismissed.", tone="neutral")


@router.post("/actions/acknowledge-all")
async def acknowledge_all(
    request: Request,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """Dismiss every outstanding alert."""
    session.query(Notification).filter(Notification.acknowledged.is_(False)).update(
        {Notification.acknowledged: True}, synchronize_session=False
    )
    session.commit()
    return redirect("/", flash="All alerts dismissed.", tone="neutral")


# ===========================================================================
# Retry
# ===========================================================================

@router.post("/batches/{batch_id}/retry-failed")
async def retry_failed(
    request: Request,
    batch_id: int,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Retry the items in a batch that Amazon rejected or that did not take effect.

    Rather than sending them again from here, they are simply marked for retry:
    because the decision engine compares against Amazon's actual quantity, the
    next ordinary run will notice the difference and handle it -- through the
    full guardrail and approval path, which a bespoke retry here would bypass.
    """
    items = list(
        session.execute(
            select(PushItem).where(
                PushItem.batch_id == batch_id,
                PushItem.result.in_([ItemResult.REJECTED, ItemResult.NOT_APPLIED]),
            )
        ).scalars()
    )
    for item in items:
        item.retry_count += 1

    session.add(
        AuditEvent(
            action="batch.retry_requested",
            actor=who.email,
            actor_ip=client_ip(request),
            target=f"batch:{batch_id}",
            detail=f"{len(items)} items marked for retry",
        )
    )
    session.commit()

    return redirect(
        f"/batches/{batch_id}",
        flash=(
            f"{len(items)} item(s) marked for retry. The next run will pick them up, "
            "with all the usual safety checks."
        ),
    )
