"""
Parsing Amazon's All Listings Report.

WHY THIS FILE EXISTS
====================
The catalogue mirror was the least-rehearsed path in the system, and it had a
defect that no amount of running the test suite could ever have found: the
parser attached the fulfillment channel to each record with ``setattr``, and
``ListingRecord`` is a slots dataclass, so it has no ``__dict__`` and rejects
any attribute it did not declare.

Every catalogue refresh against a report carrying that column died with
``AttributeError``. The report the parser was developed against did not carry
one, so the branch was never entered: the line was written, linted,
type-checked, shipped, and had never once run. On the live account the column
is present, and the refresh failed every time -- silently, until the logging
was fixed.

So these tests parse a report **with** the column and a report **without** it.
The pair is the point. One of them alone is what let this through.
"""

from __future__ import annotations

from app.amazon.reports import ListingRecord, parse_listings_report

# Tab-separated, as Amazon delivers it.
_WITH_CHANNEL = (
    "seller-sku\tasin1\tprice\tquantity\tstatus\tfulfillment-channel\n"
    "HA-AMS-0008811065126\tB0BBFNPP5Y\t49.30\t4\tActive\tDEFAULT\n"
    "HA-AMS-5413356068320\tB01N5IB20Q\t12.12\t0\tActive\tAMAZON_NA\n"
)

_WITHOUT_CHANNEL = (
    "seller-sku\tasin1\tprice\tquantity\tstatus\n"
    "HA-AMS-0008811065126\tB0BBFNPP5Y\t49.30\t4\tActive\n"
)


def test_a_report_that_carries_the_fulfillment_channel_parses() -> None:
    """
    The exact failure from the live server.

    On the unfixed code this raises AttributeError: 'ListingRecord' object has
    no attribute '_fulfillment_channel' and no __dict__ for setting new
    attributes -- which is what took down every catalogue refresh.
    """
    records, stats = parse_listings_report(_WITH_CHANNEL)

    assert stats.parsed_rows == 2
    assert [r.fulfillment_channel for r in records] == ["DEFAULT", "AMAZON_NA"]


def test_a_report_without_that_column_still_parses() -> None:
    """
    The shape the parser was built against, which must keep working.

    The channel is None rather than a guess: absent is not the same as
    merchant-fulfilled, and the caller decides what to do about it.
    """
    records, stats = parse_listings_report(_WITHOUT_CHANNEL)

    assert stats.parsed_rows == 1
    assert records[0].fulfillment_channel is None


def test_the_channel_is_a_declared_field_and_not_bolted_on_afterwards() -> None:
    """
    Structural guard on the shape of the fix.

    ListingRecord is a slots dataclass on purpose -- a million of them are
    built during one full refresh, and slots keep that affordable. The cost is
    that nothing can be attached to an instance after the fact, which is not
    obvious at the call site and produced a line of code that could never work.

    Asserting the field exists and that instances still refuse unknown
    attributes stops the same trick being tried again with a different name.
    """
    assert "fulfillment_channel" in ListingRecord.__slots__

    record = ListingRecord(
        seller_sku="HA-AMS-1", sku_prefix="HA-AMS-", barcode="0000000000001",
        asin=None, quantity=1, price=None, status="Active",
    )
    assert record.fulfillment_channel is None

    try:
        record._something_new = "x"       # type: ignore[attr-defined]
    except AttributeError:
        pass
    else:
        raise AssertionError(
            "ListingRecord accepted a new attribute; slots have been lost, and "
            "with them the guarantee that made this bug findable at all"
        )
