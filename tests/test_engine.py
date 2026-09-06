"""
Tests for the decision logic, the mapping and the safety rules.

WHY THESE MATTER MOST
=====================
This is the code that decides what gets written to an account earning the
client's living. A bug here is not a broken page -- it is a product taken off
sale that should not have been, or a quantity promised that cannot be
fulfilled.

Every case below is either taken from the client's real data or encodes a rule
whose violation was measured to cause harm.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.amazon.feeds import FeedItem, build_feed_document, parse_processing_report
from app.amazon.guard import PriceFieldRefused, assert_quantity_only, describe_payload_fields
from app.amazon.listings import build_quantity_patch
from app.engine.decision import (
    Decision,
    DecisionEngine,
    Direction,
    QuantityRules,
    SkipReason,
    apply_change_limit,
    desired_quantity,
    rules_from_settings,
)
from app.engine.guardrails import (
    Severity,
    check_feed_header,
    check_feed_row_count,
    check_percent_changed,
    check_zeroing_limit,
    evaluate_batch,
)
from app.engine.mapping import CatalogIndex, ListingEntry, Mapper, measure_coverage
from app.models import MapSource

# ===========================================================================
# Helpers
# ===========================================================================

def listing(sku, barcode, qty, *, status="Active", channel="DEFAULT", lead=None, black=False):
    return ListingEntry(
        seller_sku=sku,
        sku_prefix=sku.rsplit("-", 1)[0] + "-" if "-" in sku else "",
        barcode=barcode,
        quantity=qty,
        status=status,
        fulfillment_channel=channel,
        lead_time_to_ship_days=lead,
        product_type="PRODUCT",
        blacklisted=black,
    )


# Real listings from All+Listings+Report_09-04-2026.txt.
AMS_LISTINGS = [
    listing("HA-AMS-0008811065126", "0008811065126", 1),
    listing("HA-AMS-0008811096427", "0008811096427", 1),
    listing("HA-AMS-3341348053448", "3341348053448", 1),
    listing("HA-AMS-0634457195035", "0634457195035", 5),
    listing("HA-AMS-0010058212225", "0010058212225", 0),
]

# Other suppliers on the same account. The system must never touch these.
OTHER_LISTINGS = [
    listing("HA-INGR-9798385266500", "9798385266500", 5),
    listing("RA-OLD-013431496021", "0013431496021", 1),
    listing("AS-HM-1234567", "", 3),
]


@pytest.fixture
def index():
    """
    A catalogue index scoped to HA-AMS-, as the client chose.

    Builds FRESH ListingEntry objects for every test. The module-level lists
    above are templates, not shared state: handing the same mutable objects to
    several tests once let a blacklist flag set in one test leak into another,
    which is how the mutation bug in Mapper._finish was found.
    """
    fresh = [replace(entry) for entry in AMS_LISTINGS + OTHER_LISTINGS]
    return CatalogIndex(fresh, prefixes_in_scope=["HA-AMS-"])


# ===========================================================================
# Scope: never touch another supplier
# ===========================================================================

class TestScope:
    """
    Measured on 2026-09-04 across 96,010 real listings:
        HA-AMS-    45,511, 99.5% present in the AMS feed  -> in scope
        HA-INGR-   22,519, 0.0% present  (Ingram)         -> out
        *-HM-     ~15,000, 0-2% present                  -> out
        *-OLD-      5,963, 68-85% present (ambiguous)     -> out

    Touching an out-of-scope listing would corrupt another supplier's
    inventory, so this is the most consequential filter in the system.
    """

    def test_only_in_scope_listings_are_indexed(self, index):
        assert index.in_scope_count == len(AMS_LISTINGS)
        assert index.out_of_scope_count == len(OTHER_LISTINGS)

    def test_out_of_scope_sku_is_not_reachable(self, index):
        assert index.by_sku("HA-INGR-9798385266500") is None
        assert index.by_sku("RA-OLD-013431496021") is None

    def test_out_of_scope_sku_is_still_known_to_exist(self, index):
        """
        The difference between "this SKU does not exist" and "it exists but
        belongs to Ingram" is a completely different message for the operator,
        so both facts are kept.
        """
        assert index.sku_exists_anywhere("HA-INGR-9798385266500") is True
        assert index.sku_exists_anywhere("HA-AMS-does-not-exist") is False

    def test_an_empty_scope_touches_nothing(self):
        """Fail safe: no prefixes configured means no listings in scope."""
        empty = CatalogIndex(AMS_LISTINGS, prefixes_in_scope=[])
        assert empty.in_scope_count == 0
        assert empty.by_sku("HA-AMS-0008811065126") is None

    def test_scope_is_case_insensitive(self):
        """A SKU typed by hand in Seller Central may differ in case."""
        idx = CatalogIndex(AMS_LISTINGS, prefixes_in_scope=["ha-ams-"])
        assert idx.in_scope_count == len(AMS_LISTINGS)


# ===========================================================================
# Mapping
# ===========================================================================

class TestMapping:
    def test_matches_the_vendor_form_of_a_barcode(self, index):
        """
        The vendor sends 8811065126; Amazon holds HA-AMS-0008811065126.
        This is the padding rule working end to end.
        """
        result = Mapper(index).match("8811065126")
        assert result.matched
        assert result.listing.seller_sku == "HA-AMS-0008811065126"
        assert result.source is MapSource.BARCODE_EXACT

    def test_matches_an_already_padded_barcode(self, index):
        result = Mapper(index).match("0008811065126")
        assert result.matched

    def test_matches_a_thirteen_digit_barcode_needing_no_padding(self, index):
        result = Mapper(index).match("3341348053448")
        assert result.matched
        assert result.listing.seller_sku == "HA-AMS-3341348053448"

    def test_unlisted_barcode_is_held_not_guessed(self, index):
        """
        The central safety property: a barcode with no confirmed listing is
        never pushed. A spreadsheet upload would send a constructed SKU, Amazon
        would report success, and nothing would happen.
        """
        result = Mapper(index).match("9999999999999")
        assert not result.matched
        assert result.source is MapSource.UNMAPPED
        assert result.reason == "no_listing"
        # The attempted SKU is recorded so an operator can search for it.
        assert result.attempted_sku == "HA-AMS-9999999999999"

    def test_other_suppliers_barcode_is_reported_as_such(self, index):
        """
        A barcode listed only under another supplier's prefix gets a specific
        reason, not a bare "not found" -- that is a decision an operator can
        act on.
        """
        result = Mapper(index).match("9798385266500")
        assert not result.matched
        assert result.reason == "out_of_scope_prefix"

    def test_junk_barcode_is_rejected(self, index):
        result = Mapper(index).match("123")
        assert not result.matched
        assert result.reason == "bad_barcode"

    def test_manual_override_wins_over_the_rules(self, index):
        """A human's explicit instruction outranks an automatic tier."""
        mapper = Mapper(index, overrides={"0009999999999": "HA-AMS-0008811065126"})
        result = mapper.match("9999999999")
        assert result.matched
        assert result.source is MapSource.MANUAL_OVERRIDE
        assert result.listing.seller_sku == "HA-AMS-0008811065126"

    def test_override_pointing_at_a_missing_sku_falls_through(self, index):
        """
        A bad override is a data-entry error. It must not silently become a
        push to a SKU that does not exist.
        """
        mapper = Mapper(index, overrides={"0008811065126": "HA-AMS-NOT-REAL"})
        result = mapper.match("8811065126")
        # Falls through to the barcode tier, which finds the real listing.
        assert result.matched
        assert result.source is MapSource.BARCODE_EXACT

    def test_duplicate_barcode_choice_is_stable(self):
        """
        If two listings share a barcode the choice must be deterministic, or a
        product's quantity would flap between two SKUs on every run.
        """
        dupes = [
            listing("HA-AMS-0008811065126", "0008811065126", 1),
            listing("HA-AMS-0008811065126b", "0008811065126", 2, status="Inactive"),
        ]
        idx = CatalogIndex(dupes, prefixes_in_scope=["HA-AMS-"])
        first = Mapper(idx).match("8811065126").listing.seller_sku
        second = Mapper(idx).match("8811065126").listing.seller_sku
        assert first == second
        # The Active one wins.
        assert first == "HA-AMS-0008811065126"

    def test_coverage_measurement(self, index):
        """The Stage 0 number, on a small scale."""
        vendor = {"0008811065126", "0008811096427", "3341348053448"}
        report = measure_coverage(index, vendor)
        assert report.matched_listings == 3
        assert report.unmatched_listings == 2
        assert report.listing_coverage_percent == pytest.approx(60.0)


# ===========================================================================
# Quantity rules
# ===========================================================================

class TestQuantityRules:
    """
    The client's stated rules, recorded 2026-09-05:
      "it must show the current stock that is available on the vendor"  -> buffer 0
      "never more than 15"                                             -> cap 15
      "when it is 0 on the vendor"                                     -> oos at 0
    """

    @pytest.mark.parametrize(
        ("vendor", "expected"),
        [(0, 0), (1, 1), (5, 5), (15, 15), (16, 15), (27, 15), (5653, 15)],
    )
    def test_clients_chosen_rules(self, vendor, expected):
        qty, _reason = desired_quantity(vendor, QuantityRules())
        assert qty == expected

    def test_the_cap_is_absolute(self):
        """
        The largest stock value in a real feed was 5,653. Promising that on
        Amazon has risk and no upside.
        """
        qty, reason = desired_quantity(5653, QuantityRules(max_quantity=15))
        assert qty == 15
        assert "capped at 15" in reason

    def test_reason_is_written_for_a_human(self):
        """The reason ends up on the dashboard and in the audit trail."""
        _qty, reason = desired_quantity(27, QuantityRules())
        assert reason == "vendor has 27, capped at 15"

    def test_buffer_never_goes_negative(self):
        qty, reason = desired_quantity(1, QuantityRules(safety_buffer=5))
        assert qty == 0
        assert "floored at 0" in reason

    def test_out_of_stock_is_decided_before_the_buffer(self):
        """
        Order of operations matters: the threshold is checked first, so a
        buffer can never turn a genuinely-stocked product into a zero by
        arithmetic and then report the wrong reason.
        """
        qty, reason = desired_quantity(2, QuantityRules(out_of_stock_at=2, safety_buffer=10))
        assert qty == 0
        assert "out-of-stock level" in reason

    def test_per_format_override(self):
        """
        The vendor's formats are LP (62,735 products), CD (49,752), BD, DVD and
        others, so "no more than 8 of any vinyl" must be possible without code.
        """
        rules = QuantityRules(max_quantity=15, format_overrides={"LP": {"max_quantity": 8}})
        assert desired_quantity(20, rules.for_format("LP"))[0] == 8
        assert desired_quantity(20, rules.for_format("CD"))[0] == 15

    def test_rules_from_settings(self):
        rules = rules_from_settings(
            {"safety_buffer": 2, "max_quantity": 10, "out_of_stock_at": 1,
             "min_change_to_push": 3, "allow_quantity_increases": False}
        )
        assert rules.safety_buffer == 2
        assert rules.max_quantity == 10
        assert rules.allow_increases is False


# ===========================================================================
# The decision engine
# ===========================================================================

class TestDecisionEngine:
    def test_no_change_when_already_correct(self, index):
        engine = DecisionEngine(QuantityRules())
        match = Mapper(index).match("0634457195035")   # Amazon shows 5
        decision = engine.decide(match, 5)
        assert not decision.should_push
        assert decision.skip_reason is SkipReason.ALREADY_CORRECT

    def test_going_to_zero_is_recognised(self, index):
        engine = DecisionEngine(QuantityRules())
        match = Mapper(index).match("0634457195035")   # Amazon shows 5
        decision = engine.decide(match, 0)
        assert decision.should_push
        assert decision.direction is Direction.TO_ZERO
        assert decision.desired_quantity == 0

    def test_raising_a_quantity(self, index):
        engine = DecisionEngine(QuantityRules())
        match = Mapper(index).match("0008811065126")   # Amazon shows 1
        decision = engine.decide(match, 9)
        assert decision.should_push
        assert decision.direction is Direction.UP
        assert decision.desired_quantity == 9

    def test_fba_listings_are_never_touched(self):
        """Amazon owns the quantity when Amazon holds the stock."""
        fba = [listing("HA-AMS-0008811065126", "0008811065126", 3, channel="AMAZON_NA")]
        idx = CatalogIndex(fba, prefixes_in_scope=["HA-AMS-"])
        engine = DecisionEngine(QuantityRules())
        decision = engine.decide(Mapper(idx).match("8811065126"), 0)
        assert not decision.should_push
        assert decision.skip_reason is SkipReason.FBA_LISTING

    def test_inactive_listings_are_skipped(self):
        """The account has 15,768 Inactive and 440 Incomplete listings."""
        inactive = [listing("HA-AMS-0008811065126", "0008811065126", 1, status="Inactive")]
        idx = CatalogIndex(inactive, prefixes_in_scope=["HA-AMS-"])
        engine = DecisionEngine(QuantityRules())
        decision = engine.decide(Mapper(idx).match("8811065126"), 9)
        assert not decision.should_push
        assert decision.skip_reason is SkipReason.INACTIVE_LISTING

    def test_blacklisted_sku_is_skipped(self):
        idx = CatalogIndex(AMS_LISTINGS, prefixes_in_scope=["HA-AMS-"])
        mapper = Mapper(idx, blacklist=["HA-AMS-0008811065126"])
        engine = DecisionEngine(QuantityRules())
        decision = engine.decide(mapper.match("8811065126"), 9)
        assert not decision.should_push
        assert decision.skip_reason is SkipReason.BLACKLISTED

    def test_unknown_amazon_quantity_is_not_pushed(self):
        """
        Without a previous value there would be nothing to roll back to, so the
        system waits for the next catalogue refresh rather than pushing blind.
        This is what keeps EVERY push reversible.
        """
        unknown = [listing("HA-AMS-0008811065126", "0008811065126", None)]
        idx = CatalogIndex(unknown, prefixes_in_scope=["HA-AMS-"])
        engine = DecisionEngine(QuantityRules())
        decision = engine.decide(Mapper(idx).match("8811065126"), 5)
        assert not decision.should_push
        assert decision.skip_reason is SkipReason.NO_AMAZON_QUANTITY

    def test_increases_can_be_switched_off(self, index):
        """A useful setting for a nervous first week."""
        engine = DecisionEngine(QuantityRules(allow_increases=False))
        decision = engine.decide(Mapper(index).match("0008811065126"), 9)
        assert not decision.should_push
        assert decision.skip_reason is SkipReason.INCREASES_DISABLED

    def test_going_to_zero_still_works_when_increases_are_off(self, index):
        """Protecting account health must never be switched off by accident."""
        engine = DecisionEngine(QuantityRules(allow_increases=False))
        decision = engine.decide(Mapper(index).match("0634457195035"), 0)
        assert decision.should_push
        assert decision.direction is Direction.TO_ZERO

    def test_min_change_never_blocks_a_zero(self, index):
        """
        Taking a sold-out product off sale is always worth doing, however small
        the numeric change.
        """
        engine = DecisionEngine(QuantityRules(min_change_to_push=10))
        decision = engine.decide(Mapper(index).match("0008811065126"), 0)
        assert decision.should_push
        assert decision.direction is Direction.TO_ZERO

    def test_min_change_blocks_small_moves(self, index):
        engine = DecisionEngine(QuantityRules(min_change_to_push=5))
        decision = engine.decide(Mapper(index).match("0634457195035"), 7)  # 5 -> 7
        assert not decision.should_push
        assert decision.skip_reason is SkipReason.BELOW_MIN_CHANGE

    def test_unmatched_is_never_pushed(self, index):
        engine = DecisionEngine(QuantityRules())
        decision = engine.decide(Mapper(index).match("9999999999999"), 50)
        assert not decision.should_push
        assert decision.skip_reason is SkipReason.UNMAPPED


class TestDroppedProducts:
    """
    "set it to zero straight away but dont delete it from the sytem nor from
    the amazon" -- the client, 2026-09-05.
    """

    def test_dropped_product_goes_to_zero(self):
        engine = DecisionEngine(QuantityRules())
        decision = engine.decide_dropped(
            listing("HA-AMS-0008811065126", "0008811065126", 3),
            missing_from_full_feeds=1,
            threshold=1,
        )
        assert decision is not None
        assert decision.desired_quantity == 0
        assert decision.direction is Direction.TO_ZERO
        assert "dropped this product" in decision.reason
        # The listing is kept; only the quantity moves.
        assert "listing is kept" in decision.reason

    def test_threshold_is_respected(self):
        """A client who chooses "wait for two full feeds" gets that."""
        engine = DecisionEngine(QuantityRules())
        assert engine.decide_dropped(
            listing("HA-AMS-0008811065126", "0008811065126", 3),
            missing_from_full_feeds=1, threshold=2,
        ) is None

    def test_already_zero_needs_no_action(self):
        engine = DecisionEngine(QuantityRules())
        assert engine.decide_dropped(
            listing("HA-AMS-0008811065126", "0008811065126", 0),
            missing_from_full_feeds=1, threshold=1,
        ) is None

    def test_fba_dropped_product_is_still_not_touched(self):
        engine = DecisionEngine(QuantityRules())
        assert engine.decide_dropped(
            listing("HA-AMS-0008811065126", "0008811065126", 3, channel="AMAZON_NA"),
            missing_from_full_feeds=5, threshold=1,
        ) is None


# ===========================================================================
# The change limit
# ===========================================================================

class TestChangeLimit:
    """
    On 2026-09-04 the backlog between Amazon and the vendor was 38,341
    changes. The client chose 5,000 per run, which clears it in about eight
    runs without any single run looking like a runaway.
    """

    def _mixed(self):
        return [
            Decision("up-a", "1", 1, 9, 9, Direction.UP, "raise"),
            Decision("zero-a", "2", 4, 0, 0, Direction.TO_ZERO, "off sale"),
            Decision("up-b", "3", 1, 5, 5, Direction.UP, "raise"),
            Decision("down-a", "4", 9, 3, 3, Direction.DOWN, "reduce"),
            Decision("zero-b", "5", 12, 0, 0, Direction.TO_ZERO, "off sale"),
        ]

    def test_zeros_go_first(self):
        """
        The dangerous direction is prioritised: every product needing to come
        off sale is handled before any product that merely needs raising.
        """
        batch, _ = apply_change_limit(self._mixed(), 2)
        assert {d.seller_sku for d in batch} == {"zero-a", "zero-b"}

    def test_then_reductions_then_raises(self):
        batch, deferred = apply_change_limit(self._mixed(), 3)
        assert batch[2].seller_sku == "down-a"
        assert {d.seller_sku for d in deferred} == {"up-a", "up-b"}

    def test_no_limit_sends_everything(self):
        batch, deferred = apply_change_limit(self._mixed(), 0)
        assert len(batch) == 5
        assert deferred == []

    def test_limit_above_the_work_defers_nothing(self):
        batch, deferred = apply_change_limit(self._mixed(), 5000)
        assert len(batch) == 5
        assert deferred == []

    def test_larger_changes_first_within_a_direction(self):
        """Where the most risk or the most money is."""
        decisions = [
            Decision("small", "1", 1, 2, 2, Direction.UP, ""),
            Decision("big", "2", 1, 15, 15, Direction.UP, ""),
        ]
        batch, _ = apply_change_limit(decisions, 1)
        assert batch[0].seller_sku == "big"


# ===========================================================================
# Guardrails
# ===========================================================================

class TestGuardrails:
    def test_truncated_full_feed_is_refused(self):
        """
        THE most important check. A half-downloaded feed reads as "the vendor
        has sold out of everything", and with the client's "zero it straight
        away" rule that would take the whole catalogue off sale.
        """
        result = check_feed_row_count(
            filename="FULL_FEED_110708_20260904.zip",
            row_count=300_000,
            historical_median=1_158_340,
            is_full_feed=True,
            min_percent=50.0,
        )
        assert not result.passed
        assert result.halts
        assert "cut short" in result.message

    def test_normal_full_feed_passes(self):
        result = check_feed_row_count(
            filename="FULL_FEED_110708_20260904.zip",
            row_count=1_158_340,
            historical_median=1_150_544,
            is_full_feed=True,
            min_percent=50.0,
        )
        assert result.passed

    def test_small_delta_is_fine(self):
        """Real deltas ranged from 23 to 320 rows; a size floor is meaningless."""
        result = check_feed_row_count(
            filename="DELTA_FEED_110708_20260904_88.zip",
            row_count=41,
            historical_median=1_158_340,
            is_full_feed=False,
            min_percent=50.0,
        )
        assert result.passed

    def test_first_full_feed_sets_the_baseline(self):
        result = check_feed_row_count(
            filename="FULL_FEED_110708_20260901.zip",
            row_count=1_150_544,
            historical_median=None,
            is_full_feed=True,
            min_percent=50.0,
        )
        assert result.passed
        assert result.severity is Severity.WARN

    def test_changed_header_is_refused(self):
        """
        The danger is a REORDERED column: stock read from the price column
        would produce plausible nonsense.
        """
        result = check_feed_header(
            filename="FULL_FEED.zip",
            observed="barcode|artist|title|stock|price|format",
            expected="barcode|artist|title|price|stock|format",
            matched=False,
            required=True,
        )
        assert not result.passed
        assert result.halts

    def test_header_check_can_be_relaxed(self):
        result = check_feed_header(
            filename="FULL_FEED.zip", observed="a|b", expected="c|d",
            matched=False, required=False,
        )
        assert not result.passed
        assert not result.halts   # a warning, not a stop

    def test_mass_zeroing_is_refused(self):
        """The catalogue-wipe guard."""
        decisions = [
            Decision(f"sku-{i}", str(i), 5, 0, 0, Direction.TO_ZERO, "")
            for i in range(3000)
        ]
        result = check_zeroing_limit(decisions=decisions, max_zeroing=2000)
        assert not result.passed
        assert result.halts
        assert "3,000" in result.message

    def test_normal_zeroing_passes(self):
        """A real day zeroed 196 products out of 45,511."""
        decisions = [
            Decision(f"sku-{i}", str(i), 5, 0, 0, Direction.TO_ZERO, "")
            for i in range(196)
        ]
        assert check_zeroing_limit(decisions=decisions, max_zeroing=2000).passed

    def test_percentage_limit(self):
        decisions = [
            Decision(f"sku-{i}", str(i), 1, 5, 5, Direction.UP, "") for i in range(20_000)
        ]
        result = check_percent_changed(
            proposed=len(decisions), in_scope_total=45_511, max_percent=25.0
        )
        assert not result.passed

    def test_the_real_first_run_passes(self):
        """
        The measured first batch: 5,000 changes out of 45,511 in-scope
        listings, 196 of them going to zero. This must pass, or the system
        cannot start.
        """
        decisions = (
            [Decision(f"z-{i}", str(i), 5, 0, 0, Direction.TO_ZERO, "") for i in range(196)]
            + [Decision(f"u-{i}", str(i), 1, 5, 5, Direction.UP, "") for i in range(4804)]
        )
        verdict = evaluate_batch(
            decisions,
            in_scope_total=45_511,
            settings={
                "guardrail_max_percent_changed": 25.0,
                "guardrail_max_zeroing": 2000,
                "unmapped_spike_threshold": 90_000,
            },
            unmapped=74_140,
            considered=45_368,
            hours_since_catalog_sync=1.0,
        )
        assert verdict.passed, verdict.message

    def test_all_checks_run_even_after_one_fails(self):
        """
        An operator should learn everything that is wrong from one alert, not
        discover a second fault after fixing the first.
        """
        decisions = [
            Decision(f"z-{i}", str(i), 5, 0, 0, Direction.TO_ZERO, "") for i in range(9000)
        ]
        verdict = evaluate_batch(
            decisions,
            in_scope_total=10_000,
            settings={
                "guardrail_max_percent_changed": 25.0,
                "guardrail_max_zeroing": 2000,
                "unmapped_spike_threshold": 10,
            },
            unmapped=5000,
            considered=10_000,
            hours_since_catalog_sync=100.0,
        )
        assert not verdict.passed
        # percentage, zeroing, unmapped spike and stale catalogue all reported.
        assert len(verdict.failures) >= 4


# ===========================================================================
# The price invariant
# ===========================================================================

class TestNeverSendAPrice:
    """
    The client's own filled template carries quantity in column C and price in
    column G. This is the promise that must never break, and it is enforced
    structurally rather than as a setting.
    """

    def test_a_quantity_patch_is_accepted(self):
        assert_quantity_only(build_quantity_patch(7))

    def test_a_quantity_patch_contains_no_price_field(self):
        fields = describe_payload_fields(build_quantity_patch(7, lead_time_to_ship_days=5))
        assert not any("price" in f.lower() for f in fields)
        assert fields == {
            "productType", "patches", "op", "path", "value",
            "fulfillment_channel_code", "quantity", "lead_time_to_ship_max_days",
        }

    @pytest.mark.parametrize(
        "payload",
        [
            {"our_price": 15.99},
            {"patches": [{"value": [{"our_price": 15.99}]}]},
            {"attributes": {"purchasable_offer": [{"our_price": 1}]}},
            {"a": {"b": {"c": {"minimum_seller_allowed_price": 1}}}},
            {"currency": "USD"},
            {"value_with_tax": 9.99},
            {"automated_pricing_merchandising_rule_plan": {}},
            {"attribute": "purchasable_offer[marketplace_id=ATVPDKIKX0DER]#1.our_price"},
        ],
    )
    def test_anything_price_shaped_is_refused(self, payload):
        with pytest.raises(PriceFieldRefused):
            assert_quantity_only(payload)

    def test_the_real_template_price_attribute_is_refused(self):
        """The exact string from the client's own template."""
        with pytest.raises(PriceFieldRefused):
            assert_quantity_only({
                "attributes": {
                    "purchasable_offer[marketplace_id=ATVPDKIKX0DER][audience=ALL]"
                    "#1.our_price#1.schedule#1.value_with_tax": "15.99"
                }
            })

    def test_a_feed_document_carries_only_availability(self):
        import json

        doc = json.loads(
            build_feed_document(
                [FeedItem("HA-AMS-0008811065126", 7)], seller_id="A1EXAMPLESELLER"
            )
        )
        assert list(doc["messages"][0]["attributes"]) == ["fulfillment_availability"]
        assert_quantity_only(doc)

    def test_negative_quantities_are_impossible(self):
        with pytest.raises(ValueError, match="zero or more"):
            build_quantity_patch(-1)


# ===========================================================================
# Preserving the handling time
# ===========================================================================

def test_handling_time_is_preserved():
    """
    ``fulfillment_availability`` is an array and a JSON-Patch replace swaps the
    whole thing, so a naive quantity patch would silently DELETE the seller's
    handling time. Losing it changes the promised delivery date and damages
    late-shipment metrics -- on the very account whose health we are protecting.
    """
    patch = build_quantity_patch(3, lead_time_to_ship_days=5)
    availability = patch["patches"][0]["value"][0]
    assert availability["quantity"] == 3
    assert availability["lead_time_to_ship_max_days"] == 5


def test_unknown_handling_time_is_omitted_not_nulled():
    """Sending an explicit null would clear the value, which is the bug."""
    patch = build_quantity_patch(3, lead_time_to_ship_days=None)
    assert "lead_time_to_ship_max_days" not in patch["patches"][0]["value"][0]


# ===========================================================================
# Reading Amazon's processing report
# ===========================================================================

def test_a_done_feed_can_still_have_rejected_rows():
    """
    The client's last manual upload: 656 processed, 603 successful, 53
    "successful with other errors". Treating DONE as success would have hidden
    all 53.
    """
    import json

    items = [FeedItem(f"HA-AMS-{i:013d}", 5) for i in range(1, 4)]
    report = json.dumps({
        "summary": {"messagesProcessed": 3, "messagesAccepted": 2, "messagesInvalid": 1},
        "issues": [
            {"messageId": "2", "code": "8684", "severity": "ERROR",
             "message": "SKU is associated to more than 1 GCID"},
            {"messageId": "3", "code": "99", "severity": "WARNING",
             "message": "just advice"},
        ],
    })
    outcomes, summary = parse_processing_report(report, items)

    assert outcomes[items[0].seller_sku].accepted is True
    # An ERROR means it did not take effect.
    assert outcomes[items[1].seller_sku].accepted is False
    assert outcomes[items[1].seller_sku].code == "8684"
    # A WARNING is advisory; demoting on it would create phantom failures.
    assert outcomes[items[2].seller_sku].accepted is True
    assert summary["messagesAccepted"] == 2


def test_an_unreadable_report_admits_ignorance():
    """
    Claiming success when the report cannot be parsed would be worse than
    saying so. Everything is marked unverified and settled by a read-back.
    """
    items = [FeedItem("HA-AMS-0008811065126", 5)]
    outcomes, summary = parse_processing_report("<html>not json</html>", items)
    assert outcomes[items[0].seller_sku].accepted is False
    assert outcomes[items[0].seller_sku].code == "UNPARSEABLE_REPORT"
    assert summary["parse_error"] is True
