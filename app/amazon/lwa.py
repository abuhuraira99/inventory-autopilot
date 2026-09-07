"""
Login with Amazon: turning a long-lived refresh token into short-lived access
tokens.

HOW SP-API AUTHENTICATION WORKS NOW
===================================
Amazon retired the AWS Signature Version 4 requirement for the Selling Partner
API. That is genuinely good news for this project: it means

  * no AWS account
  * no IAM user or role
  * no request signing
  * no ``sts:AssumeRole`` dance

Just two steps:

  1. POST the refresh token to ``https://api.amazon.com/auth/o2/token``
     together with the app's client id and client secret.
  2. Put the returned access token in an ``x-amz-access-token`` header on every
     SP-API call.

Access tokens last one hour. Refresh tokens do not expire on their own -- they
last until somebody revokes them in Seller Central, or the app's roles change.

WHAT IS NEEDED, AND WHERE IT LIVES
==================================
    refresh token   encrypted in the database, entered on the Settings page
    client secret   encrypted in the database, entered on the Settings page
    client id       .env as LWA_CLIENT_ID    -- an identifier, not a secret
    seller id       .env as SELLER_ID        -- an identifier, not a secret

The two genuine secrets are deliberately NOT put in .env and never appear in
this repository. They are typed into the dashboard once, encrypted with
AES-256-GCM under MASTER_KEY, and are never rendered back to any page.

If either is absent, :func:`missing_credentials` produces the message the
dashboard shows -- naming the exact Seller Central screen it comes from (Apps
and Services -> Develop Apps -> the app -> "LWA credentials" -> View) rather
than letting the operator see a raw 400.

CACHING
=======
A token is fetched once and reused until shortly before it expires. Fetching
one per request would be slow, would waste Amazon's rate budget, and gives
nothing: the token is identical.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import settings

log = logging.getLogger(__name__)

#: Refresh this long before the token actually expires, so a request that
#: starts just before the boundary does not fail halfway through.
EXPIRY_SAFETY_MARGIN = timedelta(minutes=5)

#: A token exchange should be fast. If Amazon's auth service is slow we want to
#: know quickly rather than holding the run lock.
TOKEN_TIMEOUT = 30.0


class AmazonAuthError(RuntimeError):
    """
    Could not obtain an access token.

    The message is written for the operator: it names the likely cause and the
    exact place in Seller Central to fix it, because "invalid_grant" on its own
    tells a non-developer nothing.
    """


@dataclass(frozen=True, slots=True)
class LwaCredentials:
    """The three values needed for a token exchange."""

    client_id: str
    client_secret: str
    refresh_token: str

    def __repr__(self) -> str:  # pragma: no cover - log safety
        return (
            f"LwaCredentials(client_id={self.client_id[:40]!r}..., "
            "client_secret=<REDACTED>, refresh_token=<REDACTED>)"
        )

    @property
    def complete(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)


@dataclass(slots=True)
class AccessToken:
    """A token and the moment it stops being usable."""

    value: str
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return datetime.now(UTC) >= (self.expires_at - EXPIRY_SAFETY_MARGIN)

    @property
    def seconds_remaining(self) -> int:
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))


def missing_credentials(creds: LwaCredentials) -> list[str]:
    """
    Which credentials are absent, described the way the client should read it.

    Drives the red banner on the dashboard. Written as instructions, not as a
    list of variable names.
    """
    gaps: list[str] = []
    if not creds.client_id:
        gaps.append(
            "Amazon app Client ID is missing. Seller Central -> Apps and Services "
            "-> Develop Apps -> your app -> LWA credentials. It starts with "
            "'amzn1.application-oa2-client.'"
        )
    if not creds.client_secret:
        gaps.append(
            "Amazon app Client Secret is missing. Same screen as the Client ID: "
            "Seller Central -> Apps and Services -> Develop Apps -> your app -> "
            "LWA credentials -> View. This is the one credential that was not "
            "supplied, and nothing can talk to Amazon without it."
        )
    if not creds.refresh_token:
        gaps.append(
            "Amazon Refresh Token is missing. Seller Central -> Apps and Services "
            "-> Develop Apps -> your app -> Authorize -> Authorize app. It starts "
            "with 'Atzr|'. Note that changing the app's roles invalidates the old "
            "token, so if you have just fixed the permissions you need a new one."
        )
    return gaps


class TokenProvider:
    """
    Thread-safe access token cache.

    Thread-safe because the scheduler runs the pipeline on a worker thread
    while the web process may also be handling a "test connection" click. Two
    simultaneous refreshes would work but would waste a call, and the double
    lock check keeps it to one.
    """

    def __init__(self, credentials: LwaCredentials) -> None:
        self._creds = credentials
        self._token: AccessToken | None = None
        self._lock = threading.Lock()

    # -- public ------------------------------------------------------------
    def token(self) -> str:
        """
        A valid access token, fetching or refreshing only when necessary.

        Raises :class:`AmazonAuthError` with an actionable message on failure.
        """
        if self._token is not None and not self._token.expired:
            return self._token.value

        with self._lock:
            # Re-check: another thread may have refreshed while we waited.
            if self._token is not None and not self._token.expired:
                return self._token.value

            gaps = missing_credentials(self._creds)
            if gaps:
                raise AmazonAuthError(
                    "Cannot authenticate with Amazon.\n\n" + "\n\n".join(f"- {g}" for g in gaps)
                )

            self._token = self._exchange()
            log.info(
                "obtained Amazon access token, valid for %d minutes",
                self._token.seconds_remaining // 60,
            )
            return self._token.value

    def invalidate(self) -> None:
        """
        Discard the cached token.

        Called when Amazon answers 401/403, which can mean the token was
        revoked or the app's roles changed. Forces a fresh exchange rather than
        retrying with a token we now know is bad.
        """
        with self._lock:
            self._token = None

    @property
    def status(self) -> dict:
        """Non-secret summary for the dashboard's health panel."""
        return {
            "configured": self._creds.complete,
            "missing": missing_credentials(self._creds),
            "has_token": self._token is not None,
            "seconds_remaining": self._token.seconds_remaining if self._token else 0,
        }

    # -- internals ---------------------------------------------------------
    @retry(
        # Only transient problems are retried. An invalid_grant is permanent
        # until a human fixes something, so retrying it would just be noise.
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        reraise=True,
    )
    def _exchange(self) -> AccessToken:
        """POST the refresh token and read back an access token."""
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self._creds.refresh_token,
            "client_id": self._creds.client_id,
            "client_secret": self._creds.client_secret,
        }
        try:
            with httpx.Client(timeout=TOKEN_TIMEOUT) as client:
                response = client.post(
                    settings.lwa_token_url,
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except (httpx.TransportError, httpx.TimeoutException):
            # Let these propagate: @retry above catches them and backs off.
            # After the final attempt tenacity re-raises the original error.
            raise
        except Exception as exc:  # pragma: no cover
            raise AmazonAuthError(f"Unexpected problem contacting Amazon's login service: {exc}") from exc

        if response.status_code != 200:
            raise AmazonAuthError(_explain_auth_failure(response))

        try:
            body = response.json()
        except ValueError as exc:  # pragma: no cover
            raise AmazonAuthError("Amazon's login service returned something that was not JSON") from exc

        access = body.get("access_token")
        if not access:
            raise AmazonAuthError(
                "Amazon's login service replied without an access token. "
                f"Response keys: {sorted(body)}"
            )

        # Amazon returns 3600. Trust it but default defensively.
        expires_in = int(body.get("expires_in") or 3600)
        return AccessToken(
            value=access,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        )


def _explain_auth_failure(response: httpx.Response) -> str:
    """
    Turn an OAuth error into something the client can act on.

    Amazon's own messages are terse and unhelpful to a non-developer. These are
    the four failures that actually occur in practice.
    """
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = str(body.get("error") or "")
    description = str(body.get("error_description") or response.text[:300])

    if code == "invalid_grant":
        return (
            "Amazon rejected the refresh token (invalid_grant).\n\n"
            "The usual causes, most likely first:\n"
            "  1. The app's roles were changed after the token was created. Changing "
            "roles invalidates the old token -- go to Seller Central -> Apps and "
            "Services -> Develop Apps -> Authorize -> Authorize app and save the new "
            "token in Settings.\n"
            "  2. The authorisation was revoked in Seller Central.\n"
            "  3. The token was copied incompletely. It is very long (about 400 "
            "characters) and starts with 'Atzr|' -- check nothing was truncated.\n\n"
            f"Amazon said: {description}"
        )
    if code == "invalid_client":
        return (
            "Amazon rejected the app credentials (invalid_client).\n\n"
            "The Client ID or Client Secret is wrong. Both are on the same screen: "
            "Seller Central -> Apps and Services -> Develop Apps -> your app -> "
            "LWA credentials -> View. Note that the Client ID and the Application ID "
            "are different values -- the one we need starts with "
            "'amzn1.application-oa2-client.', not 'amzn1.sp.solution.'\n\n"
            f"Amazon said: {description}"
        )
    if code == "unauthorized_client":
        return (
            "The app is not authorised for what it is asking (unauthorized_client).\n\n"
            "This usually means the app is missing a role. To update stock quantity "
            "it needs the 'Product Listing' role, which as of the last check was NOT "
            "ticked on this app. See docs/AMAZON-APP-SETUP.md.\n\n"
            f"Amazon said: {description}"
        )
    if response.status_code == 429:
        return (
            "Amazon's login service is rate limiting us (429). This is unusual for "
            "token exchange and normally means something is requesting tokens in a "
            "loop. The run will back off and retry."
        )
    return (
        f"Amazon's login service returned HTTP {response.status_code}"
        f"{f' ({code})' if code else ''}. {description}"
    )
