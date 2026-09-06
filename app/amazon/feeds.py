"""
Bulk quantity updates through the Feeds API.

WHY BOTH THIS AND THE LISTINGS API
==================================
Two paths, two jobs:

  * :mod:`app.amazon.listings` patches one SKU per HTTP request. Feedback is
    immediate and per-SKU, which makes verification cheap. Right for the
    frequent delta runs, which are a few hundred products at most -- the twenty
    real delta files observed carried 23 to 320 rows each.

  * This module submits the whole batch as ONE document. Right for the daily
    full-feed reconcile. On this account the first reconcile has 38,341
    changes; at the client's 5,000-per-run limit that is 5,000 SKUs, which
    would be 5,000 HTTP requests at 2/second -- about 42 minutes of hammering
    Amazon. As a single feed it is one request.

THE CROSSOVER POINT is a setting (``feeds_threshold``), defaulting to 500.

THE FORMAT
==========
``JSON_LISTINGS_FEED`` rather than the older flat file, because it supports a
genuine partial update: only the attributes named in the message are touched.
The flat-file ``POST_FLAT_FILE_PRICEANDQUANTITYONLY_UPDATE_DATA`` route has a
column for price sitting next to the column for quantity, and a blank there has
historically been ambiguous. A format where price cannot be expressed at all is
strictly safer for a system that has promised never to touch prices.

The message shape, with every value taken from the client's own template rather
than from documentation (see :mod:`app.amazon.listings` for the derivation)::

    {
      "header": {"sellerId": "A1EXAMPLESELLER",
                 "version": "2.0",
                 "issueLocale": "en_US"},
      "messages": [
        {"messageId": 1,
         "sku": "HA-AMS-0008811065126",
         "operationType": "PARTIAL_UPDATE",
         "productType": "PRODUCT",
         "attributes": {
           "fulfillment_availability": [
             {"fulfillment_channel_code": "DEFAULT", "quantity": 7}
           ]
         }}
      ]
    }

THE LIFECYCLE
=============
    POST /feeds/2021-06-30/documents      -> upload URL + documentId
    PUT  <upload url>                      -> the JSON bytes (no auth header)
    POST /feeds/2021-06-30/feeds           -> feedId
    GET  /feeds/2021-06-30/feeds/{feedId}  -> poll until DONE
    GET  /feeds/2021-06-30/documents/{id}  -> the processing report

Amazon allows one feed submission every two minutes, which the rate limiter
already enforces.

THE PROCESSING REPORT MATTERS
=============================
A feed can report DONE while having rejected individual rows. The client's own
last manual upload is the proof: 656 SKUs processed, 603 successful, **53
"successful with other errors"**. Treating DONE as success would have hidden
all 53. :func:`parse_processing_report` extracts the per-SKU outcome so every
rejection is recorded against its own push item.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import httpx

from app.amazon.client import SpApiClient, SpApiError
from app.amazon.guard import assert_quantity_only
from app.amazon.listings import DEFAULT_PRODUCT_TYPE, MERCHANT_CHANNEL

log = logging.getLogger(__name__)

FEED_TYPE = "JSON_LISTINGS_FEED"
_CONTENT_TYPE = "application/json; charset=UTF-8"

_FEEDS = "/feeds/2021-06-30/feeds"
_FEED_DOCS = "/feeds/2021-06-30/documents"

_TERMINAL = {"DONE", "CANCELLED", "FATAL"}
POLL_INTERVAL_SECONDS = 20
POLL_TIMEOUT_SECONDS = 30 * 60


class FeedError(RuntimeError):
    """A feed could not be built, submitted, or read back."""


@dataclass(slots=True)
class FeedItem:
    """One SKU's change, as it will appear in the feed document."""

    seller_sku: str
    quantity: int
    product_type: str = DEFAULT_PRODUCT_TYPE
    fulfillment_channel_code: str = MERCHANT_CHANNEL
    #: Sent back unchanged so the patch does not wipe the seller's handling
    #: time. See the trap described in app/amazon/listings.py.
    lead_time_to_ship_days: int | None = None


@dataclass(slots=True)
class SkuOutcome:
    """One SKU's result, read out of the processing report."""

    seller_sku: str
    accepted: bool
    code: str | None = None
    message: str | None = None
    severity: str | None = None


@dataclass(slots=True)
class FeedResult:
    """The outcome of one feed submission."""

    feed_id: str
    document_id: str | None
    status: str
    submitted: int
    accepted: int
    rejected: int
    warnings: int
    outcomes: dict[str, SkuOutcome] = field(default_factory=dict)
    raw_summary: dict = field(default_factory=dict)

    @property
    def all_accepted(self) -> bool:
        return self.rejected == 0


# ===========================================================================
# Building the document
# ===========================================================================

def build_feed_document(items: list[FeedItem], *, seller_id: str) -> bytes:
    """
    Build the JSON_LISTINGS_FEED body.

    Every message is ``PARTIAL_UPDATE`` and names exactly one attribute, so
    nothing outside ``fulfillment_availability`` can be affected.

    The result is passed through :func:`assert_quantity_only` here as well as
    at the transport layer, so this function is provably safe on its own.

    >>> doc = build_feed_document(
    ...     [FeedItem("HA-AMS-0008811065126", 7),
    ...      FeedItem("HA-AMS-3341348053448", 0, lead_time_to_ship_days=5)],
    ...     seller_id="A1EXAMPLESELLER",
    ... )
    >>> body = json.loads(doc)
    >>> body["header"]["sellerId"]
    'A1EXAMPLESELLER'
    >>> len(body["messages"])
    2
    >>> body["messages"][0]["operationType"], body["messages"][0]["productType"]
    ('PARTIAL_UPDATE', 'PRODUCT')
    >>> body["messages"][0]["attributes"]["fulfillment_availability"][0]
    {'fulfillment_channel_code': 'DEFAULT', 'quantity': 7}
    >>> body["messages"][1]["attributes"]["fulfillment_availability"][0]["lead_time_to_ship_max_days"]
    5

    No message may contain anything but availability:

    >>> sorted(body["messages"][0]["attributes"])
    ['fulfillment_availability']
    """
    if not items:
        raise FeedError("refusing to submit an empty feed")
    if not seller_id:
        raise FeedError("a seller id is required to build a feed document")

    messages = []
    for i, item in enumerate(items, start=1):
        if item.quantity < 0:
            raise FeedError(f"refusing to send a negative quantity for {item.seller_sku}")

        availability: dict = {
            "fulfillment_channel_code": item.fulfillment_channel_code,
            "quantity": int(item.quantity),
        }
        if item.lead_time_to_ship_days is not None:
            availability["lead_time_to_ship_max_days"] = int(item.lead_time_to_ship_days)

        messages.append(
            {
                # 1-based and unique within the document. Amazon echoes it in
                # the processing report, which is how a rejection is tied back
                # to its SKU when the report omits the SKU itself.
                "messageId": i,
                "sku": item.seller_sku,
                "operationType": "PARTIAL_UPDATE",
                "productType": item.product_type,
                "attributes": {"fulfillment_availability": [availability]},
            }
        )

    document = {
        "header": {"sellerId": seller_id, "version": "2.0", "issueLocale": "en_US"},
        "messages": messages,
    }

    assert_quantity_only(document, context=f"feed document of {len(items)} SKUs")
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


# ===========================================================================
# Submitting
# ===========================================================================

def create_feed_document(client: SpApiClient) -> tuple[str, str]:
    """Reserve a document. Returns ``(document_id, upload_url)``."""
    try:
        response = client.post(
            _FEED_DOCS,
            operation="feeds.create",
            json_body={"contentType": _CONTENT_TYPE},
        )
    except SpApiError as exc:
        raise FeedError(f"Amazon would not accept a new feed document: {exc}") from exc

    body = response.json()
    document_id = body.get("feedDocumentId")
    url = body.get("url")
    if not document_id or not url:
        raise FeedError(f"Amazon's reply had no document id or upload URL: {response.text[:300]}")
    return document_id, url


def upload_feed_document(url: str, payload: bytes) -> None:
    """
    PUT the document to Amazon's storage.

    Uses a bare client: the presigned URL carries its own authorisation, and
    adding our access token would make Amazon reject the request.

    The content type must match what was declared when the document was
    reserved, byte for byte, or the upload is refused.
    """
    try:
        with httpx.Client(timeout=300.0) as plain:
            response = plain.put(url, content=payload, headers={"Content-Type": _CONTENT_TYPE})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FeedError(f"Could not upload the feed document to Amazon's storage: {exc}") from exc
    log.info("uploaded feed document (%s bytes)", f"{len(payload):,}")


def submit_feed(client: SpApiClient, document_id: str, *, marketplace_id: str | None = None) -> str:
    """Tell Amazon to process the uploaded document. Returns the feed id."""
    market = marketplace_id or client.marketplace_id
    try:
        response = client.post(
            _FEEDS,
            operation="feeds.create",
            json_body={
                "feedType": FEED_TYPE,
                "marketplaceIds": [market],
                "inputFeedDocumentId": document_id,
            },
        )
    except SpApiError as exc:
        raise FeedError(f"Amazon rejected the feed submission: {exc}") from exc

    feed_id = response.json().get("feedId")
    if not feed_id:
        raise FeedError(f"Amazon accepted the submission but returned no feed id: {response.text[:300]}")
    log.info("submitted feed %s (%s)", feed_id, FEED_TYPE)
    return feed_id


def wait_for_feed(
    client: SpApiClient,
    feed_id: str,
    *,
    timeout_seconds: int = POLL_TIMEOUT_SECONDS,
    poll_interval: int = POLL_INTERVAL_SECONDS,
) -> tuple[str, str | None]:
    """
    Poll until the feed finishes. Returns ``(status, result_document_id)``.

    A timeout is not treated as a failure of the push: Amazon has the document
    and will process it. The batch is left marked SENT-but-unverified, and the
    verification step on a later run reads the listings back and settles what
    actually happened. That is deliberately more robust than assuming either
    success or failure.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            response = client.get(f"{_FEEDS}/{feed_id}", operation="feeds.get")
        except SpApiError as exc:
            raise FeedError(f"Could not check the status of feed {feed_id}: {exc}") from exc

        body = response.json()
        status = str(body.get("processingStatus") or "").upper()

        if status in _TERMINAL:
            return status, body.get("resultFeedDocumentId")

        if time.monotonic() > deadline:
            log.warning(
                "feed %s still %s after %d minutes; leaving it to be verified later",
                feed_id, status, timeout_seconds // 60,
            )
            return status or "IN_PROGRESS", None

        time.sleep(poll_interval)


def download_feed_result(client: SpApiClient, document_id: str) -> str:
    """Fetch the processing report's text."""
    try:
        response = client.get(f"{_FEED_DOCS}/{document_id}", operation="feeds.document")
    except SpApiError as exc:
        raise FeedError(f"Could not get the processing report link: {exc}") from exc

    meta = response.json()
    url = meta.get("url")
    if not url:
        raise FeedError("Amazon returned no URL for the processing report")

    try:
        with httpx.Client(timeout=180.0) as plain:
            payload = plain.get(url)
            payload.raise_for_status()
            raw = payload.content
    except httpx.HTTPError as exc:
        raise FeedError(f"Could not download the processing report: {exc}") from exc

    if str(meta.get("compressionAlgorithm") or "").upper() == "GZIP":
        import gzip

        raw = gzip.decompress(raw)
    return raw.decode("utf-8-sig", errors="replace")


# ===========================================================================
# Reading the result
# ===========================================================================

def parse_processing_report(text: str, items: list[FeedItem]) -> tuple[dict[str, SkuOutcome], dict]:
    """
    Extract the per-SKU outcome from a JSON_LISTINGS_FEED processing report.

    Returns ``(outcomes_by_sku, summary)``.

    THIS IS WHY WE DO NOT TRUST "DONE". The client's last manual upload
    reported 656 processed, 603 successful and 53 "successful with other
    errors" -- the 53 were real failures wearing a success label. Codes seen on
    this account: 8684 (SKU linked to more than one GCID), 13013 (product not
    in the catalogue), 8560 (identifier not matched).

    The report's shape::

        {"header": {...},
         "summary": {"errors": 4, "warnings": 0,
                     "messagesProcessed": 656, "messagesAccepted": 603,
                     "messagesInvalid": 53},
         "issues": [{"messageId": "12", "code": "8684",
                     "severity": "ERROR", "message": "...", "attributeNames": [...]}]}

    Amazon reports only the problems, so every SKU starts out accepted and is
    demoted by an issue. That is the right default: a SKU absent from the
    issues list genuinely was accepted.

    >>> items = [FeedItem("HA-AMS-0074646623022", 5), FeedItem("HA-AMS-0008811065126", 3)]
    >>> report = json.dumps({
    ...     "summary": {"messagesProcessed": 2, "messagesAccepted": 1, "messagesInvalid": 1,
    ...                 "errors": 1, "warnings": 0},
    ...     "issues": [{"messageId": "1", "code": "8684", "severity": "ERROR",
    ...                 "message": "SKU is associated to more than 1 GCID"}],
    ... })
    >>> outcomes, summary = parse_processing_report(report, items)
    >>> outcomes["HA-AMS-0074646623022"].accepted, outcomes["HA-AMS-0074646623022"].code
    (False, '8684')
    >>> outcomes["HA-AMS-0008811065126"].accepted
    True
    >>> summary["messagesAccepted"]
    1
    """
    # messageId is 1-based and matches the order the document was built in.
    by_message_id = {str(i): item.seller_sku for i, item in enumerate(items, start=1)}

    outcomes: dict[str, SkuOutcome] = {
        item.seller_sku: SkuOutcome(seller_sku=item.seller_sku, accepted=True) for item in items
    }

    try:
        body = json.loads(text)
    except ValueError:
        # A non-JSON report means we cannot tell what happened per SKU. Rather
        # than guess, mark everything unverified and let the read-back settle
        # it -- claiming success here would be worse than admitting ignorance.
        log.error("feed processing report was not JSON; marking the batch unverified")
        for o in outcomes.values():
            o.accepted = False
            o.code = "UNPARSEABLE_REPORT"
            o.message = "Amazon's processing report could not be read; verifying by reading the listings back"
        return outcomes, {"parse_error": True, "raw_head": text[:500]}

    summary = body.get("summary") or {}

    for issue in body.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or "").upper()
        code = str(issue.get("code") or "")
        message = str(issue.get("message") or "")

        sku = issue.get("sku") or by_message_id.get(str(issue.get("messageId") or ""))
        if not sku or sku not in outcomes:
            # A document-level issue rather than a per-SKU one.
            log.warning("feed issue not tied to a SKU: %s %s %s", severity, code, message[:160])
            continue

        outcome = outcomes[sku]
        # Only an ERROR means the change did not take effect. WARNING and INFO
        # are advisory -- demoting on those would create phantom failures and a
        # retry storm.
        if severity == "ERROR":
            outcome.accepted = False
        if outcome.code is None or severity == "ERROR":
            outcome.code = code
            outcome.message = message
            outcome.severity = severity

    accepted = sum(1 for o in outcomes.values() if o.accepted)
    log.info(
        "processing report: %d submitted, %d accepted, %d rejected",
        len(outcomes), accepted, len(outcomes) - accepted,
    )
    return outcomes, summary


# ===========================================================================
# One call
# ===========================================================================

def push_batch_via_feed(
    client: SpApiClient,
    items: list[FeedItem],
    *,
    seller_id: str | None = None,
    marketplace_id: str | None = None,
    wait: bool = True,
) -> FeedResult:
    """
    Submit a batch and, if asked, wait for and parse the result.

    In practice mode the document is built and validated but never uploaded,
    and a synthetic all-accepted result is returned. Same code path, no side
    effect -- which is what makes a dry run a genuine rehearsal rather than a
    different program.
    """
    seller = seller_id or client.seller_id
    payload = build_feed_document(items, seller_id=seller)

    if client.dry_run:
        log.info("PRACTICE MODE: built a %s-SKU feed document (%s bytes) and did not send it",
                 len(items), f"{len(payload):,}")
        client.dry_run_payloads.append(
            {"method": "FEED", "feedType": FEED_TYPE, "skus": len(items), "bytes": len(payload)}
        )
        return FeedResult(
            feed_id="dry-run",
            document_id=None,
            status="DONE",
            submitted=len(items),
            accepted=len(items),
            rejected=0,
            warnings=0,
            outcomes={i.seller_sku: SkuOutcome(i.seller_sku, True) for i in items},
            raw_summary={"dryRun": True},
        )

    document_id, url = create_feed_document(client)
    upload_feed_document(url, payload)
    feed_id = submit_feed(client, document_id, marketplace_id=marketplace_id)

    if not wait:
        return FeedResult(
            feed_id=feed_id,
            document_id=None,
            status="IN_PROGRESS",
            submitted=len(items),
            accepted=0,
            rejected=0,
            warnings=0,
        )

    status, result_document_id = wait_for_feed(client, feed_id)

    if status != "DONE" or not result_document_id:
        return FeedResult(
            feed_id=feed_id,
            document_id=result_document_id,
            status=status,
            submitted=len(items),
            accepted=0,
            rejected=0,
            warnings=0,
            raw_summary={"note": f"feed finished with status {status}; will be verified by read-back"},
        )

    report_text = download_feed_result(client, result_document_id)
    outcomes, summary = parse_processing_report(report_text, items)
    accepted = sum(1 for o in outcomes.values() if o.accepted)

    return FeedResult(
        feed_id=feed_id,
        document_id=result_document_id,
        status=status,
        submitted=len(items),
        accepted=accepted,
        rejected=len(outcomes) - accepted,
        warnings=int(summary.get("warnings") or 0),
        outcomes=outcomes,
        raw_summary=summary,
    )
