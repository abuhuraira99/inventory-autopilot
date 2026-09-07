"""
Configurations the system must refuse to start with.

A misconfiguration that stops the process is an inconvenience. A
misconfiguration that lets the process run while quietly disabling a safeguard
is an incident, and the safeguards here are the kind that fail silently: an
advisory lock that is simply absent, a password sent in clear text over a
network nobody is watching.

``app/main.py`` turns anything reported here into a refusal to boot when
ENVIRONMENT=production, and into a warning otherwise, so development stays
convenient.
"""

from __future__ import annotations

import pytest

from app.config import Settings


def _settings(**overrides) -> Settings:
    """
    A valid production configuration, with overrides applied.

    Built explicitly rather than by mutating the cached singleton, because that
    singleton is shared by the whole test session.
    """
    base = {
        "environment": "production",
        "master_key": "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleXRlc3Q=",
        "session_secret": "a-long-enough-session-secret-for-tests",
        "database_url": "postgresql+psycopg://autopilot:pw@db:5432/autopilot",
        "base_url": "https://autopilot.example.com",
        "debug": False,
    }
    base.update(overrides)
    return Settings(**base)


class TestProductionRefusals:
    def test_a_valid_production_configuration_has_no_problems(self):
        """The baseline. Without this, every test below could pass vacuously."""
        assert _settings().startup_problems() == []

    def test_sqlite_is_refused_in_production(self):
        """
        THE SILENT ONE.

        ``app.db.run_lock`` is a PostgreSQL advisory lock. On SQLite it degrades
        to a no-op that always reports success, so nothing prevents two runs
        from overlapping -- each reading Amazon's quantity, each computing a
        change from the same starting point, and each pushing it.

        Nothing about that failure is visible. The system appears to work and
        double-writes to a live seller account. Refusing to start is the only
        honest response, and previously nothing checked it at all: SQLite is
        the test default and needs no container, so reaching for it in
        production is an easy mistake to make.
        """
        problems = _settings(database_url="sqlite+pysqlite:///./autopilot.db").startup_problems()

        assert problems, "SQLite in production must be refused"
        joined = "\n".join(problems)
        assert "run lock" in joined, "the message must explain the correctness risk"
        assert "docker-compose" in joined, "and say what to do instead"

    def test_debug_is_refused_in_production(self):
        assert any("DEBUG" in p for p in _settings(debug=True).startup_problems())

    def test_plaintext_ftp_is_refused_unless_explicitly_accepted(self):
        """
        Plain FTP sends the vendor password in clear text. Refused by default,
        with a documented override -- because a vendor that offers nothing else
        is a business problem, not a reason to pretend the risk is absent.
        """
        problems = _settings(vendor_ftp_mode="ftp").startup_problems()
        assert any("clear text" in p for p in problems)

        assert _settings(
            vendor_ftp_mode="ftp", allow_plaintext_ftp=True
        ).startup_problems() == []

    def test_serving_the_dashboard_over_plain_http_is_refused(self):
        """
        The dashboard can change a live Amazon account. A session cookie for it
        must never cross a network unencrypted.
        """
        problems = _settings(base_url="http://203.0.113.10:8000").startup_problems()
        assert any("HTTPS" in p or "Tunnel" in p for p in problems)

    def test_localhost_over_http_is_allowed(self):
        """Reaching it through an SSH tunnel or Tailscale is the documented setup."""
        assert _settings(base_url="http://localhost:8000").startup_problems() == []

    @pytest.mark.parametrize("missing", ["master_key", "session_secret"])
    def test_the_keys_are_required_everywhere_not_only_in_production(self, missing):
        """
        Without MASTER_KEY the stored credentials cannot be decrypted, so the
        process would run in a state where nothing works and the reason is
        obscure. Checked outside the production branch deliberately.
        """
        problems = _settings(environment="development", **{missing: ""}).startup_problems()
        assert problems


class TestTheAmazonClientCanActuallyBeBuilt:
    def test_constructing_a_client_does_not_raise(self):
        """
        THE ONE THAT WAS ACTUALLY BROKEN.

        ``SpApiClient.__init__`` asks httpx for HTTP/2, and httpx raises
        ImportError from that constructor if the ``h2`` package is absent.
        ``requirements.txt`` pinned plain ``httpx==0.28.1``, so h2 was never
        installed and *every* SP-API call path died on the first line of the
        constructor -- the entire Amazon half of the system.

        No test noticed, because no test had ever built a client.

        The pin is now ``httpx[http2]``, and the constructor negotiates rather
        than demands, so a future environment without h2 runs over HTTP/1.1 and
        logs why instead of not running.
        """
        from app.amazon.client import SpApiClient

        class _Tokens:
            def token(self) -> str:
                return "t"

            def invalidate(self) -> None:
                pass

        client = SpApiClient(
            _Tokens(),
            endpoint="https://sellingpartnerapi-na.amazon.com",
            marketplace_id="ATVPDKIKX0DER",
            seller_id="A1TESTSELLER",
        )
        try:
            assert client.endpoint.endswith("amazon.com")
        finally:
            client.close()
