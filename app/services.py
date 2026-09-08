"""
Assembling the clients from stored credentials.

WHY THIS FILE EXISTS
====================
The scheduler and the web routers both need an Amazon client and a vendor
connection, and both must read the credentials the same way -- database first,
environment second. Duplicating that in two places is how one of them ends up
using a stale value.

It is also the only place outside :mod:`app.security.credentials` that touches
plaintext secrets, which keeps the blast radius of a mistake small: if you want
to know everywhere a secret can be read, it is these two files.

DEGRADING GRACEFULLY
====================
Both builders return ``None`` rather than raising when credentials are missing.
That is what lets the system run usefully in Phase 1 of the rollout: no Amazon
credentials means stages 1-3 still fetch the vendor files, update the database
and produce the five reports, with Amazon untouched. The dashboard shows a
banner saying exactly what is missing and how to supply it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.amazon.client import SpApiClient, build_client
from app.config import settings
from app.core import settings_store
from app.models import SyncMode
from app.security import credentials as creds
from app.vendor.ftp_client import VendorCredentials

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ReadinessReport:
    """
    What is configured and what is not, in the client's language.

    Drives the banner across the top of the dashboard. Written as instructions
    rather than as a list of missing keys, because the person reading it is the
    person who has to fix it.
    """

    vendor_ready: bool
    amazon_ready: bool
    problems: list[str]
    warnings: list[str]

    @property
    def fully_ready(self) -> bool:
        return self.vendor_ready and self.amazon_ready

    @property
    def can_do_anything(self) -> bool:
        """True if at least the vendor side works, which is Phase 1."""
        return self.vendor_ready


def vendor_credentials(session: Session) -> VendorCredentials | None:
    """
    Build the vendor connection details, or ``None`` if not configured.

    Host, port, username and mode come from the environment (they are not
    secret); the password comes from :mod:`app.security.credentials`, which
    prefers the client's dashboard entry over any environment value.
    """
    password = creds.get_secret(session, "vendor_ftp_password")
    host = settings.vendor_ftp_host
    user = settings.vendor_ftp_user

    if not (host and user and password):
        return None

    mode = settings.vendor_ftp_mode
    if mode == "ftp" and not settings.allow_plaintext_ftp:
        # Refusing rather than silently downgrading. Plain FTP would put the
        # vendor password on the wire in clear text, and All Media Supply
        # supports explicit TLS on the same port, so there is no reason to.
        log.error(
            "vendor mode is plain ftp but ALLOW_PLAINTEXT_FTP is not set; refusing to "
            "connect. Use ftps (explicit TLS on port 21), which this vendor supports."
        )
        return None

    return VendorCredentials(
        host=host,
        port=settings.vendor_ftp_port,
        username=user,
        password=password,
        mode=mode,
        remote_path=settings.vendor_ftp_path,
    )


def amazon_client(session: Session, *, force_dry_run: bool = False) -> SpApiClient | None:
    """
    Build an Amazon client, or ``None`` if the credentials are incomplete.

    ``dry_run`` is derived from the ``sync_mode`` setting rather than passed in,
    so practice mode cannot be bypassed by a caller that forgets to check it.
    Both practice mode and "ask first" build a dry-run client: in "ask first"
    the run must not send anything until a human approves, and the approval
    route builds a fresh, sending client at that moment.
    """
    client_id = settings.lwa_client_id
    client_secret = creds.get_secret(session, "lwa_client_secret")
    refresh_token = creds.get_secret(session, "lwa_refresh_token")
    seller_id = settings.seller_id

    if not (client_id and client_secret and refresh_token and seller_id):
        return None

    mode = SyncMode(settings_store.get(session, "sync_mode"))
    dry = force_dry_run or mode in (SyncMode.DRY_RUN, SyncMode.NEEDS_APPROVAL)

    return build_client(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        seller_id=seller_id,
        dry_run=dry,
    )


def sending_amazon_client(session: Session) -> SpApiClient | None:
    """
    A client that will actually send, whatever the mode says.

    Used by exactly two routes: approving a held batch, and rolling one back.
    In both cases a human has just clicked the button, so the human IS the
    gate; keeping the dry-run flag on would make the button silently do
    nothing, which would be far worse than sending.
    """
    client_id = settings.lwa_client_id
    client_secret = creds.get_secret(session, "lwa_client_secret")
    refresh_token = creds.get_secret(session, "lwa_refresh_token")
    seller_id = settings.seller_id

    if not (client_id and client_secret and refresh_token and seller_id):
        return None

    return build_client(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        seller_id=seller_id,
        dry_run=False,
    )


def readiness(session: Session) -> ReadinessReport:
    """
    Check what is configured, and say what to do about anything that is not.

    Every message names the screen in Seller Central or the field in Settings
    where the fix lives. A message that only says "missing credential" would
    make the reader guess.
    """
    problems: list[str] = []
    warnings: list[str] = []

    # -- vendor ------------------------------------------------------------
    vendor_ready = True
    if not settings.vendor_ftp_host:
        problems.append(
            "The vendor's server address is not set. Add VENDOR_FTP_HOST to the "
            "server's .env file. The vendor supplies it - it looks like "
            "ftp.vendor.example.com."
        )
        vendor_ready = False
    if not settings.vendor_ftp_user:
        problems.append(
            "The vendor's username is not set. Add VENDOR_FTP_USER to the .env file."
        )
        vendor_ready = False
    if not creds.get_secret(session, "vendor_ftp_password"):
        problems.append(
            "The vendor's FTP password is not set. Enter it in Settings under "
            "Credentials - it will be encrypted immediately and never shown again."
        )
        vendor_ready = False

    if settings.vendor_ftp_mode == "ftp":
        warnings.append(
            "The vendor connection is set to plain FTP, which sends the password "
            "unencrypted. This vendor supports explicit FTP over TLS on the same "
            "port 21 - set VENDOR_FTP_MODE=ftps."
        )

    # -- amazon ------------------------------------------------------------
    amazon_ready = True
    if not settings.lwa_client_id:
        problems.append(
            "The Amazon app Client ID is not set. Find it in Seller Central under "
            "Apps and Services, Develop Apps, your app, LWA credentials. It starts "
            "with 'amzn1.application-oa2-client.'"
        )
        amazon_ready = False
    if not creds.get_secret(session, "lwa_client_secret"):
        problems.append(
            "The Amazon app Client Secret is not set. It is on the same screen as the "
            "Client ID: Seller Central, Apps and Services, Develop Apps, your app, "
            "LWA credentials, View. This was the one credential not supplied with the "
            "others, and nothing can talk to Amazon without it."
        )
        amazon_ready = False
    if not creds.get_secret(session, "lwa_refresh_token"):
        problems.append(
            "The Amazon Refresh Token is not set. Seller Central, Apps and Services, "
            "Develop Apps, your app, Authorize, Authorize app. It starts with 'Atzr|'."
        )
        amazon_ready = False
    if not settings.seller_id:
        problems.append(
            "The Seller ID is not set. Add SELLER_ID to the .env file. Find it in "
            "Seller Central under Settings, Account Info, Merchant Token."
        )
        amazon_ready = False

    # -- the permission problem we already know about ---------------------
    # Recorded from the screenshot of the app supplied on 5 September 2026.
    # This is a warning rather than a problem because it cannot be detected
    # from here - SP-API will not tell us which roles were granted, and a
    # missing one only shows up as a 403 on the first write attempt. A
    # forewarning is far more useful than that surprise.
    #
    # It is silenced by a setting rather than shown forever, because a warning
    # that cannot be acted upon stops being read. Once the roles are fixed in
    # Seller Central there is nothing left for this text to tell anybody, and
    # leaving it on screen next to two real warnings teaches the operator to
    # ignore the whole panel. The setting only hides the reminder; it cannot
    # grant a role, and the 403 still happens if the work was not really done.
    cfg = settings_store.get_all(session)
    if amazon_ready and not cfg.get("amazon_roles_checked"):
        warnings.append(
            "Check the Amazon app's permissions before enabling automatic sending. "
            "When the app was last inspected it had 'Pricing' and 'Inventory and "
            "Order Tracking' ticked, but NOT 'Product Listing' - and 'Product "
            "Listing' is the role that allows a quantity to be changed. Tick it, "
            "untick 'Pricing' (this system must never change prices), save, then "
            "re-authorise to get a new refresh token. See docs/AMAZON-APP-SETUP.md."
        )

    # -- alerts ------------------------------------------------------------
    if not cfg.get("alert_emails_critical"):
        warnings.append(
            "No email address is set for serious problems. Add one in Settings under "
            "Alerts, otherwise a stopped run will only be visible if somebody opens "
            "this dashboard."
        )
    if not settings.smtp_host:
        warnings.append(
            "No mail server is configured, so alerts cannot be emailed. They will "
            "still appear on this dashboard. Set SMTP_HOST in the .env file to enable "
            "email."
        )

    return ReadinessReport(
        vendor_ready=vendor_ready,
        amazon_ready=amazon_ready,
        problems=problems,
        warnings=warnings,
    )
