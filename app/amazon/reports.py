"""
Downloading Amazon's own view of the catalogue.

WHY THIS IS THE MOST IMPORTANT READ IN THE SYSTEM
=================================================
The All Listings Report is what makes the whole design work. It supplies:

  * **the real SKUs** -- so we stop guessing. The client's spreadsheet formula
    is correct, but "correct formula" and "the SKU exists" are different
    claims, and only Amazon can settle the second one.
  * **Amazon's current quantity** -- so the decision engine can compare what
    the quantity *should* be against what Amazon *actually shows*, rather than
    against yesterday's feed. That is what makes a failed push self-healing
    instead of permanently invisible.
  * **listing status** -- 79,802 Active, 15,768 Inactive, 440 Incomplete on
    this account. Only Active listings are worth writing to.

THE REPORT THIS ACCOUNT ACTUALLY PRODUCES
=========================================
Verified against All+Listings+Report_09-04-2026.txt:

  * tab-delimited text, UTF-8 **with a byte-order mark** -- the BOM attaches
    itself to the first column name and turns ``seller-sku`` into
    ``\\ufeffseller-sku``, which silently breaks a naive ``DictReader``. Read
    with ``utf-8-sig``.
  * 96,010 rows
  * only five columns::

        seller-sku    asin1    price    quantity    status

Note what is **missing**: no ``product-id`` and no ``fulfillment-channel``.
The classic advice ("match on product-id, filter on fulfillment-channel")
simply cannot be followed here. So:

  * the barcode is recovered from inside the SKU
    (:func:`app.core.barcode.split_sku`), which works because every SKU on this
    account embeds a 13-digit barcode;
  * the fulfilment channel is filled in when a listing is read individually
    through the Listings API, and defaults to merchant-fulfilled, which is what
    this whole account uses.

REPORT LIFECYCLE
================
Reports are asynchronous, and deliberately slow to request -- Amazon allows one
creation per minute:

    POST /reports/2021-06-30/reports            -> reportId
    GET  /reports/2021-06-30/reports/{id}       -> poll until DONE
    GET  /reports/2021-06-30/documents/{docId}  -> a presigned URL
    GET  <that url>                             -> the bytes, possibly gzipped

A full listings report on this account takes a few minutes.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.amazon.client import SpApiClient, SpApiError
from app.core.barcode import normalise, split_sku

log = logging.getLogger(__name__)

#: Every listing, active and inactive. The inactive ones matter: a SKU we think
#: is missing might merely be inactive, and that is a different problem with a
#: different fix.
REPORT_ALL_LISTINGS = "GET_MERCHANT_LISTINGS_ALL_DATA"

#: Active listings only. Smaller and faster; used when a quick refresh is
#: enough.
REPORT_ACTIVE_LISTINGS = "GET_MERCHANT_LISTINGS_DATA"

_REPORTS = "/reports/2021-06-30/reports"
_DOCUMENTS = "/reports/2021-06-30/documents"

#: Terminal states. Anything else means keep polling.
_DONE = "DONE"
_TERMINAL = {"DONE", "CANCELLED", "FATAL"}

#: Poll every 15s for up to 20 minutes. A full report on this account has
#: reliably been ready inside 5.
POLL_INTERVAL_SECONDS = 15
POLL_TIMEOUT_SECONDS = 20 * 60


class ReportError(RuntimeError):
    """A report could not be requested, waited for, or read."""


@dataclass(slots=True)
class ListingRecord:
    """One row of the All Listings Report, cleaned up."""

    seller_sku: str
    sku_prefix: str
    #: Canonical 13-digit barcode recovered from the SKU. Empty when the SKU
    #: contains no usable digit block.
    barcode: str
    asin: str | None
    quantity: int | None
    price: float | None
    status: str | None

    @property
    def is_active(self) -> bool:
        return (self.status or "").strip().lower() == "active"


@dataclass(slots=True)
class ReportParseStats:
    """Counters, for the run summary and for the coverage report."""

    total_rows: int = 0
    parsed_rows: int = 0
    no_barcode_rows: int = 0
    unreadable_quantity: int = 0
    by_prefix: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    observed_columns: list[str] = field(default_factory=list)


# ===========================================================================
# Requesting a report
# ===========================================================================

def request_report(
    client: SpApiClient,
    *,
    report_type: str = REPORT_ALL_LISTINGS,
    marketplace_id: str | None = None,
) -> str:
    """
    Ask Amazon to build a report. Returns the report id.

    Amazon permits roughly one creation per minute, which the rate limiter in
    :mod:`app.amazon.client` already enforces, so this will block rather than
    earn a 429 if called in a loop.
    """
    market = marketplace_id or client.marketplace_id
    body = {"reportType": report_type, "marketplaceIds": [market]}

    try:
        response = client.post(_REPORTS, operation="reports.create", json_body=body)
    except SpApiError as exc:
        raise ReportError(
            f"Could not ask Amazon for the {report_type} report. {exc}"
        ) from exc

    report_id = response.json().get("reportId")
    if not report_id:
        raise ReportError(f"Amazon accepted the request but returned no report id: {response.text[:300]}")
    log.info("requested %s, report id %s", report_type, report_id)
    return report_id


def wait_for_report(
    client: SpApiClient,
    report_id: str,
    *,
    timeout_seconds: int = POLL_TIMEOUT_SECONDS,
    poll_interval: int = POLL_INTERVAL_SECONDS,
) -> str:
    """
    Poll until the report is ready. Returns the report **document** id.

    Raises :class:`ReportError` on CANCELLED, FATAL, or timeout. CANCELLED
    usually means there was no data to report; FATAL means Amazon failed to
    build it and it should simply be requested again next cycle.
    """
    deadline = time.monotonic() + timeout_seconds
    attempts = 0

    while True:
        attempts += 1
        try:
            response = client.get(f"{_REPORTS}/{report_id}", operation="reports.get")
        except SpApiError as exc:
            raise ReportError(f"Could not check the status of report {report_id}. {exc}") from exc

        body = response.json()
        status = str(body.get("processingStatus") or "").upper()

        if status == _DONE:
            document_id = body.get("reportDocumentId")
            if not document_id:
                raise ReportError(f"Report {report_id} is DONE but has no document id")
            log.info("report %s ready after %d polls", report_id, attempts)
            return document_id

        if status in _TERMINAL:
            raise ReportError(
                f"Amazon finished report {report_id} with status {status}. "
                + (
                    "CANCELLED normally means there was nothing to report. "
                    if status == "CANCELLED"
                    else "FATAL means Amazon could not build it; it will be requested again "
                    "on the next cycle. "
                )
                + "No catalogue refresh has happened, so the system is still using the "
                "previous snapshot."
            )

        if time.monotonic() > deadline:
            raise ReportError(
                f"Report {report_id} was still {status or 'unknown'} after "
                f"{timeout_seconds // 60} minutes. Giving up for now; it will be "
                "requested again on the next cycle. The system continues to use the "
                "previous catalogue snapshot, so nothing is broken - it is just stale."
            )

        time.sleep(poll_interval)


def download_report(
    client: SpApiClient,
    document_id: str,
    *,
    save_to: Path | None = None,
) -> str:
    """
    Fetch a finished report's contents as text.

    Amazon hands back a presigned URL on its own storage; the download itself
    carries no authentication header, which is why it uses a bare httpx client
    rather than the SP-API client.

    ``compressionAlgorithm`` is ``GZIP`` for anything sizeable, so the body is
    decompressed when Amazon says it is compressed. Saving a copy is worth the
    disk: it is the snapshot that makes "restore the account to how it looked on
    Tuesday" possible.
    """
    try:
        response = client.get(f"{_DOCUMENTS}/{document_id}", operation="reports.document")
    except SpApiError as exc:
        raise ReportError(f"Could not get the download link for report document {document_id}. {exc}") from exc

    meta = response.json()
    url = meta.get("url")
    if not url:
        raise ReportError(f"Amazon returned no download URL for document {document_id}")
    compression = str(meta.get("compressionAlgorithm") or "").upper()

    try:
        # 5 minutes: the report can be tens of megabytes.
        with httpx.Client(timeout=300.0) as plain:
            payload = plain.get(url)
            payload.raise_for_status()
            raw = payload.content
    except httpx.HTTPError as exc:
        raise ReportError(f"Could not download the report from Amazon's storage: {exc}") from exc

    if compression == "GZIP":
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise ReportError(f"Amazon said the report was gzipped but it could not be decompressed: {exc}") from exc

    if save_to is not None:
        save_to.parent.mkdir(parents=True, exist_ok=True)
        save_to.write_bytes(raw)
        log.info("saved catalogue snapshot to %s (%s bytes)", save_to, f"{len(raw):,}")

    # utf-8-sig strips the byte-order mark that this account's report carries.
    # Without it the first column name becomes "﻿seller-sku" and every
    # lookup of "seller-sku" silently returns None.
    return raw.decode("utf-8-sig", errors="replace")


# ===========================================================================
# Parsing
# ===========================================================================

def parse_listings_report(text: str) -> tuple[list[ListingRecord], ReportParseStats]:
    """
    Parse the All Listings Report into records.

    Tolerant about which columns exist, because Amazon's report shape varies
    between accounts and over time. This account supplies only five columns;
    others include ``product-id`` and ``fulfillment-channel``, and those are
    used when present.

    >>> report = "seller-sku\\tasin1\\tprice\\tquantity\\tstatus\\n"
    >>> report += "HA-AMS-0008811065126\\tB0CM198LCS\\t32.86\\t1\\tActive\\n"
    >>> report += "HA-INGR-9798385266500\\tB0FR1SD7SN\\t37.4\\t5\\tActive\\n"
    >>> records, stats = parse_listings_report(report)
    >>> len(records)
    2
    >>> records[0].seller_sku, records[0].sku_prefix, records[0].barcode
    ('HA-AMS-0008811065126', 'HA-AMS-', '0008811065126')
    >>> records[0].quantity, records[0].is_active
    (1, True)
    >>> stats.by_prefix['HA-AMS-']
    1
    """
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    columns = [c.strip().lstrip("﻿") for c in (reader.fieldnames or [])]

    stats = ReportParseStats(observed_columns=columns)
    if not columns:
        raise ReportError("The report is empty - it has no header row.")

    lower = {c.lower(): c for c in columns}

    def col(*names: str) -> str | None:
        for n in names:
            if n in lower:
                return lower[n]
        return None

    c_sku = col("seller-sku", "sku", "seller_sku")
    c_asin = col("asin1", "asin", "asin-1")
    c_qty = col("quantity", "afn-fulfillable-quantity")
    c_price = col("price")
    c_status = col("status", "listing-status")
    # Present on some accounts, absent on this one. Used if available.
    c_pid = col("product-id", "product_id")
    c_channel = col("fulfillment-channel", "fulfilment-channel")

    if not c_sku:
        raise ReportError(
            "The report has no seller-sku column, so it cannot be used. "
            f"Columns found: {columns}. Make sure the report requested was "
            "'All Listings Report' rather than a different inventory report."
        )

    records: list[ListingRecord] = []

    for raw in reader:
        stats.total_rows += 1

        # DictReader keys carry the BOM-stripped names only if the header was
        # decoded with utf-8-sig upstream; guard anyway.
        sku = (raw.get(c_sku) or "").strip()
        if not sku:
            continue

        prefix, digits = split_sku(sku)

        # Prefer an explicit product-id when the report has one; otherwise fall
        # back to the digits inside the SKU, which is all this account offers.
        barcode = ""
        if c_pid:
            pid = (raw.get(c_pid) or "").strip()
            if pid:
                bc = normalise(pid)
                barcode = bc.canonical if bc.usable else ""
        if not barcode and digits:
            bc = normalise(digits)
            barcode = bc.canonical if bc.usable else ""
        if not barcode:
            stats.no_barcode_rows += 1

        quantity: int | None = None
        if c_qty:
            q = (raw.get(c_qty) or "").strip()
            if q:
                try:
                    quantity = int(float(q))
                except ValueError:
                    stats.unreadable_quantity += 1

        price: float | None = None
        if c_price:
            p = (raw.get(c_price) or "").strip().replace("$", "").replace(",", "")
            if p:
                try:
                    price = float(p)
                except ValueError:
                    price = None

        status = (raw.get(c_status) or "").strip() if c_status else None
        channel = (raw.get(c_channel) or "").strip() if c_channel else None

        records.append(
            ListingRecord(
                seller_sku=sku,
                sku_prefix=prefix,
                barcode=barcode,
                asin=(raw.get(c_asin) or "").strip() or None if c_asin else None,
                quantity=quantity,
                price=price,
                status=status,
            )
        )
        stats.parsed_rows += 1
        stats.by_prefix[prefix] = stats.by_prefix.get(prefix, 0) + 1
        key = status or "(none)"
        stats.by_status[key] = stats.by_status.get(key, 0) + 1

        # Channel is recorded on the model by the caller; kept here so the
        # column is used when an account does provide it.
        if channel:
            setattr(records[-1], "_fulfillment_channel", channel)  # noqa: B010

    log.info(
        "parsed catalogue report: %d rows, %d prefixes, statuses %s",
        stats.parsed_rows, len(stats.by_prefix), stats.by_status,
    )
    return records, stats


# ===========================================================================
# One-call convenience
# ===========================================================================

def fetch_all_listings(
    client: SpApiClient,
    *,
    report_type: str = REPORT_ALL_LISTINGS,
    snapshot_dir: Path | None = None,
) -> tuple[list[ListingRecord], ReportParseStats, dict]:
    """
    Request, wait for, download and parse the catalogue in one call.

    Returns ``(records, stats, metadata)``. ``metadata`` carries the report and
    document ids plus the snapshot path, all of which go on the
    :class:`app.models.CatalogSync` row for the audit trail.

    Takes several minutes by design -- Amazon's report pipeline is
    asynchronous. Called once a day by the scheduler, and on demand from the
    dashboard.
    """
    started = datetime.now(UTC)
    report_id = request_report(client, report_type=report_type)
    document_id = wait_for_report(client, report_id)

    snapshot_path: Path | None = None
    if snapshot_dir is not None:
        stamp = started.strftime("%Y%m%d-%H%M%S")
        snapshot_path = snapshot_dir / f"catalog-{stamp}.tsv"

    text = download_report(client, document_id, save_to=snapshot_path)
    records, stats = parse_listings_report(text)

    return (
        records,
        stats,
        {
            "report_id": report_id,
            "report_document_id": document_id,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "requested_at": started.isoformat(),
            "report_type": report_type,
        },
    )


def parse_local_report(path: Path) -> tuple[list[ListingRecord], ReportParseStats]:
    """
    Parse a report file already on disk.

    Used by the Stage 0 coverage script, which runs against the report the
    client emailed over -- so the mapping can be measured before a single
    Amazon credential is configured.
    """
    return parse_listings_report(path.read_text(encoding="utf-8-sig", errors="replace"))
