"""
Storing and reading the four secrets, safely.

THE POLICY
==========
The system holds exactly four secrets:

    vendor_ftp_password    the vendor's FTP password
    lwa_client_secret      the Amazon app's client secret
    lwa_refresh_token      the Amazon refresh token
    smtp_password          optional, for sending alerts

And deliberately holds none of these:

    the Seller Central password        never needed
    any card details                  never needed
    customer names, addresses, orders  never needed, and the Amazon app is not
                                       granted the role that would allow it

THE RECOMMENDED FLOW
====================
The developer never receives any of them. Once the server is up, the client
types their own credentials into the dashboard over HTTPS, and this module
encrypts them immediately. Afterwards the interface can only show the last four
characters -- there is no code path that returns a stored secret to a browser.

Everything up to that point is built and tested against the sample feed files,
which contain no credentials at all.

WHY BOTH ENVIRONMENT AND DATABASE
=================================
Values may come from either. The database wins when both are present, because
that is where the client's own entry lands, and their choice should beat a
leftover value in a ``.env`` file. Environment variables remain useful for
automated deployment and for the initial bootstrap.

READING IS DELIBERATELY NARROW
==============================
:func:`get_secret` is the only way to obtain a plaintext, and it is called from
exactly two places: the vendor client and the Amazon token provider. Nothing
returns a secret to a template, a JSON response, or a log line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuditEvent, Credential
from app.security.crypto import CryptoError, decrypt, encrypt, hint_of

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SecretSpec:
    """Declaration of one secret: where it comes from and how to describe it."""

    key: str
    label: str
    help_text: str
    #: Matching environment variable, used when the database has no row.
    env_attr: str | None = None
    required_for: str = ""


#: Every secret the system knows about. The help text is written for the
#: client, because it appears next to the field they will type into.
SECRET_SPECS: tuple[SecretSpec, ...] = (
    SecretSpec(
        key="vendor_ftp_password",
        label="Vendor FTP password",
        help_text=(
            "The password for the vendor's file server. For All Media Supply this "
            "For a vendor offering explicit FTP over TLS this is the password that "
            "goes with the username on port 21."
        ),
        env_attr="vendor_ftp_password",
        required_for="downloading feed files",
    ),
    SecretSpec(
        key="lwa_client_secret",
        label="Amazon app Client Secret",
        help_text=(
            "Seller Central -> Apps and Services -> Develop Apps -> your app -> "
            "LWA credentials -> View. This is the one credential that was not "
            "supplied with the others, and nothing can talk to Amazon without it."
        ),
        env_attr="lwa_client_secret",
        required_for="every Amazon call",
    ),
    SecretSpec(
        key="lwa_refresh_token",
        label="Amazon Refresh Token",
        help_text=(
            "Seller Central -> Apps and Services -> Develop Apps -> your app -> "
            "Authorize -> Authorize app. It is about 400 characters long and starts "
            "with 'Atzr|'. Important: changing the app's roles invalidates the old "
            "token, so if you have just corrected the permissions you need a new one."
        ),
        env_attr="lwa_refresh_token",
        required_for="every Amazon call",
    ),
    SecretSpec(
        key="smtp_password",
        label="Email password",
        help_text="Only needed if you want the system to send alert emails.",
        env_attr="smtp_password",
        required_for="sending alerts",
    ),
)

SPEC_BY_KEY = {s.key: s for s in SECRET_SPECS}


class CredentialError(RuntimeError):
    """A credential could not be stored or read. Message is user-facing."""


# ===========================================================================
# Write
# ===========================================================================

def set_secret(
    session: Session,
    key: str,
    plaintext: str,
    *,
    actor: str = "system",
    actor_ip: str | None = None,
) -> str:
    """
    Encrypt and store one secret. Returns the display hint (last 4 characters).

    The audit event records that the credential changed, who changed it and
    when -- and never any part of the value itself, not even the hint, because
    an audit log is exactly the kind of thing that gets exported and shared.
    """
    spec = SPEC_BY_KEY.get(key)
    if spec is None:
        raise CredentialError(f"There is no credential called {key!r}.")

    plaintext = (plaintext or "").strip()
    if not plaintext:
        raise CredentialError(f"{spec.label} cannot be blank. To remove it, use Delete.")

    _sanity_check(spec, plaintext)

    try:
        # aad binds the ciphertext to this key name, so a row cannot be moved
        # to a different slot and still decrypt.
        blob = encrypt(plaintext, aad=key)
    except CryptoError as exc:
        raise CredentialError(
            f"Could not encrypt {spec.label}: {exc}. Check that MASTER_KEY is set "
            "correctly on the server."
        ) from exc

    hint = hint_of(plaintext)
    row = session.get(Credential, key)
    existed = row is not None

    if row is None:
        row = Credential(key=key, value_enc=blob, hint=hint, updated_by=actor)
        session.add(row)
    else:
        row.value_enc = blob
        row.hint = hint
        row.updated_by = actor

    session.add(
        AuditEvent(
            action="credential.updated",
            actor=actor,
            actor_ip=actor_ip,
            target=key,
            # No value, no hint. Only the fact that it changed.
            new_value={"changed": True, "was_set_before": existed},
            detail=f"{spec.label} was {'replaced' if existed else 'set'}",
        )
    )
    log.info("credential %s %s by %s", key, "replaced" if existed else "set", actor)
    return hint


def delete_secret(
    session: Session, key: str, *, actor: str = "system", actor_ip: str | None = None
) -> None:
    """
    Remove a stored secret.

    Whatever is in the environment then applies again, which is the desired
    behaviour: deleting a dashboard entry reverts to the deployment default
    rather than leaving the system with nothing.
    """
    spec = SPEC_BY_KEY.get(key)
    if spec is None:
        raise CredentialError(f"There is no credential called {key!r}.")

    row = session.get(Credential, key)
    if row is not None:
        session.delete(row)
        session.add(
            AuditEvent(
                action="credential.deleted",
                actor=actor,
                actor_ip=actor_ip,
                target=key,
                detail=f"{spec.label} was removed",
            )
        )
        log.warning("credential %s deleted by %s", key, actor)


# ===========================================================================
# Read
# ===========================================================================

def get_secret(session: Session, key: str) -> str:
    """
    The plaintext value, from the database if present, otherwise the environment.

    **The only function in the codebase that returns a decrypted secret.**
    Called from :mod:`app.vendor.ftp_client` and :mod:`app.amazon.lwa` and
    nowhere else. Never call it from a router, a template context, or anything
    that produces output.

    Returns ``""`` when the secret is not configured, rather than raising --
    the caller (see :func:`app.amazon.lwa.missing_credentials`) turns that into
    a clear, actionable message for the client, which is much more useful than
    a stack trace.
    """
    spec = SPEC_BY_KEY.get(key)
    if spec is None:
        raise CredentialError(f"There is no credential called {key!r}.")

    row = session.get(Credential, key)
    if row is not None:
        try:
            return decrypt(row.value_enc, aad=key)
        except CryptoError as exc:
            # The usual cause is a changed or lost MASTER_KEY. Say so plainly:
            # the fix is to re-enter the credential, not to debug the crypto.
            log.error("could not decrypt credential %s: %s", key, exc)
            raise CredentialError(
                f"{spec.label} is stored but cannot be decrypted. This almost always "
                "means MASTER_KEY has changed since it was saved. Re-enter the "
                "credential in Settings to fix it."
            ) from exc

    if spec.env_attr:
        return str(getattr(settings, spec.env_attr, "") or "")
    return ""


def get_hint(session: Session, key: str) -> str:
    """
    The display hint, safe to show anywhere.

    For a value that came from the environment there is no stored hint, so one
    is computed on the fly -- deliberately without keeping the plaintext.
    """
    row = session.get(Credential, key)
    if row is not None:
        return row.hint or "(set)"

    spec = SPEC_BY_KEY.get(key)
    if spec and spec.env_attr:
        value = str(getattr(settings, spec.env_attr, "") or "")
        return hint_of(value) if value else ""
    return ""


def status(session: Session) -> list[dict]:
    """
    A safe summary of every credential, for the settings page.

    Contains no secret material -- only whether each one is set, where it came
    from, when it changed and its last four characters.
    """
    stored = {
        row.key: row
        for row in session.execute(select(Credential)).scalars()
    }

    out: list[dict] = []
    for spec in SECRET_SPECS:
        row = stored.get(spec.key)
        from_env = bool(spec.env_attr and getattr(settings, spec.env_attr, ""))
        out.append(
            {
                "key": spec.key,
                "label": spec.label,
                "help_text": spec.help_text,
                "required_for": spec.required_for,
                "is_set": bool(row) or from_env,
                "source": "dashboard" if row else ("environment" if from_env else "not set"),
                "hint": get_hint(session, spec.key),
                "updated_at": row.updated_at.isoformat() if row else None,
                "updated_by": row.updated_by if row else None,
            }
        )
    return out


def missing_required(session: Session) -> list[str]:
    """
    Which required credentials are absent, described for the client.

    Drives the red banner on the dashboard. ``smtp_password`` is excluded --
    email is a convenience, and the system works without it.
    """
    gaps: list[str] = []
    for spec in SECRET_SPECS:
        if spec.key == "smtp_password":
            continue
        if not get_secret(session, spec.key):
            gaps.append(f"{spec.label} is not set - needed for {spec.required_for}.")
    return gaps


# ===========================================================================
# Sanity checks
# ===========================================================================

def _sanity_check(spec: SecretSpec, value: str) -> None:
    """
    Catch the obvious paste mistakes at entry, not at 3am on the first run.

    Every check below corresponds to something that genuinely happens: a
    truncated refresh token, the Application ID pasted where the Client ID
    belongs, a value copied with its label attached.
    """
    if spec.key == "lwa_refresh_token":
        if not value.startswith("Atzr|"):
            raise CredentialError(
                "That does not look like an Amazon refresh token. A refresh token "
                "starts with 'Atzr|'. If your value starts with 'amzn1.application-"
                "oa2-client.' then it is the Client ID, which belongs in a different "
                "field."
            )
        if len(value) < 200:
            raise CredentialError(
                f"That refresh token is only {len(value)} characters long. Amazon's "
                "refresh tokens are around 400 characters, so this one looks "
                "truncated - it is easy to lose the end when copying. Please copy it "
                "again, all of it."
            )

    if spec.key == "lwa_client_secret":
        # Amazon issues client secrets in two formats, and BOTH are valid:
        #
        #   modern  amzn1.oa2-cs.v1.<64 hex chars>
        #   legacy  <64 hex chars, no prefix>
        #
        # An earlier version of this check rejected anything starting "amzn1."
        # on the theory that only IDs carry that prefix. That was wrong, and it
        # would have refused the client's real secret outright -- the credential
        # could not have been entered at all. Amazon's own LWA credentials
        # dialog shows the modern secret with the "amzn1.oa2-cs.v1." prefix.
        # Only the two prefixes below are genuinely IDs rather than secrets.
        if value.startswith("amzn1.application-oa2-client."):
            raise CredentialError(
                "That is the Client ID, not the Client Secret. They sit next to each "
                "other on the LWA credentials screen, so this is an easy mix-up. The "
                "Client ID goes in the LWA_CLIENT_ID setting; the Secret is the value "
                "hidden behind the eye icon underneath it, and it starts with "
                "'amzn1.oa2-cs.'"
            )
        if value.startswith("amzn1.sp.solution."):
            raise CredentialError(
                "That is the Application ID, not the Client Secret. The Application ID "
                "identifies the app in Developer Central and is not used by this "
                "system at all. The Secret is on the same screen under 'LWA "
                "credentials' and starts with 'amzn1.oa2-cs.'"
            )
        if len(value) < 20:
            raise CredentialError(
                f"That secret is only {len(value)} characters. Amazon's client secrets "
                "are much longer - either 'amzn1.oa2-cs.v1.' followed by 64 characters, "
                "or 64 characters on their own. Please check it was copied in full."
            )

    if ":" in value and value.split(":", 1)[0].lower() in {
        "password", "secret", "token", "refresh token", "client secret",
    }:
        raise CredentialError(
            "It looks like the field label was copied along with the value. Please "
            "paste only the value itself, without the 'password:' or 'token:' part."
        )
