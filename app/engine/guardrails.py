"""
The circuit breakers: the last chance to stop a run before it reaches Amazon.

WHAT THIS PROTECTS AGAINST
==========================
Not bad code -- the tests cover that. Bad *data*, arriving from a system nobody
here controls. The realistic disasters, in order of how much they would cost:

  1. **A truncated vendor file.** A download cut short reads as "the vendor has
     sold out of everything". Without a brake, the system would set the whole
     45,511-listing catalogue to zero, and the client's business would stop
     until somebody noticed. Two independent defences catch this: the archive
     CRC check in :mod:`app.vendor.parser` and the zeroing limit here.

  2. **The wrong vendor's file.** If a file for a different account appeared in
     the folder, almost none of its barcodes would match, and almost every
     in-scope listing would look dropped.

  3. **A vendor format change.** If the columns were reordered, stock could be
     read out of the price column. Prices in this feed run from about 3 to 30,
     so the result would look plausible -- which is exactly what makes it
     dangerous.

  4. **A bug we have not thought of.** The percentage limit is a blunt
     instrument that catches surprises of a kind nobody predicted, which is the
     point of having it.

DESIGN
======
Every check returns a :class:`GuardrailResult` rather than raising, so a run
evaluates **all** of them and reports every problem at once. An operator should
learn everything that is wrong in one email, not discover a second fault after
fixing the first.

Thresholds are settings, not constants -- with one exception. The
"never send a price" rule is not here at all: it lives in
:mod:`app.amazon.guard` as a structural invariant, because a safety rule that
important must not be one careless click away from being switched off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from app.engine.decision import Decision, Direction

log = logging.getLogger(__name__)


class Severity(str, Enum):
    """
    How a failed check is treated.

    HALT stops the run and sends nothing. WARN records the concern, alerts, and
    continues -- used where the condition is suspicious but stopping would
    itself be harmful.
    """

    HALT = "halt"
    WARN = "warn"


@dataclass(slots=True)
class GuardrailResult:
    """The outcome of one check."""

    name: str
    passed: bool
    severity: Severity
    #: Written for the client, not for a developer. Goes straight into the
    #: dashboard and the alert email.
    message: str
    #: The numbers behind the verdict, for the run record.
    detail: dict = field(default_factory=dict)

    @property
    def halts(self) -> bool:
        return not self.passed and self.severity is Severity.HALT


@dataclass(slots=True)
class GuardrailVerdict:
    """All checks, and whether the run may proceed."""

    results: list[GuardrailResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(r.halts for r in self.results)

    @property
    def failures(self) -> list[GuardrailResult]:
        return [r for r in self.results if not r.passed]

    @property
    def halting(self) -> list[GuardrailResult]:
        return [r for r in self.results if r.halts]

    @property
    def warnings(self) -> list[GuardrailResult]:
        return [r for r in self.results if not r.passed and r.severity is Severity.WARN]

    @property
    def message(self) -> str:
        """A single paragraph explaining the verdict, for the alert email."""
        if self.passed and not self.failures:
            return "All safety checks passed."
        if self.passed:
            return "Proceeding, with warnings: " + " ".join(w.message for w in self.warnings)
        return "RUN STOPPED. " + " ".join(h.message for h in self.halting)

    def as_dict(self) -> dict:
        """Serialised onto ``Run.guardrail_detail`` so a halt is explainable later."""
        return {
            "passed": self.passed,
            "checks": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "severity": r.severity.value,
                    "message": r.message,
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }


# ===========================================================================
# Feed-level checks: run before a file is parsed into the database
# ===========================================================================

def check_feed_row_count(
    *,
    filename: str,
    row_count: int,
    historical_median: int | None,
    is_full_feed: bool,
    min_percent: float,
) -> GuardrailResult:
    """
    Refuse a full feed that is suspiciously small.

    THE most important check in the system. A full feed on this account carries
    about 1.15 million rows; a half-finished download might carry 300,000, and
    every product absent from those 300,000 would look dropped. With the
    client's "set it to zero straight away" rule, that is the whole catalogue
    off sale.

    Only applied to full feeds. Delta files are legitimately tiny -- the twenty
    observed ranged from 23 to 320 rows -- so a size floor there would be
    meaningless.
    """
    if not is_full_feed:
        return GuardrailResult(
            "feed_row_count", True, Severity.HALT,
            "Delta feeds have no minimum size, so this check does not apply.",
            {"row_count": row_count, "is_full_feed": False},
        )

    if historical_median is None:
        # Nothing to compare against yet. On a brand-new deployment the first
        # full feed sets the baseline, so this is accepted -- but noted, so the
        # operator knows the check was not able to run.
        return GuardrailResult(
            "feed_row_count", True, Severity.WARN,
            f"{filename} has {row_count:,} rows. There is no previous full feed to "
            "compare against, so this one becomes the baseline. The size check will "
            "be active from the next full feed onwards.",
            {"row_count": row_count, "historical_median": None},
        )

    floor = int(historical_median * min_percent / 100.0)
    if row_count >= floor:
        return GuardrailResult(
            "feed_row_count", True, Severity.HALT,
            f"{filename} has {row_count:,} rows, which is normal "
            f"(previous full feeds average {historical_median:,}).",
            {"row_count": row_count, "historical_median": historical_median, "floor": floor},
        )

    return GuardrailResult(
        "feed_row_count", False, Severity.HALT,
        f"STOPPED: {filename} contains only {row_count:,} rows, but previous full "
        f"feeds have about {historical_median:,}. That is below the {min_percent:g}% "
        f"minimum of {floor:,} rows.\n\n"
        "A file this small is almost always a download that was cut short, or a file "
        "the vendor was still uploading. Processing it would make most of the "
        "catalogue look out of stock and could take thousands of products off sale.\n\n"
        "Nothing has been changed. The file will be tried again on the next cycle. "
        "If the vendor has genuinely reduced their catalogue this much, lower the "
        "minimum in Settings.",
        {"row_count": row_count, "historical_median": historical_median, "floor": floor},
    )


def check_feed_header(
    *,
    filename: str,
    observed: str,
    expected: str,
    matched: bool,
    required: bool,
) -> GuardrailResult:
    """
    Refuse a feed whose columns have changed.

    The danger is not a missing column -- that fails loudly. It is a
    *reordered* one. Swap ``price`` and ``stock`` in this feed and the system
    would read stock values of 12.12 and 9.00, round them to 12 and 9, and
    publish confident nonsense.
    """
    if matched:
        return GuardrailResult(
            "feed_header", True, Severity.HALT,
            f"{filename} has the expected columns.",
            {"observed": observed},
        )
    severity = Severity.HALT if required else Severity.WARN
    verb = "STOPPED" if required else "WARNING"
    return GuardrailResult(
        "feed_header", False, severity,
        f"{verb}: {filename} does not have the expected columns.\n"
        f"  expected: {expected}\n"
        f"  found:    {observed}\n\n"
        "This matters because a reordered column would be read as the wrong kind of "
        "value - stock could be taken from the price column, which would look "
        "plausible and be completely wrong.\n\n"
        + (
            "Nothing has been changed. If the vendor really has changed their format, "
            "update 'Which column is which' in Settings."
            if required
            else "Continuing anyway because the strict header check is switched off."
        ),
        {"observed": observed, "expected": expected},
    )


def check_rejected_row_ratio(
    *, filename: str, rejected: int, total: int, max_percent: float = 5.0
) -> GuardrailResult:
    """
    Warn when an unusual share of rows could not be read.

    A warning rather than a halt: this feed normally carries about 153 junk
    rows in 1.15 million (0.013%), and the client should not lose a whole
    cycle's stock updates because a few more arrived. But a jump means the
    vendor has changed something.
    """
    if total == 0:
        return GuardrailResult("rejected_rows", True, Severity.WARN, "No rows to check.", {})
    percent = 100.0 * rejected / total
    if percent <= max_percent:
        return GuardrailResult(
            "rejected_rows", True, Severity.WARN,
            f"{rejected:,} of {total:,} rows could not be read ({percent:.2f}%), which "
            "is normal for this vendor.",
            {"rejected": rejected, "total": total, "percent": round(percent, 2)},
        )
    return GuardrailResult(
        "rejected_rows", False, Severity.WARN,
        f"WARNING: {rejected:,} of {total:,} rows in {filename} could not be read "
        f"({percent:.1f}%). Normally this is well under 1%. The vendor may have "
        "changed their format. The readable rows have still been processed.",
        {"rejected": rejected, "total": total, "percent": round(percent, 2)},
    )


# ===========================================================================
# Batch-level checks: run after decisions, before anything is sent
# ===========================================================================

def check_percent_changed(
    *, proposed: int, in_scope_total: int, max_percent: float
) -> GuardrailResult:
    """
    Refuse a run that would change too much of the catalogue at once.

    The blunt instrument. It does not know *why* a run wants to change 40,000
    listings, only that a normal run changes a few hundred, and that a sudden
    jump deserves a human's attention before it reaches a live account.

    Note the interaction with the change limit: because
    :func:`app.engine.decision.apply_change_limit` runs first, a large backlog
    arrives here already trimmed to the per-run limit. So this check fires on a
    genuinely anomalous run, not on the expected first-day catch-up.
    """
    if in_scope_total == 0:
        return GuardrailResult(
            "percent_changed", True, Severity.HALT,
            "No listings are in scope yet, so there is nothing to compare against.",
            {"proposed": proposed, "in_scope_total": 0},
        )
    percent = 100.0 * proposed / in_scope_total
    if percent <= max_percent:
        return GuardrailResult(
            "percent_changed", True, Severity.HALT,
            f"This run would change {proposed:,} of {in_scope_total:,} listings "
            f"({percent:.1f}%), within the {max_percent:g}% limit.",
            {"proposed": proposed, "in_scope_total": in_scope_total, "percent": round(percent, 2)},
        )
    return GuardrailResult(
        "percent_changed", False, Severity.HALT,
        f"STOPPED: this run wants to change {proposed:,} of {in_scope_total:,} "
        f"listings ({percent:.1f}%), which is above the {max_percent:g}% safety limit.\n\n"
        "A normal run changes a few hundred. A jump this large usually means a "
        "problem with the vendor's file rather than a real change in their stock.\n\n"
        "Nothing has been sent to Amazon. Look at the proposed changes on the "
        "dashboard: if they are genuinely correct, approve the run manually or raise "
        "the limit in Settings.",
        {"proposed": proposed, "in_scope_total": in_scope_total, "percent": round(percent, 2)},
    )


def check_zeroing_limit(*, decisions: list[Decision], max_zeroing: int) -> GuardrailResult:
    """
    Refuse a run that would take too many products off sale at once.

    The most consequential single check, because zeroing is irreversible in
    business terms even though it is reversible in data terms: sales lost while
    a product was wrongly off sale do not come back.

    For calibration, a real day on this account: the 2026-09-04 comparison
    found 102 products needing to go to zero from the feed, plus 107 dropped
    entirely. The default limit of 2,000 leaves a wide margin over normal
    behaviour while still catching a catalogue-wide wipe.
    """
    zeroing = [d for d in decisions if d.direction is Direction.TO_ZERO]
    count = len(zeroing)

    if count <= max_zeroing:
        return GuardrailResult(
            "zeroing_limit", True, Severity.HALT,
            f"{count:,} products would be taken off sale, within the limit of "
            f"{max_zeroing:,}.",
            {"zeroing": count, "limit": max_zeroing},
        )

    examples = ", ".join(d.seller_sku for d in zeroing[:5])
    return GuardrailResult(
        "zeroing_limit", False, Severity.HALT,
        f"STOPPED: this run would take {count:,} products off sale, which is above "
        f"the limit of {max_zeroing:,}.\n\n"
        "This is the check that exists to prevent the worst thing this system could "
        "do - switching off a large part of the catalogue because of a bad file.\n\n"
        f"Examples: {examples}\n\n"
        "Nothing has been sent to Amazon. Check the vendor's file on the dashboard. "
        "If the vendor really has sold out of this many products, approve the run "
        "manually or raise the limit in Settings.",
        {"zeroing": count, "limit": max_zeroing, "examples": [d.seller_sku for d in zeroing[:20]]},
    )


def check_no_price_in_decisions(*, decisions: list[Decision]) -> GuardrailResult:
    """
    Confirm no decision carries a price. Belt and braces.

    :class:`Decision` has no price field, so this cannot fail as the code
    stands -- and that is the point. It is here so that if somebody ever adds
    one, a test fails and a run stops, rather than a price quietly reaching a
    live listing. Cheap insurance on the one promise the client cares most
    about.
    """
    suspicious = [
        d.seller_sku
        for d in decisions
        if any("price" in f.lower() for f in getattr(d, "__slots__", ()))
    ]
    if not suspicious:
        return GuardrailResult(
            "no_price_fields", True, Severity.HALT,
            "Confirmed: these changes contain stock quantities only, no prices.",
            {},
        )
    return GuardrailResult(  # pragma: no cover - unreachable by design
        "no_price_fields", False, Severity.HALT,
        "STOPPED: a proposed change carried price information. This system must "
        "never change a price. Nothing has been sent. This is a code fault - see "
        "app/amazon/guard.py.",
        {"skus": suspicious[:20]},
    )


def check_unmapped_spike(
    *, unmapped: int, threshold: int, considered: int
) -> GuardrailResult:
    """
    Warn when an unusual number of products cannot be matched.

    A warning, not a halt: unmatched products are never pushed anyway, so they
    are already safe. But a spike is the signature of the vendor changing their
    barcode format, or somebody renaming SKUs in Seller Central -- and the
    client should hear about that on the day it happens rather than a month
    later.
    """
    if unmapped <= threshold:
        return GuardrailResult(
            "unmapped_spike", True, Severity.WARN,
            f"{unmapped:,} products could not be matched to an Amazon listing, which "
            "is within the normal range.",
            {"unmapped": unmapped, "threshold": threshold, "considered": considered},
        )
    return GuardrailResult(
        "unmapped_spike", False, Severity.WARN,
        f"WARNING: {unmapped:,} products could not be matched to an Amazon listing, "
        f"which is above the usual {threshold:,}.\n\n"
        "None of them have been changed - unmatched products are never sent. But a "
        "jump like this usually means either the vendor has changed how they write "
        "barcodes, or some SKUs were renamed in Seller Central.\n\n"
        "The full list is on the dashboard under Unmatched.",
        {"unmapped": unmapped, "threshold": threshold, "considered": considered},
    )


def check_catalog_freshness(*, hours_since_sync: float | None, max_hours: float = 48.0) -> GuardrailResult:
    """
    Warn when Amazon's catalogue snapshot is stale.

    Everything depends on knowing Amazon's current quantities. If the daily
    report has not run for two days, the comparison is against old numbers and
    the system might "correct" a quantity that is already right -- harmless,
    but it clutters the audit trail and wastes rate budget.

    A warning rather than a halt, because refusing to fix a sold-out product
    just because the snapshot is a day old would be worse than the staleness.
    """
    if hours_since_sync is None:
        return GuardrailResult(
            "catalog_freshness", False, Severity.WARN,
            "WARNING: Amazon's catalogue has never been downloaded, so there is no "
            "record of what Amazon currently shows. Run 'Refresh Amazon catalogue' "
            "from the dashboard before enabling any sending.",
            {"hours_since_sync": None},
        )
    if hours_since_sync <= max_hours:
        return GuardrailResult(
            "catalog_freshness", True, Severity.WARN,
            f"Amazon's catalogue was last refreshed {hours_since_sync:.1f} hours ago.",
            {"hours_since_sync": round(hours_since_sync, 1)},
        )
    return GuardrailResult(
        "catalog_freshness", False, Severity.WARN,
        f"WARNING: Amazon's catalogue was last refreshed {hours_since_sync:.0f} hours "
        f"ago, which is more than the {max_hours:.0f}-hour limit. Comparisons are "
        "being made against old numbers. Check the daily refresh is working.",
        {"hours_since_sync": round(hours_since_sync, 1), "max_hours": max_hours},
    )


# ===========================================================================
# Running all of them
# ===========================================================================

def evaluate_batch(
    decisions: list[Decision],
    *,
    in_scope_total: int,
    settings: dict,
    unmapped: int = 0,
    considered: int = 0,
    hours_since_catalog_sync: float | None = None,
) -> GuardrailVerdict:
    """
    Run every batch-level check and return the combined verdict.

    All checks run even after one fails, so a single alert tells the operator
    everything that is wrong.
    """
    verdict = GuardrailVerdict()

    verdict.results.append(
        check_percent_changed(
            proposed=len(decisions),
            in_scope_total=in_scope_total,
            max_percent=float(settings.get("guardrail_max_percent_changed", 25.0)),
        )
    )
    verdict.results.append(
        check_zeroing_limit(
            decisions=decisions,
            max_zeroing=int(settings.get("guardrail_max_zeroing", 2000)),
        )
    )
    verdict.results.append(check_no_price_in_decisions(decisions=decisions))
    verdict.results.append(
        check_unmapped_spike(
            unmapped=unmapped,
            threshold=int(settings.get("unmapped_spike_threshold", 500)),
            considered=considered,
        )
    )
    verdict.results.append(check_catalog_freshness(hours_since_sync=hours_since_catalog_sync))

    if verdict.passed:
        log.info("guardrails passed: %d changes cleared to send", len(decisions))
    else:
        for r in verdict.halting:
            log.error("guardrail %s STOPPED the run: %s", r.name, r.message.splitlines()[0])

    return verdict


def evaluate_feed(
    *,
    filename: str,
    row_count: int,
    rejected: int,
    historical_median: int | None,
    is_full_feed: bool,
    observed_header: str,
    expected_header: str,
    header_matched: bool,
    settings: dict,
) -> GuardrailVerdict:
    """Run every feed-level check for one file, before it is trusted."""
    verdict = GuardrailVerdict()
    verdict.results.append(
        check_feed_row_count(
            filename=filename,
            row_count=row_count,
            historical_median=historical_median,
            is_full_feed=is_full_feed,
            min_percent=float(settings.get("guardrail_min_feed_rows_percent", 50.0)),
        )
    )
    verdict.results.append(
        check_feed_header(
            filename=filename,
            observed=observed_header,
            expected=expected_header,
            matched=header_matched,
            required=bool(settings.get("guardrail_require_known_header", True)),
        )
    )
    verdict.results.append(
        check_rejected_row_ratio(filename=filename, rejected=rejected, total=row_count + rejected)
    )
    return verdict
