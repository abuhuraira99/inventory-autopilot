"""
Tests for the barcode rules.

These are the highest-value tests in the project. Every case below is either
taken from the client's real data or encodes a rule whose violation was
*measured* to break the system:

  * getting the padding wrong loses 29,701 of 45,511 listings (65.3%)
  * treating a 7-digit junk barcode as real would send a malformed SKU
  * failing to recognise 14-digit GTINs orphans 18 live listings
"""

from __future__ import annotations

import pytest

from app.core.barcode import (
    CANONICAL_WIDTH,
    Barcode,
    barcode_from_sku,
    build_sku,
    candidate_skus,
    gtin_check_digit,
    is_valid_gtin,
    match_candidates,
    normalise,
    sku_prefix,
    split_sku,
)
from tests.conftest import REAL_AMS_SKUS, REAL_MALFORMED_SKUS

# ===========================================================================
# The central rule: pad to 13
# ===========================================================================

class TestPaddingIsTheWholeGame:
    """
    The vendor strips leading zeros; Amazon keeps them. Every one of these
    pairs was verified against the live All Listings Report.
    """

    @pytest.mark.parametrize(
        ("vendor_sends", "amazon_has"),
        [
            # (vendor barcode, the SKU that actually exists on the account)
            ("8811065126", "HA-AMS-0008811065126"),   # 10 digits -> 3 zeros
            ("8811096427", "HA-AMS-0008811096427"),
            ("10058212225", "HA-AMS-0010058212225"),  # 11 digits -> 2 zeros
            ("634457195035", "HA-AMS-0634457195035"), # 12 digits -> 1 zero
            ("3341348053448", "HA-AMS-3341348053448"),# 13 digits -> unchanged
        ],
    )
    def test_builds_the_sku_that_actually_exists(self, vendor_sends, amazon_has):
        assert build_sku("HA-AMS-", vendor_sends) == amazon_has

    def test_naive_concatenation_would_be_wrong(self):
        """
        Documents the bug this module exists to prevent.

        Plain string concatenation produces a SKU that does not exist on the
        account, and Amazon does not report an error for an unknown SKU -- it
        silently does nothing. That is how an update fails invisibly.
        """
        vendor = "8811065126"
        naive = "HA-AMS-" + vendor
        correct = build_sku("HA-AMS-", vendor)
        assert naive == "HA-AMS-8811065126"
        assert correct == "HA-AMS-0008811065126"
        assert naive != correct

    def test_canonical_is_always_thirteen_wide(self):
        for raw in ("1", "12345678", "8811065126", "634457195035", "3341348053448"):
            bc = normalise(raw)
            if bc.usable:
                assert len(bc.canonical) == CANONICAL_WIDTH, raw

    def test_matches_the_clients_spreadsheet_formula(self):
        """
        The client's sheet does TEXT(barcode, "0000000000000"), i.e. pad to 13.
        This asserts we produce byte-identical output for that formula.
        """
        for raw in ("45778689316", "8811060626", "656605142524"):
            expected = raw.zfill(13)
            assert normalise(raw).canonical == expected


# ===========================================================================
# Rejecting rubbish
# ===========================================================================

class TestRejectsJunk:
    """
    The live full feed carries ~150 unusable rows a day (barcodes of 1-7
    characters). They must be recorded and skipped, never sent.
    """

    @pytest.mark.parametrize("junk", ["", "   ", "1", "12", "123", "1234567", "abc", "-", None])
    def test_unusable(self, junk):
        assert normalise(junk).usable is False

    @pytest.mark.parametrize("junk", ["1", "1234567"])
    def test_no_sku_is_built_for_junk(self, junk):
        assert build_sku("HA-AMS-", junk) is None

    def test_too_long_is_rejected(self):
        # 15 digits is longer than any GTIN and is certainly a data error.
        assert normalise("123456789012345").usable is False

    def test_empty_candidate_list_for_junk(self):
        assert match_candidates("") == []
        assert match_candidates(None) == []


# ===========================================================================
# Real-world messiness
# ===========================================================================

class TestHandlesMessyInput:
    def test_strips_separators_and_whitespace(self):
        assert normalise("  811576-034265 ").canonical == "0811576034265"
        assert normalise("0 850054 327437").canonical == "0850054327437"

    def test_handles_spreadsheet_floats(self):
        """
        The client's own sheet has barcodes as floats in scientific notation
        (column G contains values like 1.2414152725E10). str() on those gives
        '12414152725.0' or worse, so they are converted through int().
        """
        assert normalise(1.2414152725e10).canonical == "0012414152725"
        assert normalise(12414152725).canonical == "0012414152725"

    def test_integer_input(self):
        assert normalise(8811065126).canonical == "0008811065126"

    def test_raw_is_preserved_for_audit(self):
        """
        ``raw`` keeps exactly what arrived, whitespace included, so an operator
        investigating a mismatch can see the original bytes. ``digits`` is the
        cleaned form and ``canonical`` is what gets used.
        """
        bc = normalise("  8811065126 ")
        assert bc.raw == "  8811065126 "     # untouched, for the audit trail
        assert bc.digits == "8811065126"     # cleaned
        assert bc.canonical == "0008811065126"  # padded, used to build the SKU


# ===========================================================================
# Check digits
# ===========================================================================

class TestCheckDigit:
    """
    The check digit is what proves the vendor is stripping zeros rather than
    sending genuinely short codes: 99% of the 11-digit values become valid
    once a zero is restored, and 0% are valid as sent.
    """

    @pytest.mark.parametrize(
        "code",
        [
            "008811060626",   # MCA, Belinda Carlisle
            "008138010236",   # Smart Travels Europe
            "5413356068320",  # Laurent Garnier, real EAN-13
        ],
    )
    def test_real_barcodes_validate(self, code):
        assert is_valid_gtin(code) is True

    def test_stripped_form_does_not_validate(self):
        """This is the measured evidence for the whole padding rule."""
        assert is_valid_gtin("8811060626") is False   # as the vendor sends it
        assert is_valid_gtin("008811060626") is True  # once restored

    def test_check_digit_computation(self):
        assert gtin_check_digit("00881106062") == "6"
        assert gtin_check_digit("541335606832") == "0"

    def test_non_numeric_returns_none(self):
        assert gtin_check_digit("abc") is None
        assert gtin_check_digit("") is None

    def test_invalid_checksum_does_not_block_use(self):
        """
        A failed checksum lowers confidence but must not discard the product.
        Some live Amazon listings hold barcodes that fail the check digit, and
        refusing to match them would orphan real, selling products.
        """
        bc = normalise("1234567890123")  # valid length, wrong check digit
        assert bc.usable is True
        assert bc.checksum_ok is False


# ===========================================================================
# Candidate ordering
# ===========================================================================

class TestMatchCandidates:
    def test_canonical_form_is_tried_first(self):
        cands = match_candidates("8811060626")
        assert cands[0] == "0008811060626"

    def test_includes_twelve_and_fourteen_digit_forms(self):
        cands = match_candidates("8811060626")
        assert "008811060626" in cands   # UPC-A width
        assert "00008811060626" in cands  # GTIN-14 width

    def test_includes_the_raw_form(self):
        assert "8811060626" in match_candidates("8811060626")

    def test_no_duplicates_and_order_preserved(self):
        cands = match_candidates("3341348053448")  # already 13 wide
        assert len(cands) == len(set(cands))
        assert cands[0] == "3341348053448"

    def test_candidate_skus_all_carry_the_prefix(self):
        for s in candidate_skus("HA-AMS-", "8811060626"):
            assert s.startswith("HA-AMS-")


# ===========================================================================
# Reading a SKU backwards
# ===========================================================================

class TestSplitSku:
    """
    This account's All Listings Report has NO product-id column -- only
    seller-sku, asin1, price, quantity and status. So the barcode has to be
    recovered from inside the SKU. These are all real SKU shapes from the live
    report.
    """

    @pytest.mark.parametrize(
        ("sku", "prefix", "digits"),
        [
            ("HA-AMS-0008811065126", "HA-AMS-", "0008811065126"),
            ("HA-INGR-9798385266500", "HA-INGR-", "9798385266500"),
            ("0061297805198", "", "0061297805198"),           # bare barcode SKU
            ("RA-OLD-013431496021", "RA-OLD-", "013431496021"),
            ("HB-WOB-1234567890123-V.G", "HB-WOB-", "1234567890123"),  # condition suffix
            ("RA-THB-1234567890123-LN", "RA-THB-", "1234567890123"),
        ],
    )
    def test_real_sku_shapes(self, sku, prefix, digits):
        assert split_sku(sku) == (prefix, digits)

    def test_suffixed_sku_with_trailing_letter(self):
        # 1,920 live listings look like HA-INGR-<13 digits>X
        p, d = split_sku("HA-INGR-9798385266500X")
        assert p == "HA-INGR-"
        assert d == "9798385266500"

    def test_prefix_helper(self):
        assert sku_prefix("HA-AMS-0008811065126") == "HA-AMS-"
        assert sku_prefix("HA-INGR-9798385266500") == "HA-INGR-"

    @pytest.mark.parametrize("sku", REAL_MALFORMED_SKUS)
    def test_malformed_skus_do_not_crash(self, sku):
        """Real data-entry accidents found live. Must degrade, not explode."""
        prefix, digits = split_sku(sku)
        assert isinstance(prefix, str)
        assert isinstance(digits, str)

    def test_no_digits_at_all(self):
        assert split_sku("BROKEN-SKU") == ("BROKEN-SKU", "")
        assert split_sku("") == ("", "")

    def test_round_trip_through_barcode(self):
        for sku in REAL_AMS_SKUS:
            bc = barcode_from_sku(sku)
            assert bc.usable
            assert build_sku("HA-AMS-", bc.canonical) == sku


# ===========================================================================
# The dataclass contract
# ===========================================================================

def test_barcode_is_immutable_and_hashable():
    """
    Frozen so a Barcode can be a dict key in the mapping engine's index, and
    so nothing downstream can mutate a canonical value after validation.
    """
    bc = normalise("8811060626")
    assert isinstance(bc, Barcode)
    with pytest.raises((AttributeError, TypeError)):
        bc.canonical = "nope"  # type: ignore[misc]
    assert hash(bc) is not None
