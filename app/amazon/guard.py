"""
The payload guard: the last thing between this system and the client's listings.

WHY THIS FILE EXISTS
====================
The client was explicit: this system updates stock quantity and must never
touch prices, because they set prices themselves after adding shipping, tax and
margin. Their own filled-in Price & Quantity template proves how easy the
mistake would be -- it carries both quantity AND price in adjacent columns::

    A7='HA-AMS-0634457195035' | C7='1' | G7='15.99'
                                 ^qty        ^price

Column C is ours. Column G is emphatically not.

A promise like that cannot live in a setting, because a setting can be clicked
off by accident or flipped by a bug in the settings page. So it is enforced
here, structurally:

  * every outbound payload passes through :func:`assert_quantity_only`
  * that function walks the payload recursively and raises on any price-shaped
    key, whatever the nesting or spelling
  * the check runs at the transport boundary, so there is no way to send a
    request that skipped it

Defence in depth, three independent layers:

  1. The Amazon app is not granted the ``Pricing`` role, so Amazon itself would
     refuse a price change. (As of 2026-09-05 the app DOES have this role
     ticked and it should be removed -- see docs/AMAZON-APP-SETUP.md.)
  2. The builders in :mod:`app.amazon.listings` and :mod:`app.amazon.feeds`
     only ever construct a ``fulfillment_availability`` patch.
  3. This guard, which refuses to transmit regardless of what the builders did.

All three would have to fail simultaneously.

THE ATTRIBUTE NAMES ARE REAL
============================
Taken from the attribute row of the client's own template, so these are the
exact strings Amazon uses on this account:

  ours   ``fulfillment_availability#1.quantity``
  ours   ``fulfillment_availability#1.fulfillment_channel_code``
  ours   ``fulfillment_availability#1.lead_time_to_ship_max_days``
  THEIRS ``purchasable_offer[marketplace_id=ATVPDKIKX0DER][audience=ALL]#1.our_price#1.schedule#1.value_with_tax``
  THEIRS ``...minimum_seller_allowed_price...``
  THEIRS ``...maximum_seller_allowed_price...``
  THEIRS ``...discounted_price...``
  THEIRS ``...automated_pricing_merchandising_rule_plan...``
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)


class PriceFieldRefused(RuntimeError):
    """
    A payload contained something price-shaped and was not sent.

    This is a programming error, never a data error, and it is deliberately
    fatal: the run stops, the operator is alerted, and nothing is transmitted.
    Downgrading this to a warning would defeat the purpose.
    """


#: Substrings that mark a field as price-related. Matched case-insensitively
#: against every key anywhere in the payload.
#:
#: Deliberately broad. A false positive costs one confused developer five
#: minutes; a false negative silently overwrites a price the client spent real
#: effort calculating and may not notice for weeks.
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "price",              # our_price, discounted_price, list_price, msrp_price
    "pricing",            # automated_pricing_merchandising_rule_plan
    "currency",           # only ever appears alongside a monetary value
    "value_with_tax",     # the leaf where Amazon actually stores the amount
    "value_without_tax",
    "our_price",
    "discount",           # quantity_discount_plan, discounted_price
    "merchandising_rule", # attaches an automated repricing rule
    "msrp",
    "cost",
    "amount",
    "money",
    "tax",                # tax rate / tax code: the client's accountant owns these
    "business_price",
    "sale_price",
    "map",                # minimum advertised price
)

#: Keys that contain a forbidden substring but are genuinely ours and safe.
#: Kept explicit and tiny -- every entry is a hole in the net and must be
#: justified in a comment.
_ALLOWED_EXACT: frozenset[str] = frozenset({
    # Nothing today. If an Amazon attribute ever legitimately contains one of
    # the substrings above, add it here WITH a comment explaining why it is not
    # a price. Do not widen the substring list instead.
})

#: Fields this system is permitted to write. Anything outside this set is
#: reported by :func:`describe_payload_fields`, which the dry-run view shows to
#: the operator so they can see exactly what would be sent.
ALLOWED_WRITE_FIELDS: frozenset[str] = frozenset({
    "fulfillment_availability",
    "fulfillment_channel_code",
    "quantity",
    "lead_time_to_ship_max_days",
    "is_inventory_available",
    "restock_date",
    # structural, not data
    "productType",
    "patches",
    "op",
    "path",
    "value",
    "sku",
    "operationType",
    "messageId",
    "attributes",
    "header",
    "messages",
    "sellerId",
    "version",
    "issueLocale",
    "marketplaceIds",
})

_PRICE_KEY = re.compile("|".join(re.escape(s) for s in _FORBIDDEN_SUBSTRINGS), re.IGNORECASE)


def is_price_field(key: str) -> bool:
    """
    Whether ``key`` looks price-related.

    >>> is_price_field("quantity")
    False
    >>> is_price_field("fulfillment_availability")
    False
    >>> is_price_field("our_price")
    True
    >>> is_price_field("purchasable_offer[marketplace_id=ATVPDKIKX0DER][audience=ALL]#1.our_price#1.schedule#1.value_with_tax")
    True
    >>> is_price_field("minimum_seller_allowed_price")
    True
    >>> is_price_field("PRICE")
    True
    >>> is_price_field("automated_pricing_merchandising_rule_plan")
    True
    """
    if not key:
        return False
    if key in _ALLOWED_EXACT:
        return False
    return bool(_PRICE_KEY.search(key))


def assert_quantity_only(payload: Any, *, context: str = "") -> None:
    """
    Refuse to let a price-shaped field leave this process.

    Walks the whole structure -- dicts, lists, nested arbitrarily -- and checks
    every key. Also checks string *values*, because a flat-file feed encodes
    the attribute name in the data rather than in a key.

    Called from the transport layer, so nothing can be sent without passing
    through it.

    Parameters
    ----------
    context:
        Included in the error message so the operator knows which SKU or batch
        triggered it.

    Raises
    ------
    PriceFieldRefused

    Examples
    --------
    A legitimate quantity patch passes:

    >>> assert_quantity_only({
    ...     "productType": "PRODUCT",
    ...     "patches": [{
    ...         "op": "replace",
    ...         "path": "/attributes/fulfillment_availability",
    ...         "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": 7}],
    ...     }],
    ... })

    A payload carrying a price is refused, however deeply it is buried:

    >>> assert_quantity_only({"patches": [{"value": [{"our_price": 15.99}]}]})
    ... # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    PriceFieldRefused: refused

    And so is one that only names a price attribute as a string, which is how a
    flat-file feed carries an attribute path:

    >>> assert_quantity_only({"attribute": "purchasable_offer#1.our_price"})
    ... # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    PriceFieldRefused: refused

    The offending field is named in the message, so a developer can find it:

    >>> try:
    ...     assert_quantity_only({"patches": [{"value": [{"our_price": 15.99}]}]})
    ... except PriceFieldRefused as exc:
    ...     "our_price" in str(exc)
    True
    """
    offenders: list[str] = []
    _walk(payload, "", offenders)

    if offenders:
        where = f" while building {context}" if context else ""
        msg = (
            f"REFUSED to send this request{where}: it contains price-related "
            f"field(s) {offenders[:5]}. This system is only ever allowed to write "
            "stock quantity. Prices belong to the client, who sets them with their "
            "own shipping and margin. Nothing has been sent to Amazon. "
            "This is a bug in the payload builder, not a configuration problem -- "
            "see app/amazon/guard.py."
        )
        log.critical(msg)
        raise PriceFieldRefused(msg)


def _walk(node: Any, path: str, offenders: list[str]) -> None:
    """Recursive helper for :func:`assert_quantity_only`."""
    if isinstance(node, dict):
        for key, value in node.items():
            key_s = str(key)
            if is_price_field(key_s):
                offenders.append(f"{path}.{key_s}" if path else key_s)
            _walk(value, f"{path}.{key_s}" if path else key_s, offenders)

    elif isinstance(node, (list, tuple)):
        for i, item in enumerate(node):
            _walk(item, f"{path}[{i}]", offenders)

    # A flat-file or JSON feed can carry the attribute NAME as a value, e.g.
    # {"attribute": "...our_price..."}. Only strings long enough to look like an
    # attribute path are checked, so ordinary text such as a product title
    # containing the word "price" does not trip the guard.
    elif (
        isinstance(node, str)
        and len(node) > 12
        and ("#" in node or "_" in node or "[" in node)
        and is_price_field(node)
    ):
        offenders.append(f"{path}=<value naming a price attribute>")


def describe_payload_fields(payload: Any) -> set[str]:
    """
    Every field name appearing in a payload.

    Used by the dry-run view to show the operator exactly what a request would
    contain, and by the tests to assert that a builder produces nothing beyond
    :data:`ALLOWED_WRITE_FIELDS`.

    >>> sorted(describe_payload_fields({"productType": "PRODUCT", "patches": [{"op": "replace"}]}))
    ['op', 'patches', 'productType']
    """
    found: set[str] = set()

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                found.add(str(k))
                collect(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                collect(item)

    collect(payload)
    return found


def assert_only_expected_fields(payload: Any, *, context: str = "") -> None:
    """
    Belt and braces: warn if a payload contains a field we did not plan to send.

    Unlike :func:`assert_quantity_only` this does not raise -- Amazon adds
    structural keys over time and a hard failure on an unrecognised-but-harmless
    key would be a self-inflicted outage. It logs loudly instead, so an
    unexpected field is noticed in review rather than in production.
    """
    unexpected = describe_payload_fields(payload) - ALLOWED_WRITE_FIELDS
    if unexpected:
        log.warning(
            "payload for %s contains unplanned field(s) %s. Not blocking, but "
            "check that this is intended.",
            context or "an Amazon request",
            sorted(unexpected),
        )
