"""
The application: startup, security headers, error handling, routes.

STARTUP ORDER, AND WHY IT IS THAT ORDER
=======================================
    1  logging          first, so a failure in step 2 is logged, with secrets
                        already scrubbed
    2  configuration    refuse to boot on a fatal misconfiguration rather than
                        running in a broken state
    3  directories      the quarantine and report folders must exist before any
                        job can write to them
    4  database         schema check and default settings
    5  admin account    the single shared login, created once
    6  scheduler        last, so nothing fires before everything above is ready

WHY IT REFUSES TO START
=======================
A missing ``MASTER_KEY`` means credentials cannot be decrypted; a missing
``SESSION_SECRET`` means logins cannot be signed. Starting anyway would produce
a dashboard that looks fine and fails at the first useful action. Failing loudly
at boot, with the exact command to generate the key, is kinder.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, logging_setup, scheduler
from app.config import BASE_DIR, settings
from app.core import settings_store
from app.db import healthcheck, session_scope
from app.security.auth import ensure_admin_user

log = logging.getLogger(__name__)


#: The first-start banner carrying the generated administrator password.
#:
#: NOTE THE ABSENT COLONS after "email" and "password". They are missing on
#: purpose. The log redaction filter strips the value out of any
#: ``password:`` or ``password=`` pair -- correctly, everywhere else -- so
#: writing this banner the tidy way replaces the generated password with
#: ``<REDACTED>`` in the one message whose entire purpose is to display it, on
#: the one occasion it is ever displayed. The password is stored nowhere and
#: cannot be recovered, so that mistake locks the operator out of a fresh
#: install.
#:
#: A module constant rather than an inline string so a test can render it
#: through the real filter:
#: ``tests/test_logging_redaction.py::test_the_administrator_banner_survives_redaction``
#: fails if a colon comes back.
ADMIN_BANNER = (
    "\n"
    "==============================================================\n"
    "  ADMINISTRATOR ACCOUNT CREATED\n"
    "==============================================================\n"
    "  Sign in at %s\n"
    "\n"
    "    email      %s\n"
    "    password   %s\n"
    "\n"
    "  WRITE THIS DOWN NOW. It is not stored anywhere and cannot\n"
    "  be recovered. Change it after signing in, and switch on\n"
    "  two-factor authentication.\n"
    "=============================================================="
)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    """Start up in the order described in the module docstring, and shut down cleanly."""
    logging_setup.configure()
    log.info("starting %s v%s (%s)", settings.app_name, __version__, settings.environment)

    problems = settings.startup_problems()
    if problems:
        for p in problems:
            log.critical("configuration problem: %s", p)
        if settings.is_production:
            raise RuntimeError(
                "Refusing to start with a broken configuration:\n\n"
                + "\n\n".join(f"  - {p}" for p in problems)
                + "\n\nSee docs/DEPLOYMENT.md."
            )
        log.warning("continuing despite the above because this is not production")

    settings.ensure_directories()

    ok, detail = healthcheck()
    if not ok:
        log.critical("cannot reach the database: %s", detail)
        raise RuntimeError(
            f"The database is not reachable: {detail}\n\n"
            "Check DATABASE_URL and that PostgreSQL is running. With Docker Compose "
            "this usually means the database container has not finished starting; it "
            "will be retried automatically."
        )

    with session_scope() as session:
        created = settings_store.seed_defaults(session)
        if created:
            log.info("seeded %d default settings", created)

        # Before the scheduler starts, so the dashboard never shows a run as
        # in progress when the process that owned it is gone. Safe here and
        # nowhere else: this process has only just started, and exactly one
        # process owns the scheduler, so nothing can legitimately be running.
        from app.engine.pipeline import close_interrupted_runs

        closed = close_interrupted_runs(session)
        if closed:
            log.warning("closed %d run(s) left in progress by a previous process", closed)

        admin_email = "admin@localhost"
        _user, generated = ensure_admin_user(session, email=admin_email)
        if generated:
            # Printed once, never stored in plaintext, never recoverable. The
            # operator must write it down now -- which is the correct trade for
            # not keeping a plaintext password anywhere.
            log.warning(ADMIN_BANNER, settings.base_url, admin_email, generated)

    scheduler.start()

    yield

    log.info("shutting down")
    scheduler.shutdown()


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    lifespan=lifespan,
    # The interactive API docs are useful in development and are an unnecessary
    # surface in production, where the only client is this application's own
    # dashboard.
    docs_url="/api/docs" if not settings.is_production else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if not settings.is_production else None,
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


# ===========================================================================
# Security headers
# ===========================================================================

@app.middleware("http")
async def security_headers(request: Request, call_next):  # noqa: ANN001, ANN201
    """
    Add defensive headers to every response.

    The Content-Security-Policy is the important one. The dashboard has no
    build step and loads no third-party scripts, so the policy can be strict:
    nothing but this origin, and no framing at all. That removes a large class
    of attack from a page that can change quantities on a live Amazon account.

    ``'unsafe-inline'`` is present for styles only, because the templates use a
    handful of inline ``style`` attributes for data-driven widths (progress
    bars). Scripts have no such exception.
    """
    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "object-src 'none'",
        ]
    )
    if settings.is_production:
        # Two years, subdomains included. Only sent over HTTPS, so it is inert
        # on a local HTTP deployment.
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"

    return response


# ===========================================================================
# Health
# ===========================================================================

@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """
    Liveness probe, for the external uptime monitor.

    Deliberately unauthenticated and deliberately uninformative: it says
    whether the service is up, and nothing about the account, the credentials
    or the data. An uptime check should not be a reconnaissance endpoint.
    """
    ok, detail = healthcheck()
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ok" if ok else "degraded",
            "version": __version__,
            "database": "ok" if ok else detail,
        },
    )


# ===========================================================================
# Error pages
# ===========================================================================

@app.exception_handler(404)
async def not_found(request: Request, _exc: Exception) -> HTMLResponse:
    """A plain 404, matching the dashboard's look."""
    from app.routers.helpers import render_error

    return render_error(
        request,
        status_code=404,
        heading="Page not found",
        detail="That address does not exist. Use the menu to get back.",
    )


@app.exception_handler(500)
async def server_error(request: Request, exc: Exception) -> HTMLResponse:
    """
    A 500 page that says what to do, and nothing about internals.

    The detail goes to the log; the page gets a reassurance that matters here
    more than usual: a dashboard error has not changed anything on Amazon,
    because writes only happen inside a run.
    """
    log.exception("unhandled error on %s %s", request.method, request.url.path)

    from app.routers.helpers import render_error

    return render_error(
        request,
        status_code=500,
        heading="Something went wrong",
        detail=(
            "The problem has been recorded in the server log. Nothing was sent to "
            "Amazon - changes are only ever sent during a sync run, not while you "
            "are using this page."
        ),
    )


# ===========================================================================
# Routes
# ===========================================================================

def _register_routers() -> None:
    """
    Attach the routers.

    Imported here rather than at module scope so that a syntax error in a
    router surfaces as a clear traceback at startup rather than as an obscure
    circular-import failure.
    """
    from app.routers import actions, api, auth, dashboard, settings_page

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(settings_page.router)
    app.include_router(actions.router)
    app.include_router(api.router)


_register_routers()
