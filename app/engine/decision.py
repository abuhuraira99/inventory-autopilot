"""
Deciding what quantity Amazon should show.

THE RULES, AS THE CLIENT STATED THEM
====================================
Recorded 2026-09-05, in the client's own words, with what each one became:

  "it must show the current stock that is available on the vendor"
      -> safety_buffer = 0. The vendor's number is published as-is.

  "the highest quantity must not exceed 15" / "never more than 15"
      -> max_quantity = 15. A hard ceiling.

  "when it is 0 on the vendor"  (asked: when is it out of stock?)
      -> out_of_stock_at = 0. Only genuinely empty means off sale.

  "set it to zero straight away but dont delete it from the sytem nor from
   the amazon"
      -> missing_full_feeds_before_zero = 1, and no code path in this system
         deletes a listing. Quantity 0 keeps the listing, its reviews and its
         ranking, and it can come back the instant the vendor restocks.

  "every smallest or tiniest one, it must be perfectly synced with the vendor"
      -> min_change_to_push = 0.

Every one of those is a dashboard setting, so the client can change their mind
without a developer.

WHY WE COMPARE AGAINST AMAZON, NOT AGAINST THE LAST FEED
========================================================
This is the single most important design decision in the system, so it is worth
stating plainly.

The old tool compared today's feed with yesterday's feed. That is right for
producing a change report, and wrong as the basis for pushing, because of this
sequence:

    Monday    vendor says 0. Tool sees a change. Upload happens. It FAILS.
    Tuesday   vendor still says 0. Tool compares 0 with 0: "no change".
    Wednesday no change. Thursday no change. Forever.

One failure becomes permanent, and nothing anywhere records that it happened.

Comparing the *desired* quantity against *Amazon's own reported quantity*
fixes it structurally:

  * a failed push leaves the difference in place, so the next run retries it
  * running twice is harmless, so a retry is never dangerous
  * a human editing a quantity in Seller Central is detected as drift

The measured backlog on this account when the comparison was done properly on
2026-09-04: 38,112 listings where Amazon disagreed with the vendor, including
4,060 that Amazon was still selling with zero vendor stock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from app.engine.mapping import ListingEntry, MatchResult

log = logging.getLogger(__name__)


class Direction(str, Enum):
    """
    Which way a change goes. Used for prioritisation and for the guardrails.

    The two directions are not equally urgent. Going to ZERO stops orders the
    client cannot fulfil, which protects account health -- the thing that is
    hardest to repair. Going UP only recovers sales, which can wait a cycle.
    So when a run hits its change limit, zeros go first.
    """

    TO_ZERO = "to_zero"
    DOWN = "down"
    UP = "up"
    NONE = "none"


class SkipReason(str, Enum):
    """Why a product was not changed. Every one is shown on the dashboard."""

    ALREADY_CORRECT = "already_correct"
    UNMAPPED = "unmapped"
    INACTIVE_LISTING = "inactive_listing"
    FBA_LISTING = "fba_listing"
    BLACKLISTED = "blacklisted"
    BELOW_MIN_CHANGE = "below_min_change"
    INCREASES_DISABLED = "increases_disabled"
    NO_AMAZON_QUANTITY = "no_amazon_quantity"


@dataclass(slots=True)
class Decision:
    """One product's verdict."""

    seller_sku: str
    barcode: str
    #: What Amazon shows now. None when the catalogue has no quantity for it.
    current_quantity: int | None
    #: What it should show.
    desired_quantity: int
    #: The vendor's raw number, before any rule was applied.
    vendor_stock: int
    direction: Direction
    #: Plain-language explanation, shown verbatim in the dashboard and stored
    #: on the push item, e.g. "vendor has 27, capped at 15".
    reason: str
    listing: ListingEntry | None = None
    skip_reason: SkipReason | None = None

    @property
    def should_push(self) -> bool:
        return self.skip_reason is None and self.direction is not Direction.NONE

    @property
    def delta(self) -> int:
        """Signed size of the change. 0 when Amazon's quantity is unknown."""
        if self.current_quantity is None:
            return 0
        return self.desired_quantity - self.current_quantity

    @property
    def priority(self) -> tuple:
        """
        Sort key for the per-run change limit. Lower sorts first.

        Ordering, most urgent first:
          1. going to zero        -- stops unfulfillable orders
          2. going down           -- reduces exposure
          3. going up             -- recovers sales, can wait
        and within each group, the biggest change first, because that is where
        the most risk or the most money is.
        """
        rank = {Direction.TO_ZERO: 0, Direction.DOWN: 1, Direction.UP: 2, Direction.NONE: 3}
        return (rank[self.direction], -abs(self.delta), self.seller_sku)


@dataclass(slots=True)
class DecisionStats:
    """Counters for the run summary and the guardrails."""

    considered: int = 0
    to_push: int = 0
    to_zero: int = 0
    going_down: int = 0
    going_up: int = 0
    already_correct: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    largest_increase: int = 0
    largest_decrease: int = 0

    def note_skip(self, reason: SkipReason) -> None:
        self.skipped[reason.value] = self.skipped.get(reason.value, 0) + 1


# ===========================================================================
# The rules
# ===========================================================================

@dataclass(frozen=True, slots=True)
class QuantityRules:
    """
    The client's quantity policy, resolved from settings once per run.

    Frozen so it cannot drift mid-run: a run's behaviour must be explainable
    afterwards from a single snapshot, which is also why it is stored on
    ``Run.settings_snapshot``.
    """

    safety_buffer: int = 0
    max_quantity: int = 15
    out_of_stock_at: int = 0
    min_change_to_push: int = 0
    allow_increases: bool = True
    #: {"LP": {"max_quantity": 8}} -- per-product-type overrides.
    format_overrides: dict[str, dict] = field(default_factory=dict)

    def for_format(self, product_format: str) -> QuantityRules:
        """
        This rule set with any per-product-type override applied.

        The vendor's formats are LP (62,735 products), CD (49,752), BD, DVD,
        SIN, TIN, MC, ACC and a few more, so a client wanting "no more than 8
        of any vinyl" can have it without touching code.
        """
        override = self.format_overrides.get((product_format or "").upper())
        if not override:
            return self
        return QuantityRules(
            safety_buffer=int(override.get("safety_buffer", self.safety_buffer)),
            max_quantity=int(override.get("max_quantity", self.max_quantity)),
            out_of_stock_at=int(override.get("out_of_stock_at", self.out_of_stock_at)),
            min_change_to_push=int(override.get("min_change_to_push", self.min_change_to_push)),
            allow_increases=bool(override.get("allow_increases", self.allow_increases)),
            format_overrides={},
        )


def desired_quantity(vendor_stock: int, rules: QuantityRules) -> tuple[int, str]:
    """
    Turn the vendor's stock into the number Amazon should show.

    Returns ``(quantity, reason)``. The reason is written for a human and ends
    up on the dashboard and in the audit trail, because "why is this SKU at 15
    when the vendor has 900" must be answerable without reading code.

    The order of operations matters and is deliberate:
      1. out-of-stock threshold  -- decided first, so a buffer can never turn
         a genuinely-stocked product into a zero by arithmetic
      2. safety buffer
      3. maximum cap
      4. floor at zero

    With the client's chosen settings (buffer 0, cap 15, out of stock at 0):

    >>> r = QuantityRules()
    >>> desired_quantity(0, r)
    (0, 'vendor has 0, out of stock')
    >>> desired_quantity(5, r)
    (5, 'vendor has 5')
    >>> desired_quantity(27, r)
    (15, 'vendor has 27, capped at 15')
    >>> desired_quantity(5653, r)
    (15, 'vendor has 5653, capped at 15')

    With a safety buffer, which the client may want later:

    >>> b = QuantityRules(safety_buffer=2)
    >>> desired_quantity(5, b)
    (3, 'vendor has 5, less 2 held back')
    >>> desired_quantity(1, b)
    (0, 'vendor has 1, less 2 held back, floored at 0')

    With a higher out-of-stock threshold:

    >>> t = QuantityRules(out_of_stock_at=2)
    >>> desired_quantity(2, t)
    (0, 'vendor has 2, at or below the out-of-stock level of 2')
    >>> desired_quantity(3, t)
    (3, 'vendor has 3')
    """
    stock = max(0, int(vendor_stock))

    # 1. Out of stock, decided before any arithmetic.
    if stock <= rules.out_of_stock_at:
        if rules.out_of_stock_at == 0:
            return 0, f"vendor has {stock}, out of stock"
        return (
            0,
            f"vendor has {stock}, at or below the out-of-stock level of {rules.out_of_stock_at}",
        )

    parts = [f"vendor has {stock}"]
    qty = stock

    # 2. Safety buffer.
    if rules.safety_buffer > 0:
        qty -= rules.safety_buffer
        parts.append(f"less {rules.safety_buffer} held back")

    # 3. Cap.
    if qty > rules.max_quantity:
        qty = rules.max_quantity
        parts.append(f"capped at {rules.max_quantity}")

    # 4. Never negative.
    if qty < 0:
        qty = 0
        parts.append("floored at 0")

    return qty, ", ".join(parts)


# ===========================================================================
# The engine
# ===========================================================================

class DecisionEngine:
    """
    Turns matched products into a list of changes.

    Deliberately has no database and no network access. It takes plain data and
    returns plain data, which makes it exhaustively testable -- and the
    behaviour it encodes is the behaviour that touches a live revenue account,
    so it is the part that most needs testing.
    """

    def __init__(self, rules: QuantityRules) -> None:
        self.rules = rules
        self.stats = DecisionStats()

    def decide(
        self,
        match: MatchResult,
        vendor_stock: int,
        *,
        product_format: str = "",
    ) -> Decision:
        """Decide one product."""
        self.stats.considered += 1

        # ---- not matched: never guess at a live account -------------------
        if not match.matched or match.listing is None:
            self.stats.note_skip(SkipReason.UNMAPPED)
            return Decision(
                seller_sku=match.attempted_sku or "",
                barcode=match.barcode,
                current_quantity=None,
                desired_quantity=0,
                vendor_stock=vendor_stock,
                direction=Direction.NONE,
                reason=f"no confirmed Amazon listing ({match.reason})",
                skip_reason=SkipReason.UNMAPPED,
            )

        listing = match.listing
        rules = self.rules.for_format(product_format)
        want, reason = desired_quantity(vendor_stock, rules)

        base = Decision(
            seller_sku=listing.seller_sku,
            barcode=match.barcode,
            current_quantity=listing.quantity,
            desired_quantity=want,
            vendor_stock=vendor_stock,
            direction=Direction.NONE,
            reason=reason,
            listing=listing,
        )

        # ---- exclusions ---------------------------------------------------
        # Checked in order of how much they matter. FBA first: writing a
        # quantity where Amazon owns the stock is the one that could confuse a
        # listing rather than merely being useless.
        if listing.is_fba:
            base.skip_reason = SkipReason.FBA_LISTING
            base.reason = "Amazon holds this stock (FBA); Amazon owns the quantity"
            self.stats.note_skip(SkipReason.FBA_LISTING)
            return base

        if listing.blacklisted:
            base.skip_reason = SkipReason.BLACKLISTED
            base.reason = "on the never-touch list"
            self.stats.note_skip(SkipReason.BLACKLISTED)
            return base

        if not listing.is_active:
            base.skip_reason = SkipReason.INACTIVE_LISTING
            base.reason = f"listing is {listing.status or 'not active'}"
            self.stats.note_skip(SkipReason.INACTIVE_LISTING)
            return base

        # ---- no baseline to compare against -------------------------------
        if listing.quantity is None:
            # We know the listing exists but not what quantity it shows. Pushing
            # blind would work, but it would also make the rollback record
            # meaningless -- there would be no previous value to restore. Better
            # to wait one cycle for the catalogue refresh.
            base.skip_reason = SkipReason.NO_AMAZON_QUANTITY
            base.reason = (
                "Amazon's current quantity is unknown, so there would be nothing to "
                "roll back to; waiting for the next catalogue refresh"
            )
            self.stats.note_skip(SkipReason.NO_AMAZON_QUANTITY)
            return base

        current = listing.quantity

        # ---- already right -------------------------------------------------
        if want == current:
            base.skip_reason = SkipReason.ALREADY_CORRECT
            base.reason = f"already showing {current}"
            self.stats.already_correct += 1
            self.stats.note_skip(SkipReason.ALREADY_CORRECT)
            return base

        # ---- direction -----------------------------------------------------
        if want == 0:
            base.direction = Direction.TO_ZERO
        elif want < current:
            base.direction = Direction.DOWN
        else:
            base.direction = Direction.UP

        # ---- increases switched off ----------------------------------------
        if base.direction is Direction.UP and not rules.allow_increases:
            base.skip_reason = SkipReason.INCREASES_DISABLED
            base.reason = (
                f"would raise {current} to {want}, but raising quantities is "
                "currently switched off"
            )
            self.stats.note_skip(SkipReason.INCREASES_DISABLED)
            return base

        # ---- too small to bother -------------------------------------------
        # Never applied to a zero: taking a sold-out product off sale is always
        # worth doing, however small the numeric change.
        if (
            rules.min_change_to_push > 0
            and base.direction is not Direction.TO_ZERO
            and abs(want - current) < rules.min_change_to_push
        ):
            base.skip_reason = SkipReason.BELOW_MIN_CHANGE
            base.reason = (
                f"{current} to {want} is smaller than the {rules.min_change_to_push}-unit "
                "minimum worth sending"
            )
            self.stats.note_skip(SkipReason.BELOW_MIN_CHANGE)
            return base

        # ---- a real change --------------------------------------------------
        self.stats.to_push += 1
        if base.direction is Direction.TO_ZERO:
            self.stats.to_zero += 1
        elif base.direction is Direction.DOWN:
            self.stats.going_down += 1
            self.stats.largest_decrease = max(self.stats.largest_decrease, current - want)
        else:
            self.stats.going_up += 1
            self.stats.largest_increase = max(self.stats.largest_increase, want - current)

        base.reason = f"{reason}; Amazon shows {current}"
        return base

    # -- the missing-product rule -----------------------------------------
    def decide_dropped(
        self,
        listing: ListingEntry,
        *,
        missing_from_full_feeds: int,
        threshold: int,
    ) -> Decision | None:
        """
        Decide what to do about a listing the vendor no longer carries.

        This case cannot be reached by iterating the feed -- a product missing
        from the feed is by definition not in it. So the caller walks the
        Amazon side instead and calls this for anything the newest full feed
        did not mention.

        Returns ``None`` when no action is due, so the caller can simply skip.

        **Only ever driven by a FULL feed.** A delta lists only what changed, so
        absence from a delta means "unchanged" and must never be read as
        "dropped". Confusing the two would zero the catalogue.

        The client's setting is 1 -- "set it to zero straight away but dont
        delete it". The listing is never removed, from this system or from
        Amazon; only its quantity goes to zero, so it keeps its reviews and its
        ranking and can return the moment the vendor restocks.
        """
        if missing_from_full_feeds < threshold:
            return None
        if listing.quantity is None or listing.quantity == 0:
            return None  # already off sale
        if listing.is_fba or listing.blacklisted or not listing.is_active:
            return None

        self.stats.considered += 1
        self.stats.to_push += 1
        self.stats.to_zero += 1

        feeds = "full feed" if missing_from_full_feeds == 1 else f"{missing_from_full_feeds} full feeds"
        return Decision(
            seller_sku=listing.seller_sku,
            barcode=listing.barcode,
            current_quantity=listing.quantity,
            desired_quantity=0,
            vendor_stock=0,
            direction=Direction.TO_ZERO,
            reason=(
                f"the vendor has dropped this product - absent from the last {feeds}. "
                f"Amazon shows {listing.quantity}. The listing is kept, only the "
                "quantity goes to 0."
            ),
            listing=listing,
        )


# ===========================================================================
# Applying the per-run change limit
# ===========================================================================

def apply_change_limit(decisions: list[Decision], limit: int) -> tuple[list[Decision], list[Decision]]:
    """
    Split decisions into ``(this run, deferred)`` honouring the limit.

    Sorted by :attr:`Decision.priority`, so the dangerous direction goes first:
    every product that needs taking off sale is handled before any product that
    merely needs its quantity raised.

    This is what makes the first big run safe. On 2026-09-04 the backlog was
    38,112 changes; at the client's chosen limit of 5,000 per run it clears in
    about eight runs, and no single run ever looks like a runaway to the
    guardrails.

    >>> from app.engine.mapping import ListingEntry
    >>> def d(sku, cur, want):
    ...     dr = Direction.TO_ZERO if want == 0 else (Direction.UP if want > cur else Direction.DOWN)
    ...     return Decision(sku, "0000000000001", cur, want, want, dr, "test")
    >>> batch, later = apply_change_limit([d("up", 1, 9), d("zero", 4, 0), d("down", 9, 3)], 2)
    >>> [x.seller_sku for x in batch]
    ['zero', 'down']
    >>> [x.seller_sku for x in later]
    ['up']
    """
    pushable = [d for d in decisions if d.should_push]
    pushable.sort(key=lambda d: d.priority)
    if limit <= 0 or len(pushable) <= limit:
        return pushable, []
    return pushable[:limit], pushable[limit:]


def rules_from_settings(values: dict) -> QuantityRules:
    """Build a :class:`QuantityRules` from the settings dictionary."""
    return QuantityRules(
        safety_buffer=int(values.get("safety_buffer", 0)),
        max_quantity=int(values.get("max_quantity", 15)),
        out_of_stock_at=int(values.get("out_of_stock_at", 0)),
        min_change_to_push=int(values.get("min_change_to_push", 0)),
        allow_increases=bool(values.get("allow_quantity_increases", True)),
        format_overrides=dict(values.get("format_overrides") or {}),
    )
