"""
The settings screen: everything the client can change without a developer.

THIS PAGE IS THE ANSWER TO A REQUIREMENT
========================================
"how the customer is going to control or change any aspect of this system
dynamically without me to change something in the code"

Every field here comes from :data:`app.core.settings_store.SPECS`, so adding a
knob is three lines in one file and it appears here automatically -- with its
label, its help text, its type and its bounds. There is no second place to
update, and no chance of the form and the validation disagreeing.

TWO THINGS THE PAGE REFUSES TO DO
=================================
1. **Show a stored secret.** Credentials are write-only: you can replace one,
   and you can see its last four characters, and there is no code path that
   returns the value.

2. **Let anybody switch off "never send a price".** That setting is marked
   ``locked``, and :func:`app.core.settings_store.set_value` refuses a locked
   key from a web request. The real enforcement is in
   :mod:`app.amazon.guard`; the locked row exists so nobody can create a
   setting that appears to turn it off.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import settings_store
from app.core.settings_store import SPECS, SettingError
from app.db import get_session
from app.models import SkuOverride, User
from app.routers.helpers import client_ip, redirect, render, require_login
from app.security import credentials as creds
from app.security.auth import (
    AuthError,
    SessionData,
    change_password,
    generate_totp_secret,
    load_totp_secret,
    store_totp_secret,
    totp_provisioning_uri,
    verify_totp,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])

#: Section order and headings. Written for the client, so the page reads as a
#: sequence of decisions rather than an alphabetical dump of fields.
SECTIONS = [
    ("safety", "Safety", "What the system is allowed to do, and the emergency stop."),
    ("schedule", "Timing", "How often it checks, and which day counts as today."),
    ("scope", "What it may touch", "Which listings are in scope. Everything else is left alone."),
    ("quantity", "Quantity rules", "How the vendor's stock becomes the number Amazon shows."),
    ("guardrails", "Safety limits", "The checks that stop a run before it sends anything odd."),
    ("parsing", "Reading the vendor's files", "Change these only if the vendor changes their format."),
    ("reports", "Reports", "The five files your team downloads."),
    ("alerts", "Alerts", "Who hears about what."),
]


@router.get("/settings", response_class=HTMLResponse)
async def settings_form(
    request: Request,
    flash: str = "",
    tone: str = "good",
    error: str = "",
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> HTMLResponse:
    """Render every setting, grouped into sections."""
    values = settings_store.get_all(session)

    grouped: dict[str, list] = {}
    for spec in sorted(SPECS, key=lambda s: (s.sort_order, s.key)):
        grouped.setdefault(spec.category, []).append(spec)

    overrides = list(
        session.execute(
            select(SkuOverride).where(SkuOverride.active.is_(True)).order_by(SkuOverride.barcode)
        ).scalars()
    )

    user = session.get(User, who.user_id)

    return render(
        request,
        session,
        "settings.html",
        flash=flash,
        flash_tone=tone,
        error=error,
        sections=SECTIONS,
        grouped=grouped,
        values=values,
        credentials=creds.status(session),
        overrides=overrides,
        user_record=user,
        # Shown read-only, because these are deployment facts rather than
        # behaviour: changing them means editing the server's .env and
        # restarting, which is correct for values that identify the account.
        env_facts={
            "Vendor server": f"{app_settings.vendor_ftp_host or 'not set'}:{app_settings.vendor_ftp_port}",
            "Vendor mode": app_settings.vendor_ftp_mode,
            "Vendor username": app_settings.vendor_ftp_user or "not set",
            "Amazon marketplace": app_settings.marketplace_id,
            "Amazon endpoint": app_settings.sp_api_endpoint,
            "Seller ID": app_settings.seller_id or "not set",
            "Amazon Client ID": (
                app_settings.lwa_client_id[:44] + "…" if app_settings.lwa_client_id else "not set"
            ),
            "Mail server": app_settings.smtp_host or "not set",
            "Environment": app_settings.environment,
        },
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Save the whole form.

    Validation is per field, and **all** fields are attempted even after one
    fails, so the client sees every problem at once rather than fixing them one
    reload at a time. Nothing is committed if anything failed -- a half-applied
    settings change would leave the system in a state nobody chose.
    """
    form = await request.form()
    ip = client_ip(request)

    errors: list[str] = []
    changed = 0

    for spec in SPECS:
        if spec.locked:
            continue  # never editable from the web

        if spec.value_type == "bool":
            # An unchecked checkbox is absent from the form, which is how HTML
            # works and why booleans need special handling.
            raw = spec.key in form
        else:
            if spec.key not in form:
                continue
            raw = form[spec.key]

        current = settings_store.get(session, spec.key)
        try:
            new = settings_store.set_value(
                session, spec.key, raw, actor=who.email, actor_ip=ip
            )
            if new != current:
                changed += 1
        except SettingError as exc:
            errors.append(str(exc))

    if errors:
        session.rollback()
        from urllib.parse import quote

        return RedirectResponse(
            url=f"/settings?error={quote(' | '.join(errors[:4]))}", status_code=303
        )

    session.commit()

    if changed == 0:
        return redirect("/settings", flash="Nothing changed.", tone="neutral")
    return redirect(
        "/settings",
        flash=f"Saved. {changed} setting{'s' if changed != 1 else ''} changed.",
    )


# ===========================================================================
# Credentials
# ===========================================================================

@router.post("/settings/credentials")
async def save_credential(
    request: Request,
    key: str = Form(...),
    value: str = Form(...),
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Store one credential, encrypted.

    This is the route that makes "the developer never holds the secrets"
    practical: the client types their own values here, over HTTPS, and they are
    encrypted before the request finishes.
    """
    try:
        hint = creds.set_secret(
            session, key, value, actor=who.email, actor_ip=client_ip(request)
        )
    except creds.CredentialError as exc:
        session.rollback()
        from urllib.parse import quote

        return RedirectResponse(url=f"/settings?error={quote(str(exc))}#credentials", status_code=303)

    session.commit()
    label = creds.SPEC_BY_KEY[key].label
    return redirect(
        "/settings",
        flash=f"{label} saved (ends in {hint.replace('...', '')}). It is now encrypted and cannot be displayed again.",
    )


@router.post("/settings/credentials/delete")
async def delete_credential(
    request: Request,
    key: str = Form(...),
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """Remove a stored credential, falling back to the environment value."""
    try:
        creds.delete_secret(session, key, actor=who.email, actor_ip=client_ip(request))
    except creds.CredentialError as exc:
        session.rollback()
        from urllib.parse import quote

        return RedirectResponse(url=f"/settings?error={quote(str(exc))}", status_code=303)

    session.commit()
    return redirect("/settings", flash="Credential removed.", tone="warn")


@router.post("/settings/test-vendor")
async def test_vendor(
    request: Request,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Try the vendor connection and report back in plain language.

    Worth its own button: a typo in a password should be discovered here, in
    two seconds, rather than as a failed scheduled run at 3am.
    """
    from app.services import vendor_credentials
    from app.vendor.ftp_client import test_connection

    credentials = vendor_credentials(session)
    if credentials is None:
        return redirect(
            "/settings",
            flash=(
                "The vendor connection is not fully configured. It needs a host, a "
                "username and a password."
            ),
            tone="warn",
        )

    ok, message, files = test_connection(credentials)
    return redirect("/settings", flash=message, tone="good" if ok else "bad")


@router.post("/settings/test-amazon")
async def test_amazon(
    request: Request,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Check the Amazon credentials and permissions without changing anything.

    Uses a read-only call, so it is safe to press at any time -- including
    before the client is ready to let the system write anything.
    """
    from app.amazon.client import preflight
    from app.services import amazon_client

    client = amazon_client(session, force_dry_run=True)
    if client is None:
        return redirect(
            "/settings",
            flash=(
                "Amazon is not fully configured. It needs the Client ID, the Client "
                "Secret, the Refresh Token and the Seller ID."
            ),
            tone="warn",
        )

    try:
        ok, message = preflight(client)
    finally:
        client.close()

    return redirect("/settings", flash=message, tone="good" if ok else "bad")


# ===========================================================================
# Manual SKU mappings
# ===========================================================================

@router.post("/settings/override")
async def add_override(
    request: Request,
    barcode: str = Form(...),
    seller_sku: str = Form(...),
    note: str = Form(""),
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Add a manual barcode-to-SKU mapping.

    For the cases the automatic rules cannot reach. The barcode is normalised
    on the way in, so an operator can paste it in whichever form they have it
    and the mapping still matches.
    """
    from app.core.barcode import normalise

    bc = normalise(barcode)
    if not bc.usable:
        return redirect(
            "/settings",
            flash=f"{barcode!r} is not a usable barcode, so no mapping was added.",
            tone="bad",
        )

    sku = seller_sku.strip()
    if not sku:
        return redirect("/settings", flash="A SKU is required.", tone="bad")

    existing = session.get(SkuOverride, bc.canonical)
    if existing is not None:
        existing.seller_sku = sku
        existing.note = note.strip()
        existing.active = True
        existing.created_by = who.email
    else:
        session.add(
            SkuOverride(
                barcode=bc.canonical,
                seller_sku=sku,
                note=note.strip(),
                created_by=who.email,
            )
        )

    from app.models import AuditEvent

    session.add(
        AuditEvent(
            action="override.added",
            actor=who.email,
            actor_ip=client_ip(request),
            target=bc.canonical,
            new_value={"seller_sku": sku},
            detail=note.strip(),
        )
    )
    session.commit()
    return redirect("/settings", flash=f"Mapping saved: {bc.canonical} to {sku}.")


@router.post("/settings/override/delete")
async def delete_override(
    request: Request,
    barcode: str = Form(...),
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """Remove a manual mapping."""
    row = session.get(SkuOverride, barcode)
    if row is not None:
        session.delete(row)
        from app.models import AuditEvent

        session.add(
            AuditEvent(
                action="override.removed",
                actor=who.email,
                actor_ip=client_ip(request),
                target=barcode,
            )
        )
        session.commit()
    return redirect("/settings", flash="Mapping removed.", tone="warn")


# ===========================================================================
# The account
# ===========================================================================

@router.post("/settings/password")
async def update_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """Change the dashboard password."""
    user = session.get(User, who.user_id)
    if user is None:  # pragma: no cover
        return redirect("/login", flash="Please sign in again.", tone="warn")

    if new_password != confirm_password:
        return redirect("/settings", flash="The two new passwords do not match.", tone="bad")

    try:
        change_password(session, user, current_password, new_password, ip=client_ip(request))
    except AuthError as exc:
        session.rollback()
        return redirect("/settings", flash=str(exc), tone="bad")

    session.commit()
    return redirect("/settings", flash="Password changed.")


@router.post("/settings/2fa/start")
async def start_2fa(
    request: Request,
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> HTMLResponse:
    """
    Begin two-factor enrolment.

    Worth doing even with one shared login -- arguably especially then, because
    a shared password is more likely to be written down somewhere. Two-factor
    makes a leaked password insufficient on its own.
    """
    user = session.get(User, who.user_id)
    if user is None:  # pragma: no cover
        return redirect("/login")

    secret = load_totp_secret(user) if user.totp_secret_enc else None
    if secret is None or not user.totp_enabled:
        secret = generate_totp_secret()
        store_totp_secret(user, secret)
        session.commit()

    return render(
        request,
        session,
        "twofactor.html",
        secret=secret,
        uri=totp_provisioning_uri(secret, user.email),
        enabled=user.totp_enabled,
    )


@router.post("/settings/2fa/confirm")
async def confirm_2fa(
    request: Request,
    code: str = Form(...),
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Turn on two-factor, once a code proves the app is set up correctly.

    Requiring a working code before enabling is essential: switching it on
    without proof would lock the only account out of the system.
    """
    user = session.get(User, who.user_id)
    if user is None:  # pragma: no cover
        return redirect("/login")

    secret = load_totp_secret(user)
    if not secret:
        return redirect("/settings", flash="Start the setup again.", tone="bad")

    if not verify_totp(secret, code):
        return redirect(
            "/settings",
            flash="That code is not correct. Codes change every 30 seconds - try the current one.",
            tone="bad",
        )

    user.totp_enabled = True
    from app.models import AuditEvent

    session.add(
        AuditEvent(
            action="auth.2fa_enabled",
            actor=user.email,
            actor_ip=client_ip(request),
            detail="two-factor authentication switched on",
        )
    )
    session.commit()
    return redirect("/settings", flash="Two-factor authentication is on. Keep your recovery method safe.")


@router.post("/settings/2fa/disable")
async def disable_2fa(
    request: Request,
    password: str = Form(...),
    session: Session = Depends(get_session),
    who: SessionData = Depends(require_login),
) -> RedirectResponse:
    """
    Turn two-factor off, requiring the password.

    Requiring the password matters: without it, anyone who got hold of an
    already-signed-in browser could remove the second factor and keep access.
    """
    from app.security.auth import verify_password

    user = session.get(User, who.user_id)
    if user is None:  # pragma: no cover
        return redirect("/login")

    if not verify_password(user.password_hash, password):
        return redirect("/settings", flash="That password is not correct.", tone="bad")

    user.totp_enabled = False
    user.totp_secret_enc = None
    from app.models import AuditEvent

    session.add(
        AuditEvent(
            action="auth.2fa_disabled",
            actor=user.email,
            actor_ip=client_ip(request),
            detail="two-factor authentication switched off",
        )
    )
    session.commit()
    return redirect("/settings", flash="Two-factor authentication is off.", tone="warn")
