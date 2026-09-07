"""
Process configuration: the small set of values that must exist before the
application can start.

DESIGN RULE
===========
There are two kinds of configuration in this system and they live in different
places on purpose:

  * **Infrastructure and secrets** -> environment variables, read here.
    Database URL, the master encryption key, the bind address. These change
    when the machine changes, never during normal operation, and two of them
    are secrets that must never touch the database.

  * **Business behaviour** -> the ``settings`` table, read through
    :mod:`app.core.settings_store`.
    Safety cap, out-of-stock threshold, sync interval, SKU prefixes in scope,
    alert recipients. The client changes these from the dashboard without a
    deploy, which was an explicit requirement.

If you are about to add a knob here, ask whether the client might ever want to
change it. If yes, it belongs in the settings table instead.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Repository root. Used to resolve the data directory and templates.
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Environment-driven settings, validated at import time."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "Inventory Autopilot"
    environment: str = Field(default="production", description="production | development")
    debug: bool = False

    #: Public base URL, used only to build links inside alert emails.
    base_url: str = "http://localhost:8000"

    # Binds all interfaces because the process runs inside a container and must
    # be reachable from the Docker network. It is NOT exposed to the internet:
    # docker-compose.yml publishes the port to 127.0.0.1 only, and the
    # recommended deployment reaches the dashboard through a private tunnel.
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8000

    # ------------------------------------------------------------- database
    #: PostgreSQL is required rather than SQLite: a full feed upserts 1.15
    #: million rows while the web process is serving reads, and SQLite's
    #: single-writer lock turns that into minutes of blocked requests.
    database_url: str = "postgresql+psycopg://autopilot:autopilot@localhost:5432/autopilot"

    #: Kept modest on purpose. The scheduler is single-threaded by design (one
    #: run at a time, enforced by an advisory lock) so a large pool would only
    #: hide bugs.
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # --------------------------------------------------------------- crypto
    #: 32 random bytes, base64 or hex encoded. Encrypts every credential stored
    #: in the database (vendor FTP password, Amazon client secret, refresh
    #: token). Losing it means re-entering those four secrets; leaking it means
    #: the database dump becomes readable, which is why it lives in the
    #: environment and never in the database or the repository.
    #:
    #: Generate with:  python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
    master_key: str = Field(default="", description="Base64 or hex 32-byte key")

    #: Signs dashboard session cookies. Rotating it logs everybody out, which
    #: is a feature after a suspected compromise.
    session_secret: str = Field(default="", min_length=0)

    #: How long a dashboard login lasts before it must be repeated.
    session_hours: int = 12

    # ------------------------------------------------------------ filesystem
    data_dir: Path = BASE_DIR / "data"

    @property
    def quarantine_dir(self) -> Path:
        """Where downloaded feed files land before they are trusted.

        Files are written here first, verified (zip opens, checksum matches,
        row count sane), and only then parsed. A half-finished download can
        therefore never be mistaken for a complete one.
        """
        return self.data_dir / "quarantine"

    @property
    def reports_dir(self) -> Path:
        """Generated CSV/XLSX reports, one folder per run."""
        return self.data_dir / "reports"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    # ------------------------------------------------------------- amazon
    #: Regional endpoint. North America covers US, CA, MX and BR, which is the
    #: set this app is authorised for. Europe and Far East have their own hosts.
    sp_api_endpoint: str = "https://sellingpartnerapi-na.amazon.com"

    #: Login with Amazon token exchange endpoint. Global, not regional.
    # noqa below: the linter reads "token" in the name as a credential. This is
    # a public, documented Amazon endpoint URL and contains no secret.
    lwa_token_url: str = "https://api.amazon.com/auth/o2/token"  # noqa: S105

    #: US marketplace. Confirmed from the client's own Price & Quantity
    #: template, whose settings blob contains
    #: primaryMarketplaceId=amzn1.mp.o.ATVPDKIKX0DER.
    marketplace_id: str = "ATVPDKIKX0DER"

    #: Amazon removed the AWS SigV4 signing requirement from SP-API, so this
    #: application needs no AWS account, no IAM user and no role to assume --
    #: only the Login with Amazon credentials below. Kept as a flag so that if
    #: Amazon ever reverses that, turning signing back on is a config change.
    sp_api_requires_sigv4: bool = False

    # ---------------------------------------------------- amazon credentials
    # These may be supplied by environment OR entered in the dashboard, which
    # is the recommended route: the client types them in once, the system
    # encrypts them, and the developer never holds them. Values found in the
    # database take precedence over these. See app.security.credentials.
    lwa_client_id: str = ""
    lwa_client_secret: str = ""
    lwa_refresh_token: str = ""
    seller_id: str = ""

    # ---------------------------------------------------- vendor credentials
    vendor_ftp_host: str = ""
    vendor_ftp_port: int = 21
    #: "ftps" = explicit FTP over TLS on port 21, which is what All Media
    #: Supply provides ("Port: 21 (Explicit FTP over TLS)"). "sftp" is SSH file
    #: transfer on 22; "ftp" is plaintext and refused unless
    #: ``allow_plaintext_ftp`` is set, because it would put the vendor password
    #: on the wire in the clear.
    vendor_ftp_mode: str = "ftps"
    vendor_ftp_user: str = ""
    vendor_ftp_password: str = ""
    vendor_ftp_path: str = "/"

    #: Escape hatch. Off by default so that a misconfiguration cannot silently
    #: downgrade the connection to plaintext.
    allow_plaintext_ftp: bool = False

    # ------------------------------------------------------------------ smtp
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "autopilot@localhost"
    smtp_starttls: bool = True

    # ------------------------------------------------------------ scheduler
    #: Set to false on a second container that should serve the dashboard only.
    #: Exactly one process must own the scheduler.
    enable_scheduler: bool = True

    #: Belt and braces on top of the database advisory lock: if a run somehow
    #: overshoots this many seconds it is abandoned and reported, rather than
    #: holding the lock forever and silently stopping all future runs.
    run_timeout_seconds: int = 3600

    # --------------------------------------------------------------- logging
    log_level: str = "INFO"
    #: JSON logs are easier to grep on a server; plain text is easier to read
    #: while developing.
    log_json: bool = True

    # ------------------------------------------------------------ validators
    @field_validator("vendor_ftp_mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        allowed = {"ftp", "ftps", "sftp"}
        v = v.lower().strip()
        if v not in allowed:
            raise ValueError(f"vendor_ftp_mode must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("environment")
    @classmethod
    def _check_env(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"production", "development", "test"}:
            raise ValueError("environment must be production, development or test")
        return v

    # ------------------------------------------------------------- helpers
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def startup_problems(self) -> list[str]:
        """
        Configuration faults that should stop a production boot.

        Returned as a list rather than raised so the caller can log every
        problem at once instead of making the operator fix them one restart at
        a time.
        """
        problems: list[str] = []

        if not self.master_key:
            problems.append(
                "MASTER_KEY is not set. Generate one with: "
                'python -c "import secrets,base64; '
                'print(base64.b64encode(secrets.token_bytes(32)).decode())"'
            )
        if not self.session_secret:
            problems.append("SESSION_SECRET is not set. Any random 32+ character string will do.")

        if self.is_production:
            if self.debug:
                problems.append("DEBUG must be false in production.")
            if self.database_url.startswith("sqlite"):
                # This is not a performance preference. app.db.run_lock is a
                # PostgreSQL advisory lock, and on SQLite it degrades to a no-op
                # that always succeeds -- silently, because there is nothing to
                # fail. That lock is the only thing preventing two overlapping
                # runs from each reading Amazon's quantity, each computing a
                # change from the same starting point, and each pushing it.
                #
                # A system that appears to work and quietly double-writes to a
                # live seller account is far worse than one that refuses to
                # start, so this refuses to start.
                problems.append(
                    "DATABASE_URL points at SQLite, which is not usable in production.\n"
                    "  Two reasons, and the first is a correctness one:\n"
                    "    1. the exclusive run lock is a PostgreSQL advisory lock. On\n"
                    "       SQLite it is a no-op, so nothing stops two runs from\n"
                    "       overlapping and pushing the same change twice.\n"
                    "    2. a full feed upserts around 1.15 million rows while the\n"
                    "       dashboard serves reads, and SQLite takes a single writer\n"
                    "       lock for the duration.\n"
                    "  Use the PostgreSQL container in docker-compose.yml."
                )
            if self.vendor_ftp_mode == "ftp" and not self.allow_plaintext_ftp:
                problems.append(
                    "VENDOR_FTP_MODE=ftp would send the vendor password in clear text. "
                    "Use ftps (explicit TLS on port 21) or sftp. To override anyway, "
                    "set ALLOW_PLAINTEXT_FTP=true and accept the risk."
                )
            if self.base_url.startswith("http://") and "localhost" not in self.base_url:
                problems.append(
                    "BASE_URL uses http:// on a non-local host. Serve the dashboard over "
                    "HTTPS, or reach it through a Cloudflare Tunnel / Tailscale so it is "
                    "never exposed in the clear."
                )
        return problems

    def ensure_directories(self) -> None:
        """Create the data directories. Safe to call repeatedly."""
        for d in (self.data_dir, self.quarantine_dir, self.reports_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    The process-wide settings singleton.

    Cached so that reading configuration is free at every call site, and so
    that a mid-run environment change cannot make one half of a run behave
    differently from the other half.
    """
    return Settings()


#: Convenience alias so modules can ``from app.config import settings``.
settings = get_settings()


def running_under_pytest() -> bool:
    """True during a test run. Used to keep the scheduler asleep in tests."""
    return "PYTEST_CURRENT_TEST" in os.environ
