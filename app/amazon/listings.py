"""
Reading and writing a single listing's quantity.

THE EXACT PAYLOAD, AND HOW WE KNOW IT IS RIGHT
==============================================
Every value below was read out of the client's own Price & Quantity template
rather than guessed from documentation. The template's attribute row (row 5)
names the real attributes for this account::

    contribution_sku#1.value                              -> the SKU
    fulfillment_availability#1.fulfillment_channel_code   -> "DEFAULT"
    fulfillment_availability#1.quantity                   -> THE FIELD WE WRITE
    fulfillment_availability#1.lead_time_to_ship_max_days -> handling time
    fulfillment_availability#1.restock_date
    fulfillment_availability#1.is_inventory_available

and the template's settings blob carries, base64-encoded:

    ptds=UFJPRFVDVA==                        -> product type is "PRODUCT"
    AttributeDefaultValues={"product_type#1.value":"PRODUCT",
                            "record_action#1.value":"partial_update"}
    primaryMarketplaceId=amzn1.mp.o.ATVPDKIKX0DER   -> US marketplace
    fulfillment_channel_code aliases include
        "Fulfillment by Merchant (Default)": "DEFAULT"
        "Fulfillment by Amazon (NA)": "AMAZON_NA"

The client's filled-in template confirms it in practice::

    A7='HA-AMS-0634457195035' | B7='Fulfillment by Merchant (Default)' | C7='1'

So a quantity patch on this account is::

    PATCH /listings/2021-08-01/items/{sellerId}/{sku}?marketplaceIds=ATVPDKIKX0DER
    {
      "productType": "PRODUCT",
      "patches": [{
        "op": "replace",
        "path": "/attributes/fulfillment_availability",
        "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": 7}]
      }]
    }

THE TRAP: ``replace`` REPLACES THE WHOLE OBJECT
===============================================
``fulfillment_availability`` is an array of objects, and a JSON-Patch
``replace`` swaps the entire array. So a naive quantity patch would silently
**delete the seller's handling time** (``lead_time_to_ship_max_days``) and any
restock date, because those live inside the same object.

Losing a handling time is not cosmetic: Amazon falls back to a default, the
promised delivery date changes, and late-shipment metrics suffer -- on the very
account whose health we are trying to protect.

So :func:`build_quantity_patch` takes the existing values and sends them back
unchanged alongside the new quantity. :func:`read_listing` fetches them, and
they are cached on ``AmazonListing.lead_time_to_ship_days`` so the common path
needs no extra call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.amazon.client import SpApiClient, SpApiError
from app.amazon.guard import assert_quantity_only

log = logging.getLogger(__name__)

#: Amazon product type for this account. From the template's ``ptds`` value.
DEFAULT_PRODUCT_TYPE = "PRODUCT"

#: Merchant-fulfilled channel code. The client ships everything themselves --
#: they told us they avoid Amazon's shipping service because it is "difficult
#: and costly" -- so every listing we touch uses this.
MERCHANT_CHANNEL = "DEFAULT"

#: Channel codes meaning Amazon holds the stock. We must never write a quantity
#: for these: Amazon owns the number and the API would reject or, worse,
#: confuse the listing.
FBA_CHANNEL_PREFIXES = ("AMAZON",)

_API = "/listings/2021-08-01/items"


@dataclass(slots=True)
class ListingSnapshot:
    """What Amazon currently holds for one SKU."""

    seller_sku: str
    quantity: int | None
    fulfillment_channel: str | None
    lead_time_to_ship_days: int | None
    product_type: str
    restock_date: str | None
    is_inventory_available: bool | None
    #: Full attributes blob, kept only for debugging a surprising response.
    raw: dict[str, Any]

    @property
    def is_fba(self) -> bool:
        ch = (self.fulfillment_channel or "").upper()
        return any(ch.startswith(p) for p in FBA_CHANNEL_PREFIXES)


# ===========================================================================
# Building the payload
# ===========================================================================

def build_quantity_patch(
    quantity: int,
    *,
    product_type: str = DEFAULT_PRODUCT_TYPE,
    fulfillment_channel_code: str = MERCHANT_CHANNEL,
    lead_time_to_ship_days: int | None = None,
    restock_date: str | None = None,
) -> dict[str, Any]:
    """
    Build the JSON body for a quantity-only patch.

    ``lead_time_to_ship_days`` and ``restock_date`` should be whatever the
    listing already had. Passing them back is what stops the patch from wiping
    them -- see the module docstring.

    The result is validated by :func:`assert_quantity_only` here as well as at
    the transport layer, so a unit test of this function alone still proves the
    invariant.

    >>> p = build_quantity_patch(7)
    >>> p["productType"]
    'PRODUCT'
    >>> p["patches"][0]["path"]
    '/attributes/fulfillment_availability'
    >>> p["patches"][0]["value"][0]
    {'fulfillment_channel_code': 'DEFAULT', 'quantity': 7}

    Existing handling time is preserved rather than dropped:

    >>> p = build_quantity_patch(3, lead_time_to_ship_days=5)
    >>> p["patches"][0]["value"][0]
    {'fulfillment_channel_code': 'DEFAULT', 'quantity': 3, 'lead_time_to_ship_max_days': 5}

    Negative quantities are impossible by construction:

    >>> build_quantity_patch(-1)
    Traceback (most recent call last):
        ...
    ValueError: quantity must be zero or more, got -1
    """
    if quantity < 0:
        raise ValueError(f"quantity must be zero or more, got {quantity}")

    availability: dict[str, Any] = {
        "fulfillment_channel_code": fulfillment_channel_code,
        "quantity": int(quantity),
    }
    # Only include these when we actually know them. Sending an explicit null
    # would clear the value, which is the exact bug this is guarding against.
    if lead_time_to_ship_days is not None:
        availability["lead_time_to_ship_max_days"] = int(lead_time_to_ship_days)
    if restock_date:
        availability["restock_date"] = restock_date

    payload = {
        "productType": product_type,
        "patches": [
            {
                "op": "replace",
                "path": "/attributes/fulfillment_availability",
                "value": [availability],
            }
        ],
    }

    # Validate at the point of construction too, not only at the transport
    # boundary, so this function is provably safe in isolation.
    assert_quantity_only(payload, context="quantity patch")
    return payload


# ===========================================================================
# Read
# ===========================================================================

def read_listing(
    client: SpApiClient,
    seller_sku: str,
    *,
    seller_id: str | None = None,
    marketplace_id: str | None = None,
) -> ListingSnapshot | None:
    """
    Read one listing's current availability. ``None`` if the SKU does not exist.

    A missing SKU is returned as ``None`` rather than raised, because "this SKU
    is not on the account" is an expected mapping outcome, not an error. It is
    also precisely the silent failure that has been losing this client stock
    updates: a flat-file upload against a non-existent SKU reports success and
    does nothing.
    """
    seller = seller_id or client.seller_id
    market = marketplace_id or client.marketplace_id

    try:
        response = client.get(
            f"{_API}/{seller}/{_quote(seller_sku)}",
            operation="listings.get",
            params={
                "marketplaceIds": market,
                # Ask for the attributes and the offer summary. Without
                # includedData Amazon returns a bare summary with no quantity.
                "includedData": "attributes,offers,fulfillmentAvailability,productTypes",
            },
        )
    except SpApiError as exc:
        if exc.status == 404:
            log.debug("SKU %s does not exist on the account", seller_sku)
            return None
        raise

    return _parse_listing(seller_sku, response.json())


def _parse_listing(seller_sku: str, body: dict) -> ListingSnapshot:
    """
    Turn Amazon's listing response into a :class:`ListingSnapshot`.

    Written defensively: Amazon returns availability in more than one shape
    depending on which ``includedData`` values were requested and how the
    listing was created. We look in every plausible place rather than assuming
    one, because a missed handling time means a wiped handling time.
    """
    attributes = body.get("attributes") or {}

    # Preferred: the top-level fulfillmentAvailability list.
    availability = body.get("fulfillmentAvailability") or []
    # Fallback: the same data inside attributes.
    if not availability:
        availability = attributes.get("fulfillment_availability") or []

    first: dict[str, Any] = availability[0] if availability else {}

    # Amazon uses camelCase at the top level and snake_case inside attributes.
    quantity = _first_present(first, "quantity")
    channel = _first_present(first, "fulfillmentChannelCode", "fulfillment_channel_code")
    lead = _first_present(first, "leadTimeToShipMaxDays", "lead_time_to_ship_max_days")
    restock = _first_present(first, "restockDate", "restock_date")
    available = _first_present(first, "isInventoryAvailable", "is_inventory_available")

    product_types = body.get("productTypes") or []
    product_type = DEFAULT_PRODUCT_TYPE
    if isinstance(product_types, list) and product_types:
        product_type = product_types[0].get("productType") or DEFAULT_PRODUCT_TYPE
    elif isinstance(attributes.get("product_type"), list) and attributes["product_type"]:
        product_type = attributes["product_type"][0].get("value") or DEFAULT_PRODUCT_TYPE

    return ListingSnapshot(
        seller_sku=seller_sku,
        quantity=int(quantity) if quantity is not None else None,
        fulfillment_channel=str(channel) if channel else None,
        lead_time_to_ship_days=int(lead) if lead is not None else None,
        product_type=product_type,
        restock_date=str(restock) if restock else None,
        is_inventory_available=bool(available) if available is not None else None,
        raw=body,
    )


def _first_present(d: dict, *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


# ===========================================================================
# Write
# ===========================================================================

@dataclass(slots=True)
class PatchOutcome:
    """The result of one quantity patch."""

    seller_sku: str
    accepted: bool
    submission_id: str | None = None
    #: "ACCEPTED" | "INVALID" | "VALID" -- Amazon's own wording.
    status: str | None = None
    issues: list[dict] | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def blocking_issues(self) -> list[dict]:
        """Issues severe enough to mean the change did not take effect."""
        return [i for i in (self.issues or []) if str(i.get("severity", "")).upper() == "ERROR"]


def patch_quantity(
    client: SpApiClient,
    seller_sku: str,
    quantity: int,
    *,
    product_type: str = DEFAULT_PRODUCT_TYPE,
    fulfillment_channel_code: str = MERCHANT_CHANNEL,
    lead_time_to_ship_days: int | None = None,
    restock_date: str | None = None,
    seller_id: str | None = None,
    marketplace_id: str | None = None,
) -> PatchOutcome:
    """
    Set one listing's quantity. Writes nothing else, ever.

    Returns a :class:`PatchOutcome` instead of raising for per-SKU problems, so
    one bad SKU cannot abort a batch of five thousand. Systemic failures --
    authentication, permissions, network -- still raise, because continuing
    through those would produce thousands of identical errors.

    In practice mode (``client.dry_run``) the payload is built, validated and
    logged, and a synthetic acceptance is returned. Same code path, no side
    effect.
    """
    if quantity < 0:  # pragma: no cover - guarded in the builder too
        raise ValueError(f"refusing to send a negative quantity for {seller_sku}")

    payload = build_quantity_patch(
        quantity,
        product_type=product_type,
        fulfillment_channel_code=fulfillment_channel_code,
        lead_time_to_ship_days=lead_time_to_ship_days,
        restock_date=restock_date,
    )

    seller = seller_id or client.seller_id
    market = marketplace_id or client.marketplace_id

    try:
        response = client.patch(
            f"{_API}/{seller}/{_quote(seller_sku)}",
            operation="listings.patch",
            params={"marketplaceIds": market, "issueLocale": "en_US"},
            json_body=payload,
        )
    except SpApiError as exc:
        # Per-SKU failures are data, not exceptions. Record and move on.
        if exc.status in (400, 404):
            return PatchOutcome(
                seller_sku=seller_sku,
                accepted=False,
                error_code=exc.first_code or str(exc.status),
                error_message=exc.first_message or str(exc),
                issues=exc.errors,
            )
        raise  # 401/403/429/5xx are systemic; let the caller stop the run

    body = response.json()
    status = str(body.get("status") or "").upper()
    issues = body.get("issues") or []
    outcome = PatchOutcome(
        seller_sku=seller_sku,
        accepted=status in {"ACCEPTED", "VALID"},
        submission_id=body.get("submissionId"),
        status=status or None,
        issues=issues,
    )

    if outcome.blocking_issues:
        # Amazon can answer 200 with ACCEPTED and still attach blocking issues.
        # Treating that as success is how a change appears to work and does not.
        first = outcome.blocking_issues[0]
        outcome.accepted = False
        outcome.error_code = str(first.get("code", ""))
        outcome.error_message = str(first.get("message", ""))
        log.warning(
            "Amazon accepted the request for %s but reported a blocking issue: %s %s",
            seller_sku, outcome.error_code, outcome.error_message,
        )

    return outcome


# ===========================================================================
# Verification
# ===========================================================================

def verify_quantity(
    client: SpApiClient,
    seller_sku: str,
    expected: int,
    *,
    seller_id: str | None = None,
    marketplace_id: str | None = None,
) -> tuple[bool, int | None]:
    """
    Read the listing back and confirm the quantity actually changed.

    Returns ``(matched, actual)``. ``actual`` is None if the SKU vanished.

    This closes the loop that the manual process never had. Amazon's listing
    updates are eventually consistent, so a mismatch immediately after a push
    is not proof of failure -- the caller should allow a short delay and retry
    once before reporting a problem. See :mod:`app.engine.pusher`.
    """
    snapshot = read_listing(
        client, seller_sku, seller_id=seller_id, marketplace_id=marketplace_id
    )
    if snapshot is None:
        return False, None
    return snapshot.quantity == expected, snapshot.quantity


def _quote(sku: str) -> str:
    """
    Percent-encode a SKU for use in a URL path.

    Necessary because real SKUs on this account contain characters that are not
    URL-safe: ``HB-WOB-1234567890123-V.G`` has a dot, and there are live SKUs
    with spaces and a leading colon (``": HA-INGR-9798385266500"``) from
    copy-paste accidents. ``safe=""`` encodes slashes too, which would
    otherwise change the request path entirely.
    """
    from urllib.parse import quote

    return quote(sku, safe="")
