"""
The Selling Partner API transport: rate limiting, retries, and the safety gate.

EVERY AMAZON CALL IN THIS SYSTEM GOES THROUGH HERE
==================================================
That is the point. It means three things are guaranteed rather than hoped for:

  1. **No payload containing a price can be sent.** :func:`request` calls
     :func:`app.amazon.guard.assert_quantity_only` before the socket is
     touched. There is no bypass, because there is no other transport.

  2. **Rate limits are respected.** Amazon publishes a token-bucket limit per
     operation. Exceeding it earns 429s and, if you keep going, a throttled or
     suspended application. A single shared limiter per operation makes that
     structurally difficult.

  3. **Nothing is logged that should not be.** Tokens are redacted at the
     logging layer (:class:`app.security.crypto.RedactingFilter`) and never
     written into a message here.

RATE LIMITS
===========
Amazon's documented steady-state rates for the operations this system uses.
They are conservative deliberately: this application shares the seller's quota
with anything else the client runs, and being a noisy neighbour on a live
account is not acceptable.

    PATCH  /listings/2021-08-01/items/...        5 req/s, burst 10
    GET    /listings/2021-08-01/items/...        5 req/s, burst 10
    POST   /reports/2021-06-30/reports          0.0167 req/s (1 per minute)
    GET    /reports/2021-06-30/reports/{id}     2 req/s
    POST   /feeds/2021-06-30/feeds              0.0083 req/s (1 per 2 minutes)
    GET    /feeds/2021-06-30/feeds/{id}         2 req/s

We run at a fraction of the ceiling (see :data:`RATE_LIMITS`) because the extra
throughput buys nothing -- a 5,000-SKU batch goes through the Feeds API in one
request, and the delta runs are a few hundred SKUs.

RETRIES
=======
Retried: 429 (throttled), 500/502/503/504 (Amazon's problem), network errors.
Not retried: 400 (our payload is wrong -- retrying sends the same bad payload),
403 (permissions -- a human must fix the app's roles), 404 (the SKU does not
exist -- that is a mapping problem, and it is recorded rather than hammered).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.amazon.guard import assert_only_expected_fields, assert_quantity_only
from app.amazon.lwa import AmazonAuthError, TokenProvider
from app.config import settings

log = logging.getLogger(__name__)


# ===========================================================================
# Rate limiting
# ===========================================================================

@dataclass
class _Bucket:
    """
    A token bucket, which is the shape Amazon's own limits take.

    ``rate`` tokens are added per second up to ``burst``. Each request removes
    one. When empty, callers wait. This mirrors Amazon's own accounting, so
    staying under our local limit keeps us under theirs.
    """

    rate: float
    burst: float
    tokens: float = field(init=False)
    updated: float = field(init=False)
    lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.tokens = self.burst
        self.updated = time.monotonic()

    def take(self, *, timeout: float = 120.0) -> float:
        """
        Consume one token, sleeping if necessary. Returns seconds waited.

        Raises :class:`TimeoutError` if a token cannot be had within
        ``timeout`` -- better to fail the run than to block the scheduler
        forever behind a mis-set rate.
        """
        waited = 0.0
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return waited
                shortfall = (1.0 - self.tokens) / self.rate
            if time.monotonic() + shortfall > deadline:
                raise TimeoutError(
                    f"waited {waited:.1f}s for rate-limit capacity and gave up; "
                    "the configured rate may be far below the work being attempted"
                )
            sleep_for = min(shortfall, 1.0)
            time.sleep(sleep_for)
            waited += sleep_for


#: Our self-imposed rates, keyed by a short operation name. Comfortably below
#: Amazon's published ceilings.
RATE_LIMITS: dict[str, tuple[float, float]] = {
    # operation:            (requests/second, burst)
    "listings.patch":       (2.0, 5.0),     # Amazon allows 5/s burst 10
    "listings.get":         (2.0, 5.0),
    "reports.create":       (0.0167, 1.0),  # 1 per minute
    "reports.get":          (1.0, 2.0),
    "reports.document":     (1.0, 2.0),
    "feeds.create":         (0.0083, 1.0),  # 1 per 2 minutes
    "feeds.get":            (1.0, 2.0),
    "feeds.document":       (1.0, 2.0),
    "default":              (1.0, 2.0),
}

_BUCKETS: dict[str, _Bucket] = {
    name: _Bucket(rate=r, burst=b) for name, (r, b) in RATE_LIMITS.items()
}


def _bucket_for(operation: str) -> _Bucket:
    return _BUCKETS.get(operation, _BUCKETS["default"])


# ===========================================================================
# Errors
# ===========================================================================

class SpApiError(RuntimeError):
    """
    An SP-API call failed.

    Carries the HTTP status and Amazon's own error list so the caller can
    decide whether the problem is per-SKU (record it and move on) or systemic
    (stop the run).
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        errors: list[dict] | None = None,
        operation: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.errors = errors or []
        self.operation = operation
        self.retryable = retryable

    @property
    def first_code(self) -> str | None:
        """Amazon's error code, e.g. "8684" or "InvalidInput"."""
        return str(self.errors[0].get("code")) if self.errors else None

    @property
    def first_message(self) -> str | None:
        return str(self.errors[0].get("message")) if self.errors else None


class SpApiPermissionError(SpApiError):
    """
    403. The app lacks the role needed for this operation.

    Given its own class because it is the single most likely thing to go wrong
    on first deployment: the app currently has ``Pricing`` and ``Inventory and
    Order Tracking`` ticked, but NOT ``Product Listing`` -- and ``Product
    Listing`` is the one that permits a quantity write. See
    docs/AMAZON-APP-SETUP.md.
    """


# ===========================================================================
# Client
# ===========================================================================

#: Retried, because they are transient and not our fault.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _http2_available() -> bool:
    """
    Whether the optional ``h2`` package is importable.

    Checked once, at import, rather than per client: it cannot change during a
    process's life, and a failed import is not free.
    """
    try:
        import h2  # noqa: F401  - probing for availability, not using it
    except ImportError:  # pragma: no cover - depends on the environment
        return False
    return True


_HTTP2_AVAILABLE = _http2_available()

#: Total attempts, including the first. Each failure backs off exponentially.
MAX_ATTEMPTS = 5


#: Operations that use a write verb but change NOTHING on the seller's account,
#: and so must still reach Amazon in practice mode.
#:
#: WHY THIS EXISTS
#: ===============
#: Practice mode intercepts writes by HTTP verb: POST, PUT, PATCH and DELETE.
#: That is the right default -- it is fail-safe, and it cannot be defeated by
#: forgetting to label a new operation.
#:
#: It also made practice mode useless. Asking Amazon to build the All Listings
#: Report is ``POST /reports/2021-06-30/reports``, because the request carries a
#: body, and it does not alter a single thing on the account: it asks Amazon to
#: describe the account back to us. Intercepted, it returned the synthetic
#: ``{"dryRun": true, "status": "ACCEPTED"}`` with no reportId, so every
#: catalogue refresh failed, so the Amazon side of the database stayed empty,
#: so nothing could ever be matched or compared. The dashboard said "Not
#: measured yet" and every run honestly reported "nothing to change".
#:
#: The client is told to start in practice mode and stay there until they trust
#: the system. They cannot build that trust in a mode where the comparison never
#: happens. Observed on the first real deployment: three failed refreshes, and
#: the reason only became visible once the dashboard started printing the body
#: of its own alerts.
#:
#: THE RULE FOR ADDING TO THIS SET
#: ==============================
#: An operation belongs here only if a successful call leaves the seller's
#: listings, prices, quantities and account settings exactly as they were. If
#: there is any doubt, it does not belong here. Nothing under ``listings.`` or
#: ``feeds.`` can ever qualify -- those are the two families that change the
#: account -- and a test enforces that, so this set cannot quietly grow into a
#: hole in practice mode.
#:
#: Note what is NOT relaxed: ``allow_write=True`` is still required from the
#: caller, and the price guard still inspects every body. This changes only
#: whether the request is actually sent while in practice mode.
READ_ONLY_WRITE_OPERATIONS = frozenset({"reports.create"})


class SpApiClient:
    """
    Rate-limited, retrying, price-refusing SP-API client.

    Deliberately synchronous. The pipeline is one job at a time on a worker
    thread; async would add complexity for throughput this workload does not
    need.
    """

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        endpoint: str | None = None,
        marketplace_id: str | None = None,
        seller_id: str = "",
        timeout: float = 60.0,
        dry_run: bool = False,
    ) -> None:
        self.tokens = token_provider
        self.endpoint = (endpoint or settings.sp_api_endpoint).rstrip("/")
        self.marketplace_id = marketplace_id or settings.marketplace_id
        self.seller_id = seller_id or settings.seller_id
        self.timeout = timeout

        #: When True the client validates and logs the request but never sends
        #: it. This is what makes practice mode a genuine rehearsal: exactly the
        #: same code path, exactly the same payload, no side effects.
        self.dry_run = dry_run

        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=15.0),
            # Amazon supports HTTP/2 and it reduces overhead when patching many
            # SKUs on one connection.
            #
            # Negotiated rather than demanded. httpx raises ImportError from this
            # constructor if http2=True and the 'h2' package is missing, which
            # would take the entire Amazon integration down at its first line
            # over an optional performance feature. requirements.txt pins
            # httpx[http2] so h2 is always present; this check means that if a
            # future environment somehow lacks it, the system runs a little
            # slower over HTTP/1.1 and says so, instead of not running at all.
            http2=_HTTP2_AVAILABLE,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={"user-agent": "InventoryAutopilot/1.0 (Language=Python)"},
        )
        if not _HTTP2_AVAILABLE:
            log.warning(
                "the 'h2' package is not installed, so Amazon is being called over "
                "HTTP/1.1. Everything works; large batches are slightly slower. "
                "Install httpx[http2] to restore it."
            )

        #: Counters for the run summary.
        self.call_count = 0
        self.retry_count = 0
        self.throttle_wait_seconds = 0.0
        self.dry_run_payloads: list[dict] = []

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SpApiClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- the one way to talk to Amazon -------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        operation: str = "default",
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        content: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        allow_write: bool = False,
    ) -> httpx.Response:
        """
        Make one SP-API call.

        Parameters
        ----------
        operation:
            Selects the rate-limit bucket. Use the documented names in
            :data:`RATE_LIMITS`; an unknown name falls back to a slow default,
            which fails safe.
        allow_write:
            Must be True for any method that changes something. A guard against
            a refactor accidentally turning a read into a write -- the caller
            has to say out loud that it means to write.

        Raises
        ------
        SpApiPermissionError
            403 -- the app is missing a role.
        SpApiError
            Anything else, with ``retryable`` set appropriately.
        """
        upper = method.upper()
        is_write = upper in {"POST", "PUT", "PATCH", "DELETE"}
        if is_write and not allow_write:
            raise SpApiError(
                f"{upper} {path} attempted without allow_write=True. Refusing. "
                "This is a programming guard: any call that changes something on "
                "the client's account must declare that it intends to.",
                operation=operation,
            )

        # ---------------------------------------------------------------
        # THE SAFETY GATE. Nothing gets past here carrying a price.
        # ---------------------------------------------------------------
        if json_body is not None:
            assert_quantity_only(json_body, context=f"{upper} {path}")
            assert_only_expected_fields(json_body, context=f"{upper} {path}")
        if content is not None:
            # Feeds are uploaded as raw bytes; decode and check them too, so the
            # bulk path is protected exactly as the per-SKU path is.
            try:
                decoded = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                decoded = content.decode("utf-8", errors="replace")
            assert_quantity_only(decoded, context=f"{upper} {path} (feed document)")

        # A write verb that changes nothing on the account still has to go to
        # Amazon in practice mode -- see READ_ONLY_WRITE_OPERATIONS.
        is_mutation = is_write and operation not in READ_ONLY_WRITE_OPERATIONS

        if self.dry_run and is_mutation:
            record = {
                "method": upper,
                "path": path,
                "operation": operation,
                "params": params,
                "body": json_body if json_body is not None else "<bytes>",
            }
            self.dry_run_payloads.append(record)
            log.info("PRACTICE MODE: would have sent %s %s (not sent)", upper, path)
            # A synthetic 200 so callers need no special dry-run branch. Their
            # normal success path runs, which is what makes the rehearsal real.
            return httpx.Response(
                200,
                json={"dryRun": True, "status": "ACCEPTED"},
                request=httpx.Request(upper, self.endpoint + path),
            )

        waited = _bucket_for(operation).take()
        self.throttle_wait_seconds += waited

        url = f"{self.endpoint}{path}"
        last: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {
                "x-amz-access-token": self.tokens.token(),
                "accept": "application/json",
            }
            if json_body is not None:
                headers["content-type"] = "application/json"
            if extra_headers:
                headers.update(extra_headers)

            try:
                self.call_count += 1
                response = self._client.request(
                    upper, url, params=params, json=json_body, content=content, headers=headers
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = exc
                if attempt == MAX_ATTEMPTS:
                    raise SpApiError(
                        f"Could not reach Amazon for {operation} after {MAX_ATTEMPTS} "
                        f"attempts: {exc}",
                        operation=operation,
                        retryable=True,
                    ) from exc
                self._backoff(attempt, reason=f"network error ({type(exc).__name__})")
                continue

            if response.status_code < 400:
                return response

            # ----- 401: token is stale or revoked --------------------------
            if response.status_code == 401:
                self.tokens.invalidate()
                if attempt < MAX_ATTEMPTS:
                    log.info("Amazon returned 401; refreshing the access token and retrying")
                    self.retry_count += 1
                    continue
                raise SpApiError(
                    "Amazon rejected our access token twice (401). The refresh token "
                    "may have been revoked in Seller Central, or the app's roles were "
                    "changed - changing roles invalidates the old token.",
                    status=401,
                    operation=operation,
                    errors=_extract_errors(response),
                )

            # ----- 403: missing role --------------------------------------
            if response.status_code == 403:
                raise SpApiPermissionError(
                    _explain_403(operation, response),
                    status=403,
                    operation=operation,
                    errors=_extract_errors(response),
                )

            # ----- retryable ----------------------------------------------
            if response.status_code in _RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
                self.retry_count += 1
                self._backoff(
                    attempt,
                    reason=f"HTTP {response.status_code}",
                    retry_after=response.headers.get("retry-after"),
                )
                continue

            # ----- permanent ----------------------------------------------
            errors = _extract_errors(response)
            raise SpApiError(
                _explain_error(operation, response, errors),
                status=response.status_code,
                errors=errors,
                operation=operation,
                retryable=response.status_code in _RETRYABLE_STATUSES,
            )

        raise SpApiError(  # pragma: no cover - loop always returns or raises
            f"{operation} failed after {MAX_ATTEMPTS} attempts: {last}",
            operation=operation,
            retryable=True,
        )

    # -- convenience wrappers ---------------------------------------------
    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        kw.setdefault("allow_write", True)
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw: Any) -> httpx.Response:
        kw.setdefault("allow_write", True)
        return self.request("PATCH", path, **kw)

    # -- internals ---------------------------------------------------------
    def _backoff(self, attempt: int, *, reason: str, retry_after: str | None = None) -> None:
        """
        Sleep before the next attempt.

        Honours Amazon's ``Retry-After`` header when present, because Amazon
        knows better than our formula does. Otherwise exponential with jitter;
        the jitter matters so that many SKUs failing at once do not all retry
        on the same tick.
        """
        if retry_after:
            try:
                delay = min(float(retry_after), 60.0)
            except ValueError:
                delay = 2.0 ** attempt
        else:
            import random

            delay = min(2.0 ** attempt + random.uniform(0, 1), 60.0)  # noqa: S311

        log.warning(
            "Amazon call failed (%s); attempt %d of %d, waiting %.1fs",
            reason, attempt, MAX_ATTEMPTS, delay,
        )
        self.throttle_wait_seconds += delay
        time.sleep(delay)

    @property
    def stats(self) -> dict:
        """Call statistics for the run record."""
        return {
            "calls": self.call_count,
            "retries": self.retry_count,
            "throttle_wait_seconds": round(self.throttle_wait_seconds, 1),
            "dry_run": self.dry_run,
            "dry_run_payloads": len(self.dry_run_payloads),
        }


# ===========================================================================
# Error explanation
# ===========================================================================

def _extract_errors(response: httpx.Response) -> list[dict]:
    """Pull Amazon's ``errors`` array out of a response body, tolerantly."""
    try:
        body = response.json()
    except ValueError:
        return [{"code": str(response.status_code), "message": response.text[:500]}]
    if isinstance(body, dict):
        errs = body.get("errors")
        if isinstance(errs, list):
            return [e for e in errs if isinstance(e, dict)]
        if "message" in body:
            return [{"code": body.get("code", str(response.status_code)), "message": body["message"]}]
    return []


def _explain_403(operation: str, response: httpx.Response) -> str:
    """
    A 403 nearly always means a missing role. Say which one.

    This is the highest-value error message in the codebase, because it is the
    failure most likely to happen on the first real push -- and a raw
    "403 Forbidden" would send somebody hunting in the wrong place for a day.
    """
    needed = {
        "listings.patch": "Product Listing",
        "listings.get": "Product Listing",
        "feeds.create": "Product Listing",
        "feeds.get": "Product Listing",
        "reports.create": "Inventory and Order Tracking",
        "reports.get": "Inventory and Order Tracking",
    }.get(operation, "the appropriate")

    return (
        f"Amazon refused the {operation} call with 403 Forbidden.\n\n"
        f"This almost always means the app is missing the '{needed}' role.\n\n"
        "As of the last check on 5 September 2026 the app named 'testing' had "
        "'Pricing' and 'Inventory and Order Tracking' ticked, but 'Product Listing' "
        "was NOT ticked -- and 'Product Listing' is the role that permits changing a "
        "quantity.\n\n"
        "To fix it:\n"
        "  1. Seller Central -> Apps and Services -> Develop Apps\n"
        "  2. Edit the app; tick 'Product Listing'; untick 'Pricing' (this system "
        "must never change prices)\n"
        "  3. Save, then Authorize -> Authorize app to generate a NEW refresh token, "
        "because changing roles invalidates the old one\n"
        "  4. Paste the new refresh token into Settings\n\n"
        f"Amazon said: {response.text[:400]}"
    )


def _explain_error(operation: str, response: httpx.Response, errors: list[dict]) -> str:
    """Plain-language wrapper around Amazon's error codes."""
    status = response.status_code
    code = str(errors[0].get("code", "")) if errors else ""
    message = str(errors[0].get("message", "")) if errors else response.text[:300]

    # Codes seen in this account's own feed processing reports.
    known = {
        "8684": (
            "This SKU is linked to more than one Amazon catalogue entry (GCID), which "
            "Amazon does not allow. It needs fixing in Seller Central; no quantity "
            "update will work until then. This affected 4 SKUs in the client's last "
            "manual upload."
        ),
        "13013": (
            "Amazon says the product is not in its catalogue, so an offer cannot be "
            "attached. This is a new-product problem rather than a stock problem - it "
            "accounted for 53 of the 656 SKUs in the client's last manual upload."
        ),
        "8560": (
            "Amazon could not match the product identifier. Amazon's own guidance: "
            "UPCs should have 12 digits, EANs 13. Worth checking the barcode."
        ),
    }
    if code in known:
        return f"Amazon rejected {operation} (code {code}): {known[code]}\n\nAmazon said: {message}"

    if status == 404:
        return (
            f"Amazon says this does not exist (404) for {operation}. For a listing "
            "patch that means the SKU is not on the account - which is exactly the "
            "silent-failure problem this system exists to prevent. It has been "
            "recorded as unmatched rather than retried.\n\n"
            f"Amazon said: {message}"
        )
    if status == 400:
        return (
            f"Amazon rejected the request as malformed (400) for {operation}. This is "
            "a bug in our payload, not a configuration problem, so it will not be "
            f"retried.\n\nAmazon said: {message}"
        )
    return f"Amazon returned HTTP {status} for {operation}{f' (code {code})' if code else ''}: {message}"


# ===========================================================================
# Factory
# ===========================================================================

def build_client(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    seller_id: str,
    dry_run: bool = False,
) -> SpApiClient:
    """
    Assemble a client from credentials.

    Kept as a function rather than done inline so the pipeline has exactly one
    place that constructs an Amazon client, and so tests can substitute a fake.
    """
    from app.amazon.lwa import LwaCredentials

    provider = TokenProvider(
        LwaCredentials(client_id=client_id, client_secret=client_secret, refresh_token=refresh_token)
    )
    return SpApiClient(provider, seller_id=seller_id, dry_run=dry_run)


def preflight(client: SpApiClient) -> tuple[bool, str]:
    """
    Check we can authenticate and read, before any run attempts a write.

    Called at the start of every run. Failing here costs one API call; failing
    halfway through a 5,000-SKU batch costs a confusing half-applied state.
    """
    try:
        client.tokens.token()
    except AmazonAuthError as exc:
        return False, str(exc)

    try:
        # Cheapest authenticated read available: list report types we can see.
        client.get(
            "/reports/2021-06-30/reports",
            operation="reports.get",
            params={"reportTypes": "GET_MERCHANT_LISTINGS_ALL_DATA", "pageSize": 1},
        )
    except SpApiPermissionError as exc:
        return False, str(exc)
    except SpApiError as exc:
        # A non-permission error here (e.g. 400 on a query parameter) does not
        # prove anything is broken, so it is reported without failing the run.
        log.info("preflight read returned %s; continuing", exc)
        return True, f"Authenticated. Read check was inconclusive: {exc}"
    return True, "Authenticated and able to read reports."
