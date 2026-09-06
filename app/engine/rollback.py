"""
Undo.

WHY THIS EXISTS AND WHY IT MUST BE TESTED FOR REAL
==================================================
This system writes to a live account that generates the client's income.
Amazon has a sandbox, but it returns canned data and knows nothing about this
seller's real listings -- it can prove the plumbing works, not that the
behaviour is right. There is no way to fully rehearse without touching the real
account.

So the plan is not to avoid touching it. The plan is to make every touch
reversible, and then to **press the undo button on purpose, before it is
needed**. Nobody should trust a rollback that has never been run.
docs/OPERATIONS.md carries that as an explicit step in the pilot.

HOW IT WORKS
============
Every :class:`app.models.PushItem` recorded ``previous_quantity`` -- Amazon's
own reported value, captured immediately before the send. Rolling back means
sending those numbers back.

Three levels, all built on that one column:

  1. :func:`rollback_batch`        -- undo one batch
  2. :func:`rollback_batches`      -- undo several, newest first
  3. :func:`restore_from_snapshot` -- put the account back to any past
                                      All Listings Report

A ROLLBACK IS A NEW BATCH, NEVER AN EDIT
========================================
The undo creates a fresh :class:`PushBatch` with ``rollback_of_batch_id`` set,
and stamps ``rolled_back_by_batch_id`` on the original. History is append-only,
so the record always shows what happened rather than what somebody wishes had
happened -- and a rollback is itself undoable, which matters when the rollback
turns out to have been the mistake.

NEWEST FIRST
============
When undoing several batches the order is reverse-chronological. Applied
oldest-first, an older batch's "previous" value would overwrite a newer one and
leave the account in a state that never existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon.client import SpApiClient
from app.engine.decision import Decision, Direction
from app.engine.mapping import ListingEntry
from app.engine.pusher import create_batch, send_batch, verify_batch
from app.models import (
    AmazonListing,
    AuditEvent,
    BatchStatus,
    ItemResult,
    PushBatch,
    PushItem,
    Run,
    RunStatus,
    RunTrigger,
    SyncMode,
    utcnow,
)

log = logging.getLogger(__name__)


class RollbackError(RuntimeError):
    """A rollback was refused. The message is shown to the operator."""


@dataclass(slots=True)
class RollbackPlan:
    """
    What a rollback would do, before it does it.

    Always shown to the operator first. An undo is itself a change to a live
    account, and it deserves the same review as the change it reverses.
    """

    batch_ids: list[int] = field(default_factory=list)
    changes: list[Decision] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (sku, why)

    @property
    def count(self) -> int:
        return len(self.changes)

    @property
    def to_zero(self) -> int:
        return sum(1 for d in self.changes if d.desired_quantity == 0)

    def summary(self) -> str:
        """One sentence for the confirmation dialog."""
        if not self.changes:
            return "Nothing to undo - none of these changes can be reversed."
        parts = [f"{self.count} product{'s' if self.count != 1 else ''} would be put back"]
        if self.to_zero:
            parts.append(f"{self.to_zero} of them to 0")
        if self.skipped:
            parts.append(f"{len(self.skipped)} cannot be reversed")
        return ", ".join(parts) + "."


# ===========================================================================
# Planning
# ===========================================================================

def plan_rollback(session: Session, batch_ids: list[int]) -> RollbackPlan:
    """
    Work out what undoing these batches would send. Sends nothing.

    Items are skipped, with a reason the operator can read, when:

      * ``previous_quantity`` is unknown -- there is nothing to put back. This
        should not occur, because the decision engine refuses to push a listing
        whose Amazon quantity it does not know, precisely so that every push is
        reversible.
      * the item was rejected -- Amazon never applied it, so there is nothing
        to reverse.
      * the previous and current values already agree -- already reversed, or
        never actually changed.
      * the listing has since disappeared from the account.
    """
    plan = RollbackPlan(batch_ids=list(batch_ids))
    if not batch_ids:
        return plan

    # Newest first: see the module docstring.
    batches = list(
        session.execute(
            select(PushBatch).where(PushBatch.id.in_(batch_ids)).order_by(PushBatch.id.desc())
        ).scalars()
    )
    if not batches:
        raise RollbackError(f"No such batch: {batch_ids}")

    # One entry per SKU. With several batches the FIRST occurrence wins, and
    # because batches are ordered newest-first that is the most recent
    # "previous" value -- which is the state the account was in before this
    # whole group of changes.
    seen: set[str] = set()

    for batch in batches:
        if batch.status is BatchStatus.ROLLED_BACK:
            plan.skipped.append((f"batch {batch.id}", "already rolled back"))
            continue

        items = list(
            session.execute(
                select(PushItem).where(PushItem.batch_id == batch.id)
            ).scalars()
        )

        for item in items:
            if item.seller_sku in seen:
                continue
            seen.add(item.seller_sku)

            if item.result in (ItemResult.REJECTED, ItemResult.SKIPPED, ItemResult.ERROR):
                plan.skipped.append(
                    (item.seller_sku, "Amazon never applied this change, so there is nothing to undo")
                )
                continue

            if item.previous_quantity is None:
                plan.skipped.append(
                    (item.seller_sku, "the quantity before the change was not recorded")
                )
                continue

            listing = session.get(AmazonListing, item.seller_sku)
            if listing is None or not listing.present_in_last_sync:
                plan.skipped.append(
                    (item.seller_sku, "this listing is no longer on the Amazon account")
                )
                continue

            current = listing.quantity
            if current == item.previous_quantity:
                plan.skipped.append(
                    (item.seller_sku, f"already showing {item.previous_quantity}")
                )
                continue

            target = int(item.previous_quantity)
            plan.changes.append(
                Decision(
                    seller_sku=item.seller_sku,
                    barcode=item.barcode or "",
                    current_quantity=current,
                    desired_quantity=target,
                    vendor_stock=item.vendor_stock or 0,
                    direction=(Direction.TO_ZERO if target == 0 else Direction.UP if (current or 0) < target else Direction.DOWN),
                    reason=(
                        f"undoing batch {batch.id}: putting the quantity back to "
                        f"{target}, which is what Amazon showed before that change"
                    ),
                    listing=ListingEntry(
                        seller_sku=listing.seller_sku,
                        sku_prefix=listing.sku_prefix,
                        barcode=listing.barcode or "",
                        quantity=listing.quantity,
                        status=listing.status,
                        fulfillment_channel=listing.fulfillment_channel,
                        lead_time_to_ship_days=listing.lead_time_to_ship_days,
                        product_type=listing.product_type,
                        blacklisted=listing.blacklisted,
                    ),
                )
            )

    log.info(
        "rollback plan for batches %s: %d changes, %d skipped",
        batch_ids, len(plan.changes), len(plan.skipped),
    )
    return plan


# ===========================================================================
# Executing
# ===========================================================================

def execute_rollback(
    session: Session,
    client: SpApiClient,
    plan: RollbackPlan,
    *,
    actor: str,
    actor_ip: str | None = None,
    reason: str = "",
    verify: bool = True,
) -> tuple[Run, PushBatch | None]:
    """
    Carry out a planned rollback.

    Creates its own :class:`Run` with trigger ``ROLLBACK`` so it appears in the
    history as a distinct, attributable event rather than being folded into a
    scheduled run.

    Deliberately **not** subject to the change limit or the percentage
    guardrail: a human has explicitly asked to restore a known-good state, and
    a brake designed to catch surprising machine behaviour would only get in
    the way. The zeroing guardrail is also skipped for the same reason -- the
    quantities being restored are ones Amazon itself reported earlier.
    """
    if not plan.changes:
        raise RollbackError(
            "There is nothing to undo. " + plan.summary()
        )

    run = Run(
        trigger=RunTrigger.ROLLBACK,
        triggered_by=actor,
        # A rollback always sends. Recording it as AUTOMATIC would be
        # misleading; the mode column reflects that this run had no gate
        # because a human was the gate.
        mode=SyncMode.AUTOMATIC,
        status=RunStatus.RUNNING,
        proposed_changes=len(plan.changes),
        settings_snapshot={
            "rollback_of_batches": plan.batch_ids,
            "requested_by": actor,
            "reason": reason,
            "guardrails": "skipped: a human requested a restore to a known previous state",
        },
    )
    session.add(run)
    session.flush()

    session.add(
        AuditEvent(
            action="batch.rolled_back",
            actor=actor,
            actor_ip=actor_ip,
            target=f"batches:{','.join(str(b) for b in plan.batch_ids)}",
            new_value={"changes": len(plan.changes), "skipped": len(plan.skipped)},
            detail=reason or plan.summary(),
        )
    )

    # A very large rollback still goes through one feed document rather than
    # thousands of requests.
    batch = create_batch(session, run.id, plan.changes, feeds_threshold=500)
    batch.rollback_of_batch_id = plan.batch_ids[0] if plan.batch_ids else None
    batch.notes = f"Undo of batch(es) {plan.batch_ids}. Requested by {actor}. {reason}".strip()
    session.flush()

    decisions_by_sku = {d.seller_sku: d for d in plan.changes}
    summary = send_batch(session, client, batch, decisions_by_sku=decisions_by_sku)

    # Mark the originals, so the interface can grey out a second undo.
    for original_id in plan.batch_ids:
        original = session.get(PushBatch, original_id)
        if original is not None and original.status is not BatchStatus.ROLLED_BACK:
            original.status = BatchStatus.ROLLED_BACK
            original.rolled_back_by_batch_id = batch.id

    if verify and summary.accepted:
        verify_batch(session, client, batch)

    run.status = RunStatus.COMPLETED if summary.all_accepted else RunStatus.FAILED
    run.pushed_changes = summary.accepted
    run.finished_at = utcnow()
    run.duration_seconds = (run.finished_at - run.started_at).total_seconds()
    if summary.errors:
        run.error = "\n".join(summary.errors[:10])
    session.flush()

    log.info(
        "rollback run %d complete: %d put back, %d rejected",
        run.id, summary.accepted, summary.rejected,
    )
    return run, batch


def rollback_batch(
    session: Session,
    client: SpApiClient,
    batch_id: int,
    *,
    actor: str,
    actor_ip: str | None = None,
    reason: str = "",
) -> tuple[Run, PushBatch | None]:
    """Undo one batch. The one-click case."""
    plan = plan_rollback(session, [batch_id])
    return execute_rollback(session, client, plan, actor=actor, actor_ip=actor_ip, reason=reason)


def rollback_batches(
    session: Session,
    client: SpApiClient,
    batch_ids: list[int],
    *,
    actor: str,
    actor_ip: str | None = None,
    reason: str = "",
) -> tuple[Run, PushBatch | None]:
    """Undo several batches at once. For unwinding a bad afternoon."""
    plan = plan_rollback(session, batch_ids)
    return execute_rollback(session, client, plan, actor=actor, actor_ip=actor_ip, reason=reason)


def rollback_last_n(
    session: Session,
    client: SpApiClient,
    n: int,
    *,
    actor: str,
    actor_ip: str | None = None,
    reason: str = "",
) -> tuple[Run, PushBatch | None]:
    """
    Undo the most recent ``n`` batches that actually sent something.

    Batches that were rejected, or already rolled back, are not counted -- "the
    last 3 batches" should mean three real changes, not three rows.
    """
    rows = list(
        session.execute(
            select(PushBatch.id)
            .where(
                PushBatch.status.in_(
                    [
                        BatchStatus.SENT,
                        BatchStatus.VERIFIED,
                        BatchStatus.PARTIALLY_FAILED,
                    ]
                ),
                PushBatch.rollback_of_batch_id.is_(None),
            )
            .order_by(PushBatch.id.desc())
            .limit(n)
        ).scalars()
    )
    if not rows:
        raise RollbackError("There are no sent batches to undo.")
    return rollback_batches(
        session, client, rows, actor=actor, actor_ip=actor_ip, reason=reason
    )


# ===========================================================================
# The emergency option
# ===========================================================================

def plan_restore_from_snapshot(
    session: Session,
    snapshot_quantities: dict[str, int],
    *,
    prefixes_in_scope: list[str],
) -> RollbackPlan:
    """
    Plan a restore of the account to a past All Listings Report.

    The nuclear option, and the reason every catalogue report is kept on disk.
    Used when something has gone badly wrong across many batches and undoing
    them one at a time would be slower than going back to a known-good day.

    Scope still applies: a snapshot restore must not touch another supplier's
    listings, however broad the request.
    """
    plan = RollbackPlan()
    upper_scope = {p.upper() for p in prefixes_in_scope}

    for sku, target in snapshot_quantities.items():
        listing = session.get(AmazonListing, sku)
        if listing is None:
            plan.skipped.append((sku, "not on the account any more"))
            continue
        if (listing.sku_prefix or "").upper() not in upper_scope:
            plan.skipped.append((sku, "outside the prefixes this system may change"))
            continue
        if listing.blacklisted:
            plan.skipped.append((sku, "on the never-touch list"))
            continue
        if listing.quantity == target:
            continue  # nothing to do, and not worth reporting

        plan.changes.append(
            Decision(
                seller_sku=sku,
                barcode=listing.barcode or "",
                current_quantity=listing.quantity,
                desired_quantity=int(target),
                vendor_stock=0,
                direction=(
                    Direction.TO_ZERO
                    if target == 0
                    else Direction.UP
                    if (listing.quantity or 0) < target
                    else Direction.DOWN
                ),
                reason=f"restoring to the catalogue snapshot value of {target}",
                listing=ListingEntry(
                    seller_sku=listing.seller_sku,
                    sku_prefix=listing.sku_prefix,
                    barcode=listing.barcode or "",
                    quantity=listing.quantity,
                    status=listing.status,
                    fulfillment_channel=listing.fulfillment_channel,
                    lead_time_to_ship_days=listing.lead_time_to_ship_days,
                    product_type=listing.product_type,
                ),
            )
        )

    log.info(
        "snapshot restore plan: %d changes, %d skipped", len(plan.changes), len(plan.skipped)
    )
    return plan


def load_snapshot_quantities(path: str) -> dict[str, int]:
    """
    Read ``{sku: quantity}`` out of a saved All Listings Report.

    Reuses the production report parser rather than a bespoke reader, so a
    restore reads the file exactly the way the daily refresh does -- including
    the byte-order-mark handling that this account's reports need.
    """
    from pathlib import Path

    from app.amazon.reports import parse_local_report

    records, _stats = parse_local_report(Path(path))
    return {r.seller_sku: r.quantity for r in records if r.quantity is not None}
