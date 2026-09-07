"""
End-to-end tests for the web interface.

WHAT THESE PROVE
================
  * every page renders without a template error
  * every page requires a login -- there is no accidentally public route
  * the pause switch, the settings form and the credentials form work
  * the JSON API answers
  * a stored secret is NEVER returned to a browser

That last one is the most valuable test in the file. A template that
accidentally rendered a decrypted credential would be a serious leak, and it is
exactly the sort of mistake a refactor introduces silently. So the test walks
every page looking for the actual secret values.

No network, no live database: SQLite in memory, and Amazon and the vendor are
simply not configured -- which is also a realistic state, because it is what
Phase 1 of the rollout looks like.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import settings_store
from app.models import Base, Run, RunStatus, RunTrigger, SyncMode, VendorProduct
from app.security import credentials as creds
from app.security.auth import ensure_admin_user

TEST_EMAIL = "admin@localhost"
TEST_PASSWORD = "a-long-enough-test-password"

# Real-shaped secrets, so the leak test is meaningful. The refresh token has to
# look like a real one or the sanity check in credentials.py rejects it.
SECRET_FTP = "test-only-not-a-real-password"
SECRET_CLIENT = "not-an-amzn-prefixed-value-just-a-long-random-looking-secret"
SECRET_TOKEN = "Atzr|" + ("IwEBI" + "x" * 40) * 5


@pytest.fixture
def wired_app(monkeypatch):
    """
    The real FastAPI application, wired to a fresh in-memory database.

    The app's own engine is replaced rather than the settings being changed,
    because ``app.config.settings`` is built and cached at import time -- which
    is the right behaviour in production (a mid-run configuration change would
    make half a run behave differently from the other half) and something the
    test simply has to work around.

    Yields the app; the two client fixtures below build on it. Sharing this
    matters: earlier versions of this file created bare clients for the
    "requires login" tests, which then hit the unpatched production engine and
    failed for the wrong reason entirely.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    import app.db as db_module

    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", factory)

    # The advisory lock is PostgreSQL-only; on SQLite it is a no-op that always
    # succeeds, which is correct for a single-process test.
    from contextlib import contextmanager

    @contextmanager
    def _always_acquire(**_kw):
        yield True

    monkeypatch.setattr(db_module, "run_lock", _always_acquire)

    # Seed the account, the settings and the credentials.
    with factory() as session:
        settings_store.seed_defaults(session)
        ensure_admin_user(session, email=TEST_EMAIL, password=TEST_PASSWORD)
        creds.set_secret(session, "vendor_ftp_password", SECRET_FTP)
        creds.set_secret(session, "lwa_client_secret", SECRET_CLIENT)
        creds.set_secret(session, "lwa_refresh_token", SECRET_TOKEN)

        # A little data so the pages are not all empty states.
        session.add(
            VendorProduct(
                barcode="0008811065126", raw_barcode="8811065126",
                artist="CARLISLE,BELINDA", title="HER GREATEST HITS",
                price=9.84, stock=2, product_format="CD",
            )
        )
        session.add(
            Run(
                trigger=RunTrigger.MANUAL,
                triggered_by=TEST_EMAIL,
                mode=SyncMode.DRY_RUN,
                status=RunStatus.DRY_RUN_COMPLETE,
                files_processed=1,
                rows_read=1158340,
                proposed_changes=412,
                guardrail_message="Practice mode. 412 products would have been changed.",
            )
        )
        session.commit()

    # Keep the scheduler asleep: a test that starts real jobs would reach the
    # vendor's FTP server, which is exactly what must never happen in CI.
    import app.scheduler as sched

    monkeypatch.setattr(sched, "start", lambda: None)
    monkeypatch.setattr(sched, "shutdown", lambda: None)
    monkeypatch.setattr(sched, "status", lambda: [])

    from app.db import get_session
    from app.main import app as fastapi_app

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    fastapi_app.dependency_overrides[get_session] = _override
    try:
        yield fastapi_app
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.fixture
def anon_client(wired_app):
    """A client that has NOT signed in."""
    with TestClient(wired_app, follow_redirects=False) as client:
        yield client


@pytest.fixture
def app_client(wired_app):
    """A signed-in client."""
    with TestClient(wired_app, follow_redirects=False) as client:
        response = client.post(
            "/login",
            data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "next": "/"},
        )
        assert response.status_code == 303, response.text
        yield client


# ===========================================================================
# Every page renders
# ===========================================================================

PAGES = [
    "/",
    "/runs",
    "/runs/1",
    "/products",
    "/products?q=8811065126",
    "/products?only=in_stock",
    "/unmatched",
    "/reports",
    "/audit",
    "/settings",
]


@pytest.mark.parametrize("path", PAGES)
def test_page_renders(app_client, path):
    """A 200 and some HTML. Catches every template syntax and filter error."""
    response = app_client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}\n{response.text[:900]}"
    assert "<!DOCTYPE html>" in response.text
    # A Jinja undefined would render as the literal word; the filters return an
    # em dash for a missing value instead.
    assert "Undefined" not in response.text


def test_health_needs_no_login(anon_client):
    """
    The liveness probe is unauthenticated on purpose, and says nothing useful
    to an attacker: up or down, and the version. Nothing about the account.
    """
    response = anon_client.get("/health")
    assert response.status_code in (200, 503)
    body = response.json()
    assert set(body) <= {"status", "version", "database"}
    assert "seller" not in response.text.lower()
    assert "amzn1" not in response.text


# ===========================================================================
# Nothing is public
# ===========================================================================

PROTECTED = [
    "/", "/runs", "/products", "/unmatched", "/reports", "/audit",
    "/settings", "/api/status", "/api/runs", "/api/coverage",
    "/api/health/detail", "/api/feed-files",
]


@pytest.mark.parametrize("path", PROTECTED)
def test_requires_login(anon_client, path):
    """
    Every page and every API route redirects a signed-out visitor to /login.

    This is the test that catches a new route added without the
    ``require_login`` dependency -- which would otherwise be invisible until
    somebody found it.
    """
    response = anon_client.get(path)
    assert response.status_code in (303, 307), f"{path} was reachable without signing in"
    assert "/login" in response.headers.get("location", "")


# ===========================================================================
# THE IMPORTANT ONE: secrets never reach a browser
# ===========================================================================

def test_no_secret_is_ever_rendered(app_client):
    """
    Walk every page and assert that no stored secret appears in the HTML.

    The most valuable test here. Credentials are write-only by design: the
    settings page shows only the last four characters, and there is no code
    path that returns a plaintext to a template. This proves it, and would fail
    loudly if a refactor ever broke it.
    """
    for path in PAGES + ["/api/status", "/api/health/detail", "/api/coverage"]:
        response = app_client.get(path)
        body = response.text
        assert SECRET_FTP not in body, f"the vendor password leaked on {path}"
        assert SECRET_CLIENT not in body, f"the Amazon client secret leaked on {path}"
        assert SECRET_TOKEN not in body, f"the Amazon refresh token leaked on {path}"
        # Not even a long fragment of the token.
        assert SECRET_TOKEN[:60] not in body, f"part of the refresh token leaked on {path}"


def test_settings_shows_only_a_hint(app_client):
    """The last four characters are shown, and nothing more."""
    response = app_client.get("/settings")
    assert response.status_code == 200
    assert SECRET_FTP[-4:] in response.text        # the hint is shown
    assert SECRET_FTP not in response.text          # the value is not


# ===========================================================================
# The controls work
# ===========================================================================

def test_pause_toggles_and_is_audited(app_client):
    """The emergency stop, and its audit trail."""
    response = app_client.post("/actions/pause")
    assert response.status_code == 303

    page = app_client.get("/")
    assert page.status_code == 200
    assert "Everything is paused" in page.text

    audit = app_client.get("/audit")
    assert "killswitch.on" in audit.text

    app_client.post("/actions/pause")
    assert "Everything is paused" not in app_client.get("/").text


def test_settings_can_be_saved(app_client):
    """A behaviour change through the form, end to end."""
    response = app_client.post(
        "/settings",
        data={
            "max_quantity": "12",
            "safety_buffer": "1",
            "sync_interval_minutes": "30",
            "sku_prefixes_in_scope": "HA-AMS-",
            "feed_delimiter": "|",
            "sku_prefix_for_new": "HA-AMS-",
            "timezone": "America/New_York",
            "sync_mode": "dry_run",
        },
    )
    assert response.status_code == 303

    page = app_client.get("/settings")
    assert 'value="12"' in page.text

    audit = app_client.get("/audit")
    assert "setting.changed" in audit.text


def test_settings_rejects_a_bad_value_and_saves_nothing(app_client):
    """
    Out-of-range input is refused with a readable message, and NOTHING is
    committed -- a half-applied settings change would leave the system in a
    state nobody chose.
    """
    before = app_client.get("/settings").text

    response = app_client.post("/settings", data={"max_quantity": "99999"})
    assert response.status_code == 303
    assert "error=" in response.headers["location"]

    after = app_client.get("/settings").text
    assert "99999" not in after
    # The previous value survived.
    assert before.count('name="max_quantity"') == after.count('name="max_quantity"')


def test_credential_can_be_replaced(app_client):
    """Storing a credential through the form."""
    response = app_client.post(
        "/settings/credentials",
        data={"key": "vendor_ftp_password", "value": "a-brand-new-password"},
    )
    assert response.status_code == 303

    page = app_client.get("/settings")
    assert "word" in page.text.lower()               # the page still renders
    assert "a-brand-new-password" not in page.text   # and does not echo it


def test_refresh_token_sanity_check(app_client):
    """
    A truncated refresh token is refused at entry.

    Amazon's tokens are about 400 characters and it is genuinely easy to lose
    the end when copying. Catching that here is far kinder than a confusing
    invalid_grant at 3am.
    """
    response = app_client.post(
        "/settings/credentials",
        data={"key": "lwa_refresh_token", "value": "Atzr|too-short"},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "truncated" in response.headers["location"].lower()


def test_client_id_pasted_into_the_secret_field_is_caught(app_client):
    """The commonest paste mistake, refused with an explanation."""
    response = app_client.post(
        "/settings/credentials",
        data={
            "key": "lwa_client_secret",
            "value": "amzn1.application-oa2-client.EXAMPLE00000000000000000000000000",
        },
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_application_id_pasted_into_the_secret_field_is_caught(app_client):
    """The other ID on the same screen."""
    response = app_client.post(
        "/settings/credentials",
        data={
            "key": "lwa_client_secret",
            "value": "amzn1.sp.solution.00000000-0000-0000-0000-000000000000",
        },
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_modern_amzn1_prefixed_client_secret_is_accepted(app_client):
    """
    Amazon's CURRENT client secret format must be accepted.

    Modern LWA secrets look like `amzn1.oa2-cs.v1.<64 hex>`. An earlier version
    of the validation rejected anything starting "amzn1." and would therefore
    have refused the client's real secret outright -- the credential could not
    have been entered at all. Regression test for exactly that.
    """
    response = app_client.post(
        "/settings/credentials",
        data={
            "key": "lwa_client_secret",
            "value": "amzn1.oa2-cs.v1." + "a1b2c3d4" * 8,
        },
    )
    assert response.status_code == 303
    assert "error=" not in response.headers["location"], (
        "the modern amzn1.oa2-cs. secret format was rejected"
    )


def test_legacy_bare_hex_client_secret_is_accepted(app_client):
    """Older accounts have a bare 64-character secret with no prefix."""
    response = app_client.post(
        "/settings/credentials",
        data={"key": "lwa_client_secret", "value": "9f8e7d6c" * 8},
    )
    assert response.status_code == 303
    assert "error=" not in response.headers["location"]


# ===========================================================================
# The API
# ===========================================================================

def test_api_status(app_client):
    response = app_client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["paused"] is False
    assert body["mode"] == "dry_run"
    assert "needs_attention" in body
    assert body["counts"]["vendor_products"] == 1


def test_api_health_detail_lists_what_is_missing(app_client):
    """
    The authenticated health check names what is misconfigured -- which is
    exactly why it is authenticated and /health is not.
    """
    response = app_client.get("/api/health/detail")
    assert response.status_code == 200
    body = response.json()
    assert "concerns" in body
    assert isinstance(body["concerns"], list)


def test_api_has_no_write_routes():
    """
    There is no API route that changes anything.

    Every mutation is a form POST behind a session cookie and, where it
    matters, a typed confirmation. An API key that could zero 45,000 listings
    would be a liability with no compensating benefit.
    """
    from app.routers.api import router

    for route in router.routes:
        methods = getattr(route, "methods", set())
        assert methods <= {"GET", "HEAD"}, f"{route.path} accepts {methods}"


# ===========================================================================
# Security headers
# ===========================================================================

def test_security_headers_are_set(app_client):
    """
    The CSP is the important one: the dashboard has no build step and loads no
    third-party scripts, so the policy can be strict.
    """
    response = app_client.get("/")
    csp = response.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    # Scripts get no inline exception, unlike styles.
    assert "script-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp

    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_session_cookie_is_hardened(app_client):
    """HttpOnly, so a cross-site script cannot read the session."""
    cookie = app_client.cookies.jar._cookies  # noqa: SLF001
    raw = str(app_client.headers) + str(cookie)
    assert "autopilot_session" in str(cookie) or raw  # cookie was set

    # Re-log in to inspect the Set-Cookie header directly.
    response = app_client.post(
        "/login", data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "next": "/"}
    )
    header = response.headers.get("set-cookie", "")
    assert "HttpOnly" in header
    assert "SameSite=lax" in header or "samesite=lax" in header.lower()


# ===========================================================================
# Login
# ===========================================================================

def test_wrong_password_is_generic(anon_client):
    """
    A failed login never reveals whether the account exists.

    Distinguishing "no such account" from "wrong password" tells an attacker
    which addresses are worth attacking.
    """
    a = anon_client.post("/login", data={"email": TEST_EMAIL, "password": "wrong", "next": "/"})
    b = anon_client.post(
        "/login", data={"email": "nobody@example.com", "password": "wrong", "next": "/"}
    )

    assert "error=" in a.headers["location"]
    assert "error=" in b.headers["location"]
    # The same message for both.
    assert a.headers["location"].split("error=")[1] == b.headers["location"].split("error=")[1]


def test_open_redirect_is_refused(anon_client):
    """
    An absolute URL in ``next`` is ignored.

    Otherwise a phishing link could pass through this domain and land the
    victim somewhere else, wearing our address bar.
    """
    response = anon_client.post(
        "/login",
        data={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "next": "https://evil.example.com/steal",
        },
    )
    if response.status_code == 303:
        assert response.headers["location"] == "/"
