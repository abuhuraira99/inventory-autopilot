"""
Signing in and out.

Deliberately small. There is one shared account, as the client asked, so there
is no registration, no invitation flow and no password-reset email -- a reset
is a documented server-side command, which is more appropriate for a single
administrative login than a self-service flow that could be abused.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.routers.helpers import client_ip, redirect, templates
from app.security.auth import (
    SESSION_COOKIE,
    AuthError,
    authenticate,
    cookie_settings,
    issue_session,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    next: str = "/",
    error: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """The login page."""
    # Rendered without the usual base context: a not-yet-signed-in visitor
    # should not be shown the pause state, the readiness banner or the
    # navigation, because all of that is information about the account.
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "app_name": "Inventory Autopilot",
            "next": next if next.startswith("/") else "/",
            "error": error,
        },
    )


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    totp_code: str = Form(""),
    next: str = Form("/"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """
    Check a login and set the session cookie.

    Every failure returns the same generic message, and the error is passed in
    the query string rather than rendered inline so a refresh does not resubmit
    the password.
    """
    ip = client_ip(request)
    try:
        user = authenticate(session, email, password, totp_code or None, ip=ip)
    except AuthError as exc:
        session.commit()  # persist the failed-attempt counter and the audit row
        from urllib.parse import quote

        safe_next = next if next.startswith("/") else "/"
        return RedirectResponse(
            url=f"/login?next={quote(safe_next)}&error={quote(str(exc))}",
            status_code=303,
        )

    token = issue_session(user)
    session.commit()

    # Open redirects are a real vulnerability: an attacker-supplied absolute
    # URL here would let a phishing link pass through our domain. Only local
    # paths are honoured.
    destination = next if next.startswith("/") and not next.startswith("//") else "/"

    response = RedirectResponse(url=destination, status_code=303)
    response.set_cookie(value=token, **cookie_settings())
    return response


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Sign out by clearing the cookie."""
    response = redirect("/login?flash=You+have+been+signed+out")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/logout")
async def logout_get(request: Request) -> RedirectResponse:
    """
    Allow signing out with a plain link as well as a form post.

    Strictly a POST would be more correct, but "sign me out" is not a dangerous
    action to trigger accidentally, and a working link is friendlier than a
    method-not-allowed page for anyone who bookmarks it.
    """
    return await logout(request)
