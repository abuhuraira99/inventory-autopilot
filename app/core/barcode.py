"""
Barcode normalisation and SKU construction.

WHY THIS FILE IS THE MOST IMPORTANT FILE IN THE PROJECT
=======================================================
The vendor (All Media Supply) and Amazon disagree about how to write a barcode,
and getting that disagreement wrong silently breaks two thirds of the catalogue.

Proven against real production data on 2026-09-04:

  * The vendor's feed strips leading zeros. Of 1,150,544 rows in
    FULL_FEED_110708_20260901, exactly ONE barcode started with "0".
    Length distribution: 13 digits (649,985), 12 (410,763), 11 (88,743),
    10 (654), 9 (242), plus ~150 rows of junk 1-8 digits long.
    An 11-digit "barcode" is not a valid GTIN at all -- but 99% of them become
    valid UPC-A once a leading zero is restored, which proves the vendor is
    stripping them rather than sending genuinely short codes.

  * Amazon's SKUs are ALWAYS zero-padded to 13 digits. Of 45,511 HA-AMS-
    listings, 45,493 have a 13-digit barcode part and 29,832 (65.5%) of those
    begin with "0".

  * The client's Google Sheet already bridges the gap with
        H2 = TEXT(G2, "0000000000000")     <- pad to exactly 13
        E2 = "HA-AMS-" & H2
    which is exactly why the manual process works day to day.

  * The cost of getting it wrong, measured: building the SKU by naive
    concatenation ("HA-AMS-" + raw vendor barcode) finds only 15,572 of the
    45,511 real listings -- 34.2%. The other 29,701 (65.3%) would be silently
    skipped, because Amazon does not raise an error for a SKU that does not
    exist; it simply does nothing.

So: pad to 13, always. Everything in this module exists to make that rule
impossible to get wrong by accident, and to recover matches for the handful of
records that do not fit the 13-digit shape.

REFERENCES
----------
GTIN check digit: the last digit is a modulo-10 checksum over the preceding
digits, with alternating weights. We use it to tell "the vendor stripped a
zero" apart from "this is genuine rubbish", which is the difference between
recovering 88,743 products and discarding them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Amazon SKUs on this account pad the barcode to exactly this many digits.
#: Derived from the client's own spreadsheet formula TEXT(x, "0000000000000")
#: and confirmed against 45,493 of 45,511 live HA-AMS- listings.
CANONICAL_WIDTH = 13

#: Anything shorter than this is not a recoverable barcode. The shortest real
#: retail barcode in use is EAN-8; the feed also contains a few dozen rows with
#: 1-7 characters which are data-entry rubbish and must never reach Amazon.
MIN_PLAUSIBLE_LENGTH = 8

#: GTIN-14 exists (shipping containers). 18 of the 45,511 live listings have a
#: 14-digit barcode part, so we tolerate it as a match candidate.
MAX_PLAUSIBLE_LENGTH = 14

_NON_DIGIT = re.compile(r"\D")

#: Pulls the digit block off the end of a SKU, e.g.
#: "HA-AMS-0008811065126" -> "0008811065126".
#: Anchored at the end so suffixed SKUs like "HA-INGR-9798385266500X" and
#: "RA-THB-1234567890123-LN" do not produce a false digit run.
_SKU_TRAILING_DIGITS = re.compile(
    rf"(\d{{{MIN_PLAUSIBLE_LENGTH},{MAX_PLAUSIBLE_LENGTH}}})$"
)

#: Fallback for SKUs where the digits are not at the very end (e.g. a "-LN" or
#: "-V.G" condition suffix). Takes the LONGEST digit run anywhere in the SKU.
_SKU_ANY_DIGITS = re.compile(r"\d+")


# ---------------------------------------------------------------------------
# Check digit
# ---------------------------------------------------------------------------

def gtin_check_digit(body: str) -> str | None:
    """
    Compute the GTIN check digit for ``body`` (the code WITHOUT its final digit).

    The weighting alternates 3,1,3,1... counted from the RIGHTMOST body digit
    leftwards. That single rule covers UPC-A (12), EAN-13 and GTIN-14, which is
    why we do not need three separate implementations.

    Returns ``None`` if ``body`` is not all digits.

    >>> gtin_check_digit("00881106062")     # Belinda Carlisle, MCA
    '6'
    >>> gtin_check_digit("541335606832")    # Laurent Garnier, EAN-13
    '0'
    """
    if not body or not body.isdigit():
        return None
    total = 0
    # enumerate from the right so the weight pattern is independent of length
    for i, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return str((10 - total % 10) % 10)


def is_valid_gtin(code: str) -> bool:
    """
    True if ``code`` is a well-formed GTIN-8/12/13/14 including its check digit.

    Used to decide whether a short vendor barcode is "a stripped zero" (valid
    once padded) or "rubbish" (never valid). We deliberately do NOT reject
    invalid codes outright -- Amazon holds some listings whose barcode fails the
    checksum, and refusing to match them would orphan real products. Validity
    is a *ranking signal*, not a gate. See :func:`normalise`.
    """
    if not code or not code.isdigit():
        return False
    if len(code) not in (8, 12, 13, 14):
        return False
    return gtin_check_digit(code[:-1]) == code[-1]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Barcode:
    """
    A vendor barcode after cleaning, with everything downstream code needs.

    Attributes
    ----------
    raw:
        Exactly what the vendor sent, untouched. Kept for audit trails so an
        operator can always see the original.
    digits:
        ``raw`` with every non-digit character removed and surrounding
        whitespace gone. Empty if there were no digits at all.
    canonical:
        ``digits`` zero-padded to :data:`CANONICAL_WIDTH`. **This is the value
        that gets concatenated with the SKU prefix.** Empty when the barcode is
        not usable.
    usable:
        False for rubbish (too short, no digits, or too long to be a GTIN).
        Unusable barcodes are logged to the rejects report and never pushed.
    checksum_ok:
        Whether :attr:`canonical` passes the GTIN check digit. Informational --
        a False here does not block a push, it only lowers match confidence.
    """

    raw: str
    digits: str
    canonical: str
    usable: bool
    checksum_ok: bool

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.canonical or self.raw


def normalise(raw: str | int | None) -> Barcode:
    """
    Clean one vendor barcode and produce its canonical 13-digit form.

    This is the single entry point for every barcode that enters the system.
    Nothing else in the codebase is allowed to zero-pad by hand -- if you find
    yourself writing ``zfill(13)`` outside this module, use this instead, so
    the rule stays in one place.

    Handles, in order:
      1. ``None`` / blank            -> unusable
      2. Numbers that arrived as floats from a spreadsheet, e.g. 1.2414152725E10
         (the client's own sheet has these in column G) -> rendered as an
         integer string rather than scientific notation
      3. Stray characters: spaces, hyphens, non-breaking spaces, quotes
      4. Junk shorter than :data:`MIN_PLAUSIBLE_LENGTH` -> unusable
      5. Anything longer than :data:`MAX_PLAUSIBLE_LENGTH` -> unusable
      6. Everything else -> zero-padded to 13

    >>> normalise("8811060626").canonical      # vendor strips zeros
    '0008811060626'
    >>> normalise("5413356068320").canonical   # already 13, untouched
    '5413356068320'
    >>> normalise("  811576-034265 ").canonical
    '0811576034265'
    >>> normalise("123").usable                # junk
    False
    >>> normalise(None).usable
    False
    """
    if raw is None:
        return Barcode("", "", "", False, False)

    # Spreadsheet exports hand us floats. int() first so 1.2414152725E10
    # becomes "12414152725" and not "1.2414152725e+10".
    if isinstance(raw, float):
        text = str(int(raw)) if raw == int(raw) else str(raw)
    elif isinstance(raw, int):
        text = str(raw)
    else:
        text = str(raw)

    digits = _NON_DIGIT.sub("", text.strip())

    if not digits:
        return Barcode(text, "", "", False, False)

    if len(digits) < MIN_PLAUSIBLE_LENGTH or len(digits) > MAX_PLAUSIBLE_LENGTH:
        # Deliberately unusable. The full feed contains ~150 such rows per day
        # (lengths 1-7). They are surfaced in the rejects report so somebody can
        # ask the vendor about them, but they never reach Amazon.
        return Barcode(text, digits, "", False, False)

    canonical = digits.zfill(CANONICAL_WIDTH) if len(digits) <= CANONICAL_WIDTH else digits
    return Barcode(
        raw=text,
        digits=digits,
        canonical=canonical,
        usable=True,
        checksum_ok=is_valid_gtin(canonical),
    )


def match_candidates(raw: str | int | None) -> list[str]:
    """
    Every form of a barcode worth trying when looking for a match, best first.

    Used by the mapping engine when the canonical 13-digit form does not find a
    listing. Real catalogues are messy: the same physical product can appear on
    Amazon as a 12-digit UPC, a 13-digit EAN, or (18 times on this account) a
    14-digit GTIN. Trying an ordered list of forms recovers those without ever
    guessing.

    Order matters -- the first hit wins, so the most trustworthy form is first:

      1. canonical 13-digit (matches 99.5% of the AMS catalogue)
      2. the digits exactly as the vendor sent them
      3. 12-digit UPC form
      4. 14-digit GTIN form
      5. zero-stripped form (in case Amazon is the one that stripped)

    Duplicates are removed while preserving order.

    >>> match_candidates("8811060626")
    ['0008811060626', '8811060626', '008811060626', '00008811060626']
    """
    bc = normalise(raw)
    if not bc.digits:
        return []

    ordered: list[str] = []
    d = bc.digits

    if bc.canonical:
        ordered.append(bc.canonical)
    ordered.append(d)
    if len(d) <= 12:
        ordered.append(d.zfill(12))
    if len(d) <= 14:
        ordered.append(d.zfill(14))
    stripped = d.lstrip("0")
    if stripped and stripped != d:
        ordered.append(stripped)

    seen: set[str] = set()
    out: list[str] = []
    for c in ordered:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# SKU construction and decomposition
# ---------------------------------------------------------------------------

def build_sku(prefix: str, raw_barcode: str | int | None) -> str | None:
    """
    Build the Amazon SKU for a vendor barcode, the way the client's sheet does.

    ``prefix`` comes from settings (default ``"HA-AMS-"``), so a new supplier or
    a changed convention is a dashboard edit, not a release.

    Returns ``None`` when the barcode is unusable, which is the signal to skip
    the row rather than push a malformed SKU.

    >>> build_sku("HA-AMS-", "8811065126")
    'HA-AMS-0008811065126'
    >>> build_sku("HA-AMS-", "3341348053448")
    'HA-AMS-3341348053448'
    >>> build_sku("HA-AMS-", "bad")
    """
    bc = normalise(raw_barcode)
    if not bc.usable or not bc.canonical:
        return None
    return f"{prefix}{bc.canonical}"


def candidate_skus(prefix: str, raw_barcode: str | int | None) -> list[str]:
    """
    Every SKU worth trying for one vendor barcode under one prefix, best first.

    Mirrors :func:`match_candidates` but in SKU space, for the tier of the
    mapping engine that searches by constructed SKU rather than by barcode.
    """
    return [f"{prefix}{c}" for c in match_candidates(raw_barcode)]


def split_sku(sku: str) -> tuple[str, str]:
    """
    Split an Amazon SKU into ``(prefix, barcode_digits)``.

    Needed because the All Listings Report on this account does **not** include
    a product-id column -- the only place the barcode appears is inside the SKU
    itself. So the mapping engine works backwards from the SKU.

    Returns ``("", "")`` if no digit block can be found.

    >>> split_sku("HA-AMS-0008811065126")
    ('HA-AMS-', '0008811065126')
    >>> split_sku("RA-THB-1234567890123-LN")
    ('RA-THB-', '1234567890123')
    >>> split_sku("0061297805198")          # some SKUs are a bare barcode
    ('', '0061297805198')
    >>> split_sku("HA-INGR-9798385266500X")
    ('HA-INGR-', '9798385266500')
    """
    s = (sku or "").strip()
    if not s:
        return "", ""

    m = _SKU_TRAILING_DIGITS.search(s)
    if m:
        return s[: m.start()], m.group(1)

    # No digits at the end -- take the longest digit run anywhere. This catches
    # condition-suffixed SKUs such as "FA-THB-1234567890123-V.G".
    runs = _SKU_ANY_DIGITS.findall(s)
    if not runs:
        return s, ""
    best = max(runs, key=len)
    if len(best) < MIN_PLAUSIBLE_LENGTH:
        return s, ""
    idx = s.rfind(best)
    return s[:idx], best


def sku_prefix(sku: str) -> str:
    """
    The prefix part of a SKU, e.g. ``"HA-AMS-"``.

    This is how the system decides whether a listing is in scope. On this
    account the prefix identifies the supplier: HA-AMS- is All Media Supply
    (45,511 listings, 99.5% present in the AMS feed), HA-INGR- is Ingram
    (22,523 listings, 0% in the AMS feed), and so on. Touching a listing from
    the wrong prefix would corrupt another supplier's inventory, so scope is
    enforced on this value. See :mod:`app.engine.mapping`.
    """
    return split_sku(sku)[0]


def barcode_from_sku(sku: str) -> Barcode:
    """
    Recover the canonical barcode from an Amazon SKU.

    Convenience wrapper used when building the Amazon-side index: it gives back
    a :class:`Barcode` so the same normalisation rules apply in both directions.
    """
    return normalise(split_sku(sku)[1])


# ---------------------------------------------------------------------------
# Bulk helpers
# ---------------------------------------------------------------------------

def normalise_many(raws: Iterable[str | int | None]) -> Iterator[Barcode]:
    """Stream-normalise a large iterable without building an intermediate list.

    The full feed is 1.15 million rows; materialising them twice costs about a
    gigabyte of memory for no reason.
    """
    for r in raws:
        yield normalise(r)
