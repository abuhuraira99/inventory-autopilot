"""
Sending changes to Amazon, and confirming they landed.

THE THREE THINGS THIS DOES THAT THE MANUAL PROCESS NEVER DID
============================================================
1. **Records the previous quantity before sending.** Every
   :class:`app.models.PushItem` stores ``previous_quantity``, taken from
   Amazon's own reported value. That single column is what makes one-click
   rollback possible. Without it, "undo" would mean "guess".

2. **Reads Amazon back afterwards.** A flat-file upload that names a SKU which
   does not exist reports success and does nothing. A feed can finish DONE and
   still have rejected rows -- the client's last manual upload had 53 of them
   in 656. So after every push the affected SKUs are re-read and compared. What
   did not stick is recorded and retried, not assumed.

3. **Chooses the transport by size.** Small batches go one SKU at a time for
   immediate per-SKU feedback; large ones go as a single feed document. See
   :mod:`app.amazon.feeds` for why.

EVENTUAL CONSISTENCY
====================
Amazon's listing updates are not immediately readable. A verification run
straight after a push will sometimes read the old value, which is not a
failure. So verification allows a settling delay and one re-check before
reporting a mismatch, and anything still wrong is left for the next run rather
than retried in a tight loop.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from app.amazon.client import SpApiClient, SpApiError, SpApiPermissionError
from app.amazon.feeds import FeedItem, push_batch_via_feed
from app.amazon.listings import patch_quantity, read_listing
from app.engine.decision import Decision, Direction
from app.models import (
    AmazonListing,
    BatchStatus,
    ItemResult,
    PushBatch,
    PushItem,
    PushMethod,
    utcnow,
)

log = logging.getLogger(__name__)

#: Above this many changes, use one feed document instead of per-SKU patches.
DEFAULT_FEEDS_THRESHOLD = 500

#: Amazon needs a moment before a change is readable. Measured informally at a
#: few seconds; 20 is comfortable without making a run feel stuck.
#:
#: This is a courtesy pause, NOT a guarantee. See VERIFY_CONFIRM_DEADLINE below:
#: a change still missing when this pause ends has not failed, it is merely
#: unconfirmed, and waiting longer here is not the answer -- verification runs
#: inside the operator's approve request, so a multi-minute sleep would hang the
#: browser on every approval.
VERIFY_SETTLE_SECONDS = 20

#: How long Amazon gets to apply an accepted change before we are willing to
#: call it a failure.
#:
#: Amazon's Listings Items API is eventually consistent: ACCEPTED means "queued",
#: not "done". On 12 September 2026 a live batch of 25 was declared "did not take
#: effect" 30 seconds after sending and all 25 had in fact been applied -- the
#: operator confirmed 0 in Seller Central by hand. Treating an early read-back as
#: proof of failure produced 25 false alarms and queued 25 needless resends.
#:
#: Six hours is deliberately generous. The cost of waiting is a row that says
#: "not confirmed yet" for a while; the cost of being hasty is crying wolf about
#: a live account, which is how an operator learns to ignore a real alarm.
VERIFY_CONFIRM_DEADLINE = timedelta(hours=6)

#: Verifying 5,000 SKUs individually would cost 5,000 GET requests. For large
#: batches we verify a random sample and rely on the next catalogue refresh for
#: the rest -- which is honest and cheap, and the dashboard says so.
VERIFY_SAMPLE_THRESHOLD = 500
VERIFY_SAMPLE_SIZE = 100


@dataclass(slots=True)
class PushSummary:
    """What happened, for the run record and the alert email."""

    batch_id: int | None = None
    method: PushMethod = PushMethod.LISTINGS_API
    submitted: int = 0
    accepted: int = 0
    rejected: int = 0
    verified: int = 0
    not_applied: int = 0
    #: Sent and accepted, but Amazon had not applied it yet when we looked.
    #: Not a failure and not a success -- a question still open. Kept apart from
    #: ``not_applied`` so the dashboard never calls a working change broken.
    pending_confirmation: int = 0
    errors: list[str] = field(default_factory=list)
    #: Amazon error codes and how often each occurred, so a recurring
    #: catalogue problem is visible rather than buried in per-SKU rows.
    codes: dict[str, int] = field(default_factory=dict)
    feed_id: str | None = None
    verification_sampled: bool = False

    @property
    def all_accepted(self) -> bool:
        return self.rejected == 0 and not self.errors


# ===========================================================================
# Creating the batch
# ===========================================================================

def create_batch(
    session: Session,
    run_id: int,
    decisions: list[Decision],
    *,
    feeds_threshold: int = DEFAULT_FEEDS_THRESHOLD,
    is_initial_sync: bool = False,
) -> PushBatch:
    """
    Persist a batch and its items **before** anything is sent.

    Order matters and is deliberate: the rollback record is written first, so a
    crash mid-send leaves a complete record of what was attempted. Writing it
    afterwards would mean a crash produced changes on Amazon with no way to
    undo them.
    """
    method = PushMethod.FEEDS_API if len(decisions) > feeds_threshold else PushMethod.LISTINGS_API

    batch = PushBatch(
        run_id=run_id,
        status=BatchStatus.PENDING,
        method=method,
        item_count=len(decisions),
        zeroing_count=sum(1 for d in decisions if d.direction is Direction.TO_ZERO),
        raising_count=sum(1 for d in decisions if d.direction is Direction.UP),
        lowering_count=sum(1 for d in decisions if d.direction is Direction.DOWN),
        is_initial_sync=is_initial_sync,
    )
    session.add(batch)
    session.flush()  # assigns batch.id

    for d in decisions:
        session.add(
            PushItem(
                batch_id=batch.id,
                seller_sku=d.seller_sku,
                barcode=d.barcode,
                # From Amazon's own reported quantity. This is what rollback
                # restores, so it must be the real previous value and not a
                # value we computed.
                previous_quantity=d.current_quantity,
                new_quantity=d.desired_quantity,
                vendor_stock=d.vendor_stock,
                reason=d.reason[:200],
                result=ItemResult.PENDING,
            )
        )

    session.flush()
    log.info(
        "batch %d created: %d items via %s (%d to zero, %d up, %d down)",
        batch.id, batch.item_count, method.value,
        batch.zeroing_count, batch.raising_count, batch.lowering_count,
    )
    return batch


# ===========================================================================
# Sending
# ===========================================================================

def send_batch(
    session: Session,
    client: SpApiClient,
    batch: PushBatch,
    *,
    decisions_by_sku: dict[str, Decision] | None = None,
) -> PushSummary:
    """
    Send a batch and record the result of every item.

    Per-SKU failures are recorded and the batch continues -- one broken listing
    must not stop 4,999 good ones. Systemic failures (authentication,
    permissions) abort, because continuing would produce thousands of identical
    errors and a very confusing audit trail.
    """
    items: list[PushItem] = list(
        session.query(PushItem).filter(PushItem.batch_id == batch.id).all()
    )
    if not items:
        log.warning("batch %d has no items", batch.id)
        batch.status = BatchStatus.SENT
        return PushSummary(batch_id=batch.id, method=batch.method)

    summary = PushSummary(batch_id=batch.id, method=batch.method, submitted=len(items))
    batch.status = BatchStatus.SENDING
    batch.sent_at = utcnow()
    # Committed, not merely flushed. SENDING is the one status that must survive
    # this process being killed: it is the difference between "we know nothing
    # was sent" and "something may have reached Amazon, go and verify". A
    # flushed-but-uncommitted SENDING would be rolled back by the crash and the
    # batch would look untouched while Amazon had in fact been changed.
    session.commit()

    decisions_by_sku = decisions_by_sku or {}

    try:
        if batch.method is PushMethod.FEEDS_API:
            _send_via_feed(session, client, batch, items, summary, decisions_by_sku)
        else:
            _send_via_listings(session, client, batch, items, summary, decisions_by_sku)
    except SpApiPermissionError as exc:
        # The single most likely first-deployment failure: the app is missing
        # the Product Listing role. Everything not yet sent stays PENDING so a
        # retry after the roles are fixed picks up exactly where this stopped.
        batch.status = BatchStatus.FAILED
        summary.errors.append(str(exc))
        log.error("batch %d stopped: %s", batch.id, str(exc).splitlines()[0])
        session.flush()
        return summary
    except SpApiError as exc:
        batch.status = BatchStatus.FAILED
        summary.errors.append(str(exc))
        log.error("batch %d failed: %s", batch.id, exc)
        session.flush()
        return summary

    batch.accepted_count = summary.accepted
    batch.rejected_count = summary.rejected
    batch.status = (
        BatchStatus.SENT
        if summary.rejected == 0
        else (BatchStatus.PARTIALLY_FAILED if summary.accepted else BatchStatus.FAILED)
    )
    session.flush()

    log.info(
        "batch %d sent: %d accepted, %d rejected%s",
        batch.id, summary.accepted, summary.rejected,
        f" (codes {summary.codes})" if summary.codes else "",
    )
    return summary


def _send_via_listings(
    session: Session,
    client: SpApiClient,
    batch: PushBatch,
    items: list[PushItem],
    summary: PushSummary,
    decisions_by_sku: dict[str, Decision],
) -> None:
    """One PATCH per SKU. Immediate per-SKU feedback."""
    for item in items:
        decision = decisions_by_sku.get(item.seller_sku)
        listing = decision.listing if decision else None

        outcome = patch_quantity(
            client,
            item.seller_sku,
            item.new_quantity,
            product_type=(listing.product_type if listing else "PRODUCT"),
            # Sent back unchanged so the patch does not wipe the handling time.
            lead_time_to_ship_days=(listing.lead_time_to_ship_days if listing else None),
        )

        item.submission_id = outcome.submission_id
        if outcome.accepted:
            item.result = ItemResult.ACCEPTED
            summary.accepted += 1
            _record_pushed(session, item)
        else:
            item.result = ItemResult.REJECTED
            item.amazon_code = outcome.error_code
            item.amazon_message = (outcome.error_message or "")[:2000]
            summary.rejected += 1
            if outcome.error_code:
                summary.codes[outcome.error_code] = summary.codes.get(outcome.error_code, 0) + 1

    session.flush()


def _send_via_feed(
    session: Session,
    client: SpApiClient,
    batch: PushBatch,
    items: list[PushItem],
    summary: PushSummary,
    decisions_by_sku: dict[str, Decision],
) -> None:
    """One feed document for the whole batch."""
    feed_items = []
    for item in items:
        decision = decisions_by_sku.get(item.seller_sku)
        listing = decision.listing if decision else None
        feed_items.append(
            FeedItem(
                seller_sku=item.seller_sku,
                quantity=item.new_quantity,
                product_type=(listing.product_type if listing else "PRODUCT"),
                lead_time_to_ship_days=(listing.lead_time_to_ship_days if listing else None),
            )
        )

    result = push_batch_via_feed(client, feed_items)

    batch.feed_id = result.feed_id
    batch.feed_document_id = result.document_id
    batch.feed_result_summary = result.raw_summary
    summary.feed_id = result.feed_id

    if result.status != "DONE":
        # Amazon has the document; it just has not told us the outcome yet.
        # Leave the items PENDING_VERIFICATION rather than claiming either
        # success or failure. The read-back settles it.
        for item in items:
            item.result = ItemResult.ACCEPTED
            item.amazon_message = f"feed {result.feed_id} finished with status {result.status}; awaiting verification"
        summary.accepted = len(items)
        session.flush()
        return

    for item in items:
        outcome = result.outcomes.get(item.seller_sku)
        if outcome is None or outcome.accepted:
            item.result = ItemResult.ACCEPTED
            summary.accepted += 1
            _record_pushed(session, item)
        else:
            item.result = ItemResult.REJECTED
            item.amazon_code = outcome.code
            item.amazon_message = (outcome.message or "")[:2000]
            summary.rejected += 1
            if outcome.code:
                summary.codes[outcome.code] = summary.codes.get(outcome.code, 0) + 1

    session.flush()


def _record_pushed(session: Session, item: PushItem) -> None:
    """
    Remember what we told Amazon, on the listing row.

    Lets the dashboard distinguish two very different situations that look the
    same in the numbers:
      * "Amazon disagrees because our push failed"     -> our problem, retry
      * "Amazon disagrees because a human changed it"  -> their choice, ask
    """
    listing = session.get(AmazonListing, item.seller_sku)
    if listing is not None:
        listing.last_pushed_quantity = item.new_quantity
        listing.last_pushed_at = utcnow()


# ===========================================================================
# Verification
# ===========================================================================

def verify_batch(
    session: Session,
    client: SpApiClient,
    batch: PushBatch,
    *,
    settle_seconds: int = VERIFY_SETTLE_SECONDS,
    sample_threshold: int = VERIFY_SAMPLE_THRESHOLD,
    sample_size: int = VERIFY_SAMPLE_SIZE,
) -> PushSummary:
    """
    Read Amazon back and confirm the changes took effect.

    This is the step most inventory tools skip, and the reason the client has
    been losing updates invisibly. Amazon accepting a request is not the same
    as Amazon applying it.

    For batches above ``sample_threshold`` a random sample is verified rather
    than every SKU -- 5,000 individual GET requests would take 40 minutes for
    information the next catalogue refresh provides for free. The dashboard
    states plainly when a batch was sampled rather than fully verified;
    pretending otherwise would be worse than admitting it.
    """
    summary = PushSummary(batch_id=batch.id, method=batch.method)

    accepted_items: list[PushItem] = list(
        session.query(PushItem)
        .filter(PushItem.batch_id == batch.id, PushItem.result == ItemResult.ACCEPTED)
        .all()
    )
    if not accepted_items:
        return summary

    to_check = accepted_items
    if len(accepted_items) > sample_threshold:
        import random

        to_check = random.sample(accepted_items, min(sample_size, len(accepted_items)))  # noqa: S311
        summary.verification_sampled = True
        log.info(
            "batch %d has %d accepted items; verifying a sample of %d",
            batch.id, len(accepted_items), len(to_check),
        )

    if settle_seconds > 0 and not client.dry_run:
        # Amazon's listing updates are eventually consistent. Reading straight
        # away would report false mismatches.
        log.debug("waiting %ds for Amazon to settle before verifying", settle_seconds)
        time.sleep(settle_seconds)

    for item in to_check:
        if client.dry_run:
            item.result = ItemResult.VERIFIED
            item.verified_quantity = item.new_quantity
            item.verified_at = utcnow()
            summary.verified += 1
            continue

        try:
            snapshot = read_listing(client, item.seller_sku)
        except SpApiError as exc:
            # Could not check. Not evidence of failure; leave it accepted and
            # let the catalogue refresh settle it.
            log.warning("could not verify %s: %s", item.seller_sku, exc)
            summary.errors.append(f"{item.seller_sku}: {exc}")
            continue

        actual = snapshot.quantity if snapshot else None
        item.verified_quantity = actual
        item.verified_at = utcnow()

        if snapshot is None:
            item.result = ItemResult.NOT_APPLIED
            item.amazon_message = (
                "The SKU could not be read back, which means it is not on the account. "
                "This is the silent-failure case: an upload naming a SKU that does not "
                "exist reports success and changes nothing."
            )
            summary.not_applied += 1
        elif actual == item.new_quantity:
            item.result = ItemResult.VERIFIED
            summary.verified += 1
            listing = session.get(AmazonListing, item.seller_sku)
            if listing is not None:
                # Keep our picture of Amazon in step with reality.
                listing.quantity = actual
                listing.synced_at = utcnow()
        elif utcnow() - (batch.sent_at or batch.created_at) < VERIFY_CONFIRM_DEADLINE:
            # Sent recently. Amazon accepted it and simply has not applied it
            # yet -- that is normal, not a fault. Leave the item ACCEPTED, which
            # already means "Amazon took it, and we have not confirmed it", and
            # look again on a later run. Calling this a failure here is what
            # produced 25 false alarms on 12 September 2026.
            item.amazon_message = (
                f"Sent {item.new_quantity}; Amazon still showed {actual} when checked. "
                "Amazon applies these in its own time, so this is not yet a failure. "
                "It will be checked again on a later run."
            )
            summary.pending_confirmation += 1
        else:
            item.result = ItemResult.NOT_APPLIED
            item.amazon_message = (
                f"Sent {item.new_quantity} but Amazon still shows {actual}, "
                "long after it accepted the change. Recorded for retry."
            )
            summary.not_applied += 1

    batch.verified_count = summary.verified
    batch.verified_at = utcnow()
    if summary.not_applied == 0 and summary.pending_confirmation == 0 and not summary.errors:
        batch.status = BatchStatus.VERIFIED
    session.flush()

    log.info(
        "batch %d verified: %d confirmed, %d did not stick, %d not yet applied by Amazon%s",
        batch.id, summary.verified, summary.not_applied, summary.pending_confirmation,
        " (sampled)" if summary.verification_sampled else "",
    )
    return summary


def collect_retries(session: Session, *, limit: int = 5000, max_retries: int = 3) -> list[str]:
    """
    SKUs whose last push did not take effect and which are worth trying again.

    Fed back into the next run. ``max_retries`` stops a permanently broken
    listing -- an 8684 "linked to more than one GCID", for example -- from
    being retried forever; after that it stays visible on the dashboard for a
    human, which is the right place for a problem only a human can fix.
    """
    rows = (
        session.query(PushItem.seller_sku)
        .filter(
            PushItem.result.in_([ItemResult.NOT_APPLIED, ItemResult.REJECTED]),
            PushItem.retry_count < max_retries,
        )
        .distinct()
        .limit(limit)
        .all()
    )
    return [sku for (sku,) in rows]
