"""
Shared plumbing for the routers: templates, the current user, formatting.

The template filters here exist because the same formatting decisions were
being repeated in every template, and inconsistency in a dashboard reads as
carelessness. A quantity is always a grouped integer; a duration is always
human ("2m 14s", never "134.2"); a timestamp is always shown in the client's
own timezone, because a New York operator should not have to convert UTC in
their head at 6am.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import __version__
from app.config import BASE_DIR, settings
from app.core import settings_store
from app.db import get_session
from app.models import User
from app.security.auth import SESSION_COOKIE, SessionData, read_session

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


# ===========================================================================
# Formatting filters
# ===========================================================================

def fmt_int(value: Any) -> str:
    """``38341`` -> ``"38,341"``. Blank for None, never "None"."""
    if value is None or value == "":
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def fmt_money(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def fmt_duration(seconds: Any) -> str:
    """Seconds as something a person reads without converting."""
    if seconds is None:
        return "—"
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if s < 1:
        return "under a second"
    if s < 60:
        return f"{s:.0f}s"
    minutes, secs = divmod(int(s), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def fmt_datetime(value: Any, tz_name: str | None = None) -> str:
    """
    A timestamp in the client's own timezone.

    ``tz_name`` is not optional in practice -- every template passes ``tz``
    from the settings. It defaults to None rather than to a city because a
    default city is a lie waiting to happen: it used to be America/New_York,
    so a caller who forgot the argument would have rendered New York times,
    correctly formatted and confidently wrong, on a system the client had
    moved to Los Angeles. Falling back to UTC and SAYING so is worse-looking
    and much safer -- a visible "UTC" is a question someone asks, and a silent
    three-hour error is not.
    """
    if value is None:
        return "—"
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    if not tz_name:
        return value.strftime("%d %b %Y, %H:%M UTC")
    try:
        local = value.astimezone(ZoneInfo(tz_name))
        return local.strftime("%d %b %Y, %H:%M")
    except Exception:  # pragma: no cover
        return value.strftime("%d %b %Y, %H:%M UTC")


def fmt_ago(value: Any) -> str:
    """"14 minutes ago". The most useful form on a status page."""
    if value is None or not isinstance(value, datetime):
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = (datetime.now(UTC) - value).total_seconds()

    if delta < 0:
        # A scheduled future time, e.g. the next run.
        delta = -delta
        prefix, suffix = "in ", ""
    else:
        prefix, suffix = "", " ago"

    if delta < 45:
        return "just now" if not prefix else "in a moment"
    if delta < 3600:
        n = int(delta // 60)
        return f"{prefix}{n} minute{'s' if n != 1 else ''}{suffix}"
    if delta < 86400:
        n = int(delta // 3600)
        return f"{prefix}{n} hour{'s' if n != 1 else ''}{suffix}"
    n = int(delta // 86400)
    return f"{prefix}{n} day{'s' if n != 1 else ''}{suffix}"


def fmt_bytes(value: Any) -> str:
    if value is None:
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def status_tone(status: Any) -> str:
    """
    Map a status to a semantic tone the stylesheet understands.

    Kept in Python rather than as a chain of template conditionals, so the
    mapping is in one place and testable. Semantic colour is separate from the
    accent colour: a green pill always means "fine" and never "this is the
    brand colour".
    """
    value = getattr(status, "value", status)
    return {
        "completed": "good",
        "verified": "good",
        "no_changes": "good",
        "sent": "good",
        "dry_run_complete": "info",
        "awaiting_approval": "warn",
        "pending": "info",
        "running": "info",
        "sending": "info",
        "paused": "warn",
        "halted_by_guardrail": "warn",
        "partially_failed": "warn",
        "rolled_back": "neutral",
        "rejected": "bad",
        "failed": "bad",
        "quarantined": "bad",
        "not_applied": "bad",
        "error": "bad",
        "skipped": "neutral",
        "skipped_old": "neutral",
        "skipped_duplicate": "neutral",
    }.get(str(value).lower(), "neutral")


def status_label(status: Any) -> str:
    """
    Turn an enum value into something a client understands.

    "halted_by_guardrail" is meaningless to the person paying for this;
    "Stopped by a safety rule" is not.
    """
    value = str(getattr(status, "value", status)).lower()
    return {
        "dry_run": "Practice mode",
        "needs_approval": "Ask first",
        "automatic": "Automatic",
        "completed": "Completed",
        "no_changes": "Nothing to change",
        "dry_run_complete": "Practice run finished",
        "awaiting_approval": "Waiting for approval",
        "halted_by_guardrail": "Stopped by a safety rule",
        "failed": "Failed",
        "paused": "Paused",
        "running": "Running",
        "pending": "Ready",
        "sending": "Sending",
        "sent": "Sent",
        "verified": "Confirmed on Amazon",
        "partially_failed": "Partly rejected",
        "rejected": "Rejected",
        "rolled_back": "Undone",
        "accepted": "Accepted",
        "not_applied": "Did not take effect",
        "skipped": "Skipped",
        "to_zero": "Off sale",
        "down": "Reduced",
        "up": "Raised",
        "full": "Full feed",
        "delta": "Delta feed",
        "discovered": "Found",
        "downloaded": "Downloaded",
        "parsed": "Processed",
        "quarantined": "Quarantined",
        "skipped_old": "Skipped (not today)",
        "skipped_duplicate": "Skipped (already seen)",
        "barcode_exact": "Barcode matched",
        "barcode_variant": "Barcode matched (different length)",
        "manual_override": "Manual mapping",
        "unmapped": "Not matched",
        "no_listing": "Not listed on Amazon",
        "out_of_scope_prefix": "Belongs to another supplier",
        "bad_barcode": "Unusable barcode",
        "inactive_listing": "Listing not active",
        "already_correct": "Already correct",
        "fba_listing": "Stored at Amazon (FBA)",
        "blacklisted": "On the never-touch list",
        "below_min_change": "Change too small to send",
        "increases_disabled": "Raising quantities is switched off",
        "no_amazon_quantity": "Amazon quantity unknown",
    }.get(value, value.replace("_", " ").capitalize())


templates.env.filters["n"] = fmt_int
templates.env.filters["money"] = fmt_money
templates.env.filters["duration"] = fmt_duration
templates.env.filters["dt"] = fmt_datetime
templates.env.filters["ago"] = fmt_ago
templates.env.filters["bytes"] = fmt_bytes
templates.env.filters["tone"] = status_tone
templates.env.filters["label"] = status_label


# ===========================================================================
# The current user
# ===========================================================================

def current_session(request: Request) -> SessionData | None:
    """The signed-in user's session, or None."""
    return read_session(request.cookies.get(SESSION_COOKIE))


def require_login(request: Request) -> SessionData:
    """
    FastAPI dependency: a signed-in user, or a redirect to the login page.

    Raises a redirect rather than a 401 because these are browser pages, and a
    401 would show a bare error instead of the login form. ``next`` preserves
    where the user was going, so signing in returns them there.
    """
    from fastapi import HTTPException

    data = current_session(request)
    if data is None:
        raise HTTPException(
            status_code=307,
            headers={"Location": f"/login?next={request.url.path}"},
        )
    return data


def current_user(
    request: Request,
    session: Session = Depends(get_session),
) -> User | None:
    """The full user record, when a template needs more than the session."""
    data = current_session(request)
    if data is None:
        return None
    return session.get(User, data.user_id)


# ===========================================================================
# Rendering
# ===========================================================================

def context(
    request: Request,
    session: Session,
    **extra: Any,
) -> dict:
    """
    The base template context, present on every page.

    Assembled in one place so that every page gets the navigation, the pause
    state and the readiness banner without each router remembering to add them
    -- a page that silently lost the "system is paused" banner would be a
    genuine operational hazard.
    """
    from app import scheduler as sched
    from app.services import readiness

    cfg = settings_store.get_all(session)
    who = current_session(request)

    base = {
        "request": request,
        "app_name": settings.app_name,
        "version": __version__,
        "environment": settings.environment,
        "user": who,
        "cfg": cfg,
        "tz": cfg.get("timezone", "America/New_York"),
        "paused": bool(cfg.get("paused")),
        "sync_mode": cfg.get("sync_mode"),
        "path": request.url.path,
        "jobs": sched.status(),
        "readiness": readiness(session),
        "now": datetime.now(UTC),
    }
    base.update(extra)
    return base


def render(
    request: Request,
    session: Session,
    template: str,
    **extra: Any,
) -> HTMLResponse:
    """Render a template with the base context merged in."""
    return templates.TemplateResponse(
        request=request, name=template, context=context(request, session, **extra)
    )


def render_error(
    request: Request,
    *,
    status_code: int,
    heading: str,
    detail: str,
) -> HTMLResponse:
    """
    An error page that does not need a database session.

    Important: the 500 handler must work even when the database is the thing
    that broke, so this deliberately builds a minimal context by hand rather
    than calling :func:`context`.
    """
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        status_code=status_code,
        context={
            "request": request,
            "app_name": settings.app_name,
            "version": __version__,
            "environment": settings.environment,
            "status_code": status_code,
            "heading": heading,
            "detail": detail,
            "user": None,
            "paused": False,
            "readiness": None,
            "jobs": [],
            "path": request.url.path,
        },
    )


def redirect(to: str, *, flash: str | None = None, tone: str = "good") -> RedirectResponse:
    """
    Redirect after a POST, optionally with a message.

    The message travels in the query string rather than in a server-side flash
    store, which keeps the app stateless -- there is no session storage to
    manage, and a bookmarked URL with a stale message is harmless.
    """
    url = to
    if flash:
        from urllib.parse import quote

        joiner = "&" if "?" in to else "?"
        url = f"{to}{joiner}flash={quote(flash)}&tone={tone}"
    return RedirectResponse(url=url, status_code=303)


def client_ip(request: Request) -> str | None:
    """
    The caller's IP, honouring one layer of proxy.

    The recommended deployment puts the dashboard behind a Cloudflare Tunnel,
    so the socket address is the tunnel's. Only the first entry of
    ``X-Forwarded-For`` is trusted, and only one hop -- trusting the whole
    chain would let a caller forge any address they liked into the audit log.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:60]
    return request.client.host if request.client else None
