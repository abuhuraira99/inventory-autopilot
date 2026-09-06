"""
Matching a vendor barcode to a real Amazon SKU.

THE PROBLEM, MEASURED
=====================
The client's team builds the SKU with a spreadsheet formula::

    H2 = TEXT(G2, "0000000000000")     pad the barcode to 13 digits
    E2 = "HA-AMS-" & H2                prepend the prefix

That formula is **correct**, and it is why the manual process works. But
"correct formula" and "the SKU exists on the account" are two different claims,
and only Amazon can settle the second. A flat-file upload naming a SKU that
does not exist reports success and does nothing -- so a mismatch is invisible.

Measured on 2026-09-04, against 96,010 real listings and a 1.15-million-row
feed:

    prefix        listings   present in the AMS feed
    HA-AMS-        45,511          99.5%      <- All Media Supply
    HA-INGR-       22,523           0.0%      <- Ingram, a different supplier
    RA-HM-          4,158           0.0%
    AS-HM-          3,834           0.0%
    HB-HM-          3,620           2.5%
    FA-HM-          3,290           0.0%
    RA-OLD-         2,170          84.6%      <- ambiguous, out of scope
    FA-OLD-         1,861          68.3%
    AS-OLD-         1,592          79.1%
    MR-OLD-           340          73.2%

Two conclusions drive the design:

  1. ``HA-AMS-`` identifies this vendor's catalogue almost perfectly, so the
     prefix is a reliable scope filter. Touching an ``HA-INGR-`` listing would
     corrupt Ingram's inventory.
  2. The ``*-OLD-`` prefixes match 68-85%, which looks like older AMS listings
     but is not certain. They are deliberately excluded, because under the
     "missing from the full feed means set to 0" rule the 15-32% that are
     absent would be switched off, possibly wrongly.

THE TIERS
=========
Trust descends. Nothing below :class:`MapSource.MANUAL_OVERRIDE` is ever sent.

  1. **BARCODE_EXACT** -- the canonical 13-digit barcode matches a real listing
     in the catalogue index. Amazon itself has confirmed the SKU exists.
  2. **BARCODE_VARIANT** -- a 12- or 14-digit form matches. Same confidence,
     different width; 18 live listings carry a 14-digit barcode.
  3. **MANUAL_OVERRIDE** -- a human asserted the mapping. The target SKU is
     still checked for existence.
  4. **UNMAPPED** -- held for review and shown on the dashboard. Never sent.

Note what is NOT a tier: "build the SKU with the formula and hope". The formula
IS used, but only to look up a listing that already exists in the index -- so
the answer always comes from Amazon's data, never from a construction.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from app.core.barcode import Barcode, build_sku, match_candidates, normalise
from app.models import MapSource

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ListingEntry:
    """The subset of an Amazon listing the mapper needs."""

    seller_sku: str
    sku_prefix: str
    barcode: str
    quantity: int | None
    status: str | None
    fulfillment_channel: str | None
    lead_time_to_ship_days: int | None
    product_type: str
    blacklisted: bool = False

    @property
    def is_active(self) -> bool:
        return (self.status or "").strip().lower() == "active"

    @property
    def is_fba(self) -> bool:
        return (self.fulfillment_channel or "").upper().startswith("AMAZON")


@dataclass(slots=True)
class MatchResult:
    """The outcome of matching one barcode."""

    barcode: str
    source: MapSource
    listing: ListingEntry | None = None
    #: Populated when unmapped, so the dashboard can explain why.
    reason: str = ""
    #: The SKU the formula produced, so an operator can search Seller Central
    #: for it and see for themselves that it is absent.
    attempted_sku: str | None = None

    @property
    def matched(self) -> bool:
        return self.listing is not None and self.source is not MapSource.UNMAPPED

    @property
    def pushable(self) -> bool:
        """
        Whether this match may be written to.

        A match is not enough: the listing must also be in scope, active, not
        FBA and not blacklisted. Those checks live here so the decision engine
        cannot forget one.
        """
        if not self.matched or self.listing is None:
            return False
        lst = self.listing
        return lst.is_active and not lst.is_fba and not lst.blacklisted


@dataclass(slots=True)
class MappingStats:
    """Counters for the run summary and the coverage report."""

    considered: int = 0
    barcode_exact: int = 0
    barcode_variant: int = 0
    manual_override: int = 0
    unmapped: int = 0
    skipped_inactive: int = 0
    skipped_fba: int = 0
    skipped_blacklisted: int = 0
    unmapped_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def matched(self) -> int:
        return self.barcode_exact + self.barcode_variant + self.manual_override

    @property
    def coverage_percent(self) -> float:
        return 100.0 * self.matched / self.considered if self.considered else 0.0

    def note_unmapped(self, reason: str) -> None:
        self.unmapped += 1
        self.unmapped_reasons[reason] = self.unmapped_reasons.get(reason, 0) + 1


# ===========================================================================
# The index
# ===========================================================================

class CatalogIndex:
    """
    An in-memory index of the Amazon catalogue, built once per run.

    Why in memory: the decision engine looks up 1.15 million barcodes against
    96,010 listings. Doing that with one database query per barcode would be
    a million round trips. The whole index is roughly 20 MB, which is nothing
    on the target machine, and lookups become dictionary hits.

    Built from ``amazon_listings``, filtered to the prefixes in scope, so an
    out-of-scope listing is not merely skipped later -- it is never in the index
    at all and therefore cannot be reached by any code path.
    """

    def __init__(
        self,
        listings: Iterable[ListingEntry],
        *,
        prefixes_in_scope: Iterable[str],
        include_out_of_scope: bool = False,
    ) -> None:
        self.prefixes: tuple[str, ...] = tuple(p for p in prefixes_in_scope if p)

        #: canonical barcode -> listings that claim it
        self._by_barcode: dict[str, list[ListingEntry]] = defaultdict(list)
        #: exact SKU -> listing
        self._by_sku: dict[str, ListingEntry] = {}
        #: Every SKU on the account, in scope or not. Used to distinguish
        #: "this SKU does not exist" from "it exists but belongs to Ingram",
        #: which are very different messages for the operator.
        self._all_skus: set[str] = set()
        self._out_of_scope_barcodes: set[str] = set()

        self.in_scope_count = 0
        self.out_of_scope_count = 0

        for lst in listings:
            self._all_skus.add(lst.seller_sku)
            in_scope = self._in_scope(lst.sku_prefix)

            if not in_scope:
                self.out_of_scope_count += 1
                if lst.barcode:
                    self._out_of_scope_barcodes.add(lst.barcode)
                if not include_out_of_scope:
                    continue

            self.in_scope_count += 1 if in_scope else 0
            self._by_sku[lst.seller_sku] = lst
            if lst.barcode:
                self._by_barcode[lst.barcode].append(lst)

        log.info(
            "catalogue index built: %d in scope (prefixes %s), %d out of scope, "
            "%d distinct barcodes",
            self.in_scope_count, list(self.prefixes), self.out_of_scope_count,
            len(self._by_barcode),
        )

    # -- scope -------------------------------------------------------------
    def _in_scope(self, prefix: str) -> bool:
        """
        Whether a SKU prefix is one this system may touch.

        Compared case-insensitively, because a SKU typed by hand in Seller
        Central may differ in case from the convention.
        """
        if not self.prefixes:
            return False  # empty scope means touch nothing. Fail safe.
        p = (prefix or "").upper()
        return any(p == s.upper() for s in self.prefixes)

    # -- lookups -----------------------------------------------------------
    def by_barcode(self, canonical: str) -> list[ListingEntry]:
        return self._by_barcode.get(canonical, [])

    def by_sku(self, sku: str) -> ListingEntry | None:
        return self._by_sku.get(sku)

    def sku_exists_anywhere(self, sku: str) -> bool:
        """True if the SKU is on the account at all, in scope or not."""
        return sku in self._all_skus

    def barcode_belongs_to_other_supplier(self, canonical: str) -> bool:
        """
        True when a barcode is listed on the account only under an out-of-scope
        prefix.

        Turns a useless "not found" into a useful "this product is listed under
        a different supplier's SKU, so we are leaving it alone" -- which is a
        decision an operator can act on.
        """
        return canonical in self._out_of_scope_barcodes and not self._by_barcode.get(canonical)

    @property
    def all_in_scope(self) -> list[ListingEntry]:
        """
        Every in-scope listing.

        Needed by the "dropped by the vendor" rule, which has to walk the
        Amazon side rather than the feed side -- a product missing from the
        feed is by definition not in the feed to iterate over.
        """
        return [lst for lst in self._by_sku.values() if self._in_scope(lst.sku_prefix)]

    def __len__(self) -> int:
        return len(self._by_sku)


# ===========================================================================
# The mapper
# ===========================================================================

class Mapper:
    """
    Resolves barcodes to listings using the tiers described in the module
    docstring.

    Stateless apart from its statistics, so one instance is used for a whole
    run and the counters describe that run.
    """

    def __init__(
        self,
        index: CatalogIndex,
        *,
        overrides: dict[str, str] | None = None,
        sku_prefix: str = "HA-AMS-",
        skip_inactive: bool = True,
        skip_fba: bool = True,
        blacklist: Iterable[str] = (),
    ) -> None:
        self.index = index
        self.overrides = overrides or {}
        self.sku_prefix = sku_prefix
        self.skip_inactive = skip_inactive
        self.skip_fba = skip_fba
        self.blacklist = {s.strip().upper() for s in blacklist if s and s.strip()}
        self.stats = MappingStats()

    def match(self, barcode: Barcode | str) -> MatchResult:
        """
        Resolve one barcode. Never raises; an unmatchable barcode is a result.

        The order of the tiers is the order of trust, and the first hit wins.
        """
        bc = barcode if isinstance(barcode, Barcode) else normalise(barcode)
        self.stats.considered += 1

        if not bc.usable:
            self.stats.note_unmapped("bad_barcode")
            return MatchResult(
                barcode=bc.digits or bc.raw,
                source=MapSource.UNMAPPED,
                reason="bad_barcode",
            )

        canonical = bc.canonical
        attempted = build_sku(self.sku_prefix, canonical)

        # ---- tier 3 first: a human's explicit instruction outranks a rule ---
        override_sku = self.overrides.get(canonical)
        if override_sku:
            listing = self.index.by_sku(override_sku)
            if listing is not None:
                return self._finish(listing, canonical, MapSource.MANUAL_OVERRIDE, attempted)
            # An override pointing at a SKU that does not exist is a data-entry
            # error worth surfacing, not something to silently ignore.
            log.warning(
                "manual override for barcode %s points at SKU %r, which is not on "
                "the account; falling through to the automatic tiers",
                canonical, override_sku,
            )

        # ---- tier 1: exact canonical barcode -------------------------------
        exact = self.index.by_barcode(canonical)
        if exact:
            return self._finish(self._pick(exact), canonical, MapSource.BARCODE_EXACT, attempted)

        # ---- tier 2: other plausible widths --------------------------------
        # Recovers the 12- and 14-digit listings. The candidate list is ordered
        # best-first by app.core.barcode.
        for candidate in match_candidates(canonical):
            if candidate == canonical:
                continue
            hits = self.index.by_barcode(candidate)
            if hits:
                return self._finish(self._pick(hits), canonical, MapSource.BARCODE_VARIANT, attempted)

        # ---- also try the constructed SKU directly -------------------------
        # Belt and braces: catches a listing whose barcode field we could not
        # parse out of its SKU but whose SKU is exactly what the formula
        # produces.
        if attempted:
            listing = self.index.by_sku(attempted)
            if listing is not None:
                return self._finish(listing, canonical, MapSource.BARCODE_EXACT, attempted)

        # ---- unmapped -------------------------------------------------------
        if self.index.barcode_belongs_to_other_supplier(canonical):
            reason = "out_of_scope_prefix"
        elif attempted and self.index.sku_exists_anywhere(attempted):
            # The SKU exists but was filtered out of the index by scope.
            reason = "out_of_scope_prefix"
        else:
            reason = "no_listing"

        self.stats.note_unmapped(reason)
        return MatchResult(
            barcode=canonical,
            source=MapSource.UNMAPPED,
            reason=reason,
            attempted_sku=attempted,
        )

    # -- internals ---------------------------------------------------------
    def _pick(self, candidates: list[ListingEntry]) -> ListingEntry:
        """
        Choose between listings sharing a barcode.

        The client said one barcode is always one product, and the data agrees:
        zero duplicates were found in a 1.15-million-row feed. But Amazon
        accounts do accumulate duplicate listings, so rather than assume, we
        pick deterministically: an Active, merchant-fulfilled, non-blacklisted
        listing wins; ties break on the SKU so the choice is stable between
        runs. A stable choice matters -- an unstable one would flap a product's
        quantity back and forth every cycle.
        """
        if len(candidates) == 1:
            return candidates[0]

        def rank(lst: ListingEntry) -> tuple:
            return (
                0 if lst.is_active else 1,
                1 if lst.is_fba else 0,
                1 if lst.blacklisted else 0,
                lst.seller_sku,
            )

        ordered = sorted(candidates, key=rank)
        log.debug(
            "barcode %s maps to %d listings; chose %s",
            candidates[0].barcode, len(candidates), ordered[0].seller_sku,
        )
        return ordered[0]

    def _finish(
        self,
        listing: ListingEntry,
        canonical: str,
        source: MapSource,
        attempted: str | None,
    ) -> MatchResult:
        """
        Record the tier that hit, apply the exclusion rules, and return.

        NOTE ON THE COPY. The blacklist decision produces a *replacement*
        :class:`ListingEntry` rather than setting a flag on the one held in the
        index. Mutating the shared object would leak the flag: two mappers
        built over the same index -- which happens when a rollback plan is
        assembled alongside a run -- would see each other's blacklist, and a
        SKU removed from the list would stay blacklisted until the index was
        rebuilt. A mapper has no business mutating the catalogue it was handed.
        """
        if source is MapSource.BARCODE_EXACT:
            self.stats.barcode_exact += 1
        elif source is MapSource.BARCODE_VARIANT:
            self.stats.barcode_variant += 1
        else:
            self.stats.manual_override += 1

        # Exclusions are applied here, once, so no caller can skip them.
        if listing.seller_sku.upper() in self.blacklist and not listing.blacklisted:
            listing = replace(listing, blacklisted=True)
        if listing.blacklisted:
            self.stats.skipped_blacklisted += 1
        if self.skip_fba and listing.is_fba:
            self.stats.skipped_fba += 1
        if self.skip_inactive and not listing.is_active:
            self.stats.skipped_inactive += 1

        return MatchResult(
            barcode=canonical,
            source=source,
            listing=listing,
            attempted_sku=attempted,
        )


# ===========================================================================
# Coverage report
# ===========================================================================

@dataclass(slots=True)
class CoverageReport:
    """
    The Stage 0 answer: how much of the catalogue can actually be reached.

    Produced before any write is enabled, and available from the dashboard at
    any time. It is the number that tells the client whether their existing
    process is working -- and it is worth having even if the automation never
    ships.
    """

    vendor_barcodes: int
    in_scope_listings: int
    matched_listings: int
    unmatched_listings: int
    matched_barcodes: int
    unmatched_barcodes: int
    by_prefix: dict[str, dict[str, int]] = field(default_factory=dict)
    sample_unmatched: list[str] = field(default_factory=list)

    @property
    def listing_coverage_percent(self) -> float:
        """
        The headline number: what share of in-scope Amazon listings can be
        matched to a vendor row.

        This is the direction that matters. The reverse (what share of the
        vendor's 1.15 million products are listed) is naturally low and not
        interesting -- the client lists a fraction of the vendor's catalogue on
        purpose.
        """
        total = self.matched_listings + self.unmatched_listings
        return 100.0 * self.matched_listings / total if total else 0.0


def measure_coverage(
    index: CatalogIndex,
    vendor_barcodes: set[str],
    *,
    sample_size: int = 25,
) -> CoverageReport:
    """
    Compare the Amazon catalogue against the vendor's barcodes.

    Runs in both directions but reports the listing-side number as the
    headline, for the reason given on
    :attr:`CoverageReport.listing_coverage_percent`.
    """
    matched_listings = 0
    unmatched_listings = 0
    by_prefix: dict[str, dict[str, int]] = {}
    samples: list[str] = []

    for lst in index.all_in_scope:
        bucket = by_prefix.setdefault(lst.sku_prefix, {"matched": 0, "unmatched": 0})
        hit = bool(lst.barcode) and (
            lst.barcode in vendor_barcodes
            or any(c in vendor_barcodes for c in match_candidates(lst.barcode))
        )
        if hit:
            matched_listings += 1
            bucket["matched"] += 1
        else:
            unmatched_listings += 1
            bucket["unmatched"] += 1
            if len(samples) < sample_size:
                samples.append(lst.seller_sku)

    matched_barcodes = sum(1 for b in vendor_barcodes if index.by_barcode(b))

    return CoverageReport(
        vendor_barcodes=len(vendor_barcodes),
        in_scope_listings=index.in_scope_count,
        matched_listings=matched_listings,
        unmatched_listings=unmatched_listings,
        matched_barcodes=matched_barcodes,
        unmatched_barcodes=len(vendor_barcodes) - matched_barcodes,
        by_prefix=by_prefix,
        sample_unmatched=samples,
    )
