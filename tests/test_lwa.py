"""
Getting and keeping an Amazon access token.

WHY THIS DESERVES ITS OWN TESTS
===============================
Every single call to Amazon depends on this module, and its two failure modes
are both quiet:

  * fetching a new token per request would work perfectly while wasting the rate
    budget the whole system is throttled against
  * a token cached past its expiry would fail an entire run an hour in, part-way
    through a batch

It is also the module an operator meets first, and always at the worst moment:
a credential is wrong and the only clue is a 400 from Amazon with a body like
``{"error": "invalid_client"}``. The messages are asserted here because "the
operator can work out what to fix" is a behaviour, not a nicety.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.amazon.lwa import (
    AmazonAuthError,
    LwaCredentials,
    TokenProvider,
    missing_credentials,
)

CREDS = LwaCredentials(
    client_id="amzn1.application-oa2-client." + "0" * 32,
    client_secret="amzn1.oa2-cs.v1." + "a" * 64,
    refresh_token="Atzr|" + "x" * 380,
)


def _provider(handler, creds: LwaCredentials = CREDS) -> TokenProvider:
    """A real TokenProvider whose token exchange is answered by ``handler``."""
    return TokenProvider(creds, transport=httpx.MockTransport(handler))


def _token_response(*, expires_in: int = 3600, value: str = "Atza|granted"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": value,
                "token_type": "bearer",
                "expires_in": expires_in,
                "refresh_token": CREDS.refresh_token,
            },
        )

    return handler


# ---------------------------------------------------------------------------
# Missing credentials
# ---------------------------------------------------------------------------


class TestMissingCredentials:
    def test_a_complete_set_reports_nothing_missing(self):
        assert missing_credentials(CREDS) == []
        assert CREDS.complete

    @pytest.mark.parametrize("field", ["client_id", "client_secret", "refresh_token"])
    def test_each_absent_value_is_named_with_where_to_find_it(self, field):
        """
        The dashboard shows these verbatim. "Missing credential" would be true
        and useless; the operator needs the Seller Central screen.
        """
        creds = LwaCredentials(
            **{**{k: getattr(CREDS, k) for k in ("client_id", "client_secret", "refresh_token")},
               field: ""}
        )
        problems = missing_credentials(creds)

        assert problems, f"{field} was not reported as missing"
        joined = " ".join(problems).lower()
        assert "develop apps" in joined or "seller central" in joined

    def test_the_credentials_never_appear_in_a_repr(self):
        """
        This object is in the frame of every traceback from a failed token
        exchange, and tracebacks get pasted into tickets and chat.
        """
        text = repr(CREDS)

        assert "REDACTED" in text
        assert CREDS.client_secret not in text
        assert CREDS.refresh_token not in text


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestCaching:
    def test_a_token_is_fetched_once_and_reused(self):
        """
        A run makes thousands of calls. One token exchange per call would waste
        the rate budget and add a round trip to every patch, for a token that is
        byte-for-byte identical.
        """
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _token_response()(request)

        provider = _provider(handler)
        first = provider.token()
        for _ in range(20):
            assert provider.token() == first

        assert len(calls) == 1, f"{len(calls)} token exchanges for 21 calls"

    def test_a_token_near_expiry_is_replaced_before_it_expires(self):
        """
        Refreshed slightly early, on purpose. A token that is valid when checked
        but expires while the request is in flight fails the request, and a
        five-thousand-item batch spans minutes.
        """
        issued = ["first", "second"]

        def handler(request: httpx.Request) -> httpx.Response:
            return _token_response(value=issued.pop(0), expires_in=3600)(request)

        provider = _provider(handler)
        assert provider.token() == "first"

        # Still inside the nominal hour, but within the safety margin.
        provider._token.expires_at = datetime.now(UTC) + timedelta(seconds=30)
        assert provider.token() == "second", "the near-expired token was reused"

    def test_invalidate_forces_a_fresh_exchange(self):
        """
        Used when Amazon answers 401 mid-run: the token is rejected before its
        stated expiry -- revoked, or the app's roles changed.
        """
        issued = ["first", "second"]

        def handler(request: httpx.Request) -> httpx.Response:
            return _token_response(value=issued.pop(0))(request)

        provider = _provider(handler)
        assert provider.token() == "first"
        provider.invalidate()
        assert provider.token() == "second"

    def test_concurrent_callers_perform_one_exchange_between_them(self):
        """
        The scheduler runs the pipeline on a background thread while the
        dashboard serves requests on others. Without the double-checked lock,
        every thread that arrived on a cold cache would start its own exchange.
        """
        calls: list[httpx.Request] = []
        barrier = threading.Barrier(8)

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _token_response()(request)

        provider = _provider(handler)
        results: list[str] = []

        def worker() -> None:
            barrier.wait()  # maximise the overlap
            results.append(provider.token())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(calls) == 1, f"{len(calls)} concurrent token exchanges"
        assert len(set(results)) == 1


# ---------------------------------------------------------------------------
# Failures the operator has to act on
# ---------------------------------------------------------------------------


class TestFailureMessages:
    def test_invalid_client_names_the_secret_and_the_rotation_deadline(self):
        """
        ``invalid_client`` means the client id or secret is wrong -- and it is
        also exactly what Amazon returns after the secret's rotation deadline
        passes, which looks identical to a bug. The message has to raise that
        possibility or the operator will go looking in the code.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_client"})

        with pytest.raises(AmazonAuthError) as exc:
            _provider(handler).token()

        message = str(exc.value)
        assert "secret" in message.lower()
        assert "rotat" in message.lower() or "expire" in message.lower()

    def test_invalid_grant_explains_that_a_role_change_revokes_the_token(self):
        """
        The single most common confusion in this integration: adding the
        Product Listing role invalidates the existing refresh token, so the
        credential that worked yesterday returns invalid_grant today.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        with pytest.raises(AmazonAuthError) as exc:
            _provider(handler).token()

        assert "role" in str(exc.value).lower()

    def test_a_network_failure_is_reported_as_such_and_not_as_bad_credentials(self):
        """
        Sending an operator to re-check working credentials because the VPS
        briefly lost DNS wastes an afternoon.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("name resolution failed")

        with pytest.raises(AmazonAuthError) as exc:
            _provider(handler).token()

        assert "reach" in str(exc.value).lower() or "network" in str(exc.value).lower()

    def test_a_response_with_no_token_is_refused_rather_than_cached_as_empty(self):
        """
        A 200 with a body we do not understand must not become an empty token
        that then fails every call with a confusing 401.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"token_type": "bearer", "expires_in": 3600})

        with pytest.raises(AmazonAuthError):
            _provider(handler).token()

    def test_the_secret_is_not_in_the_error_message(self):
        """
        These messages are shown on the dashboard and written to the log.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_client"})

        with pytest.raises(AmazonAuthError) as exc:
            _provider(handler).token()

        text = str(exc.value)
        assert CREDS.client_secret not in text
        assert CREDS.refresh_token not in text
