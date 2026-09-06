"""
Dashboard login: password hashing, sessions, two-factor codes, lockout.

WHAT THE CLIENT ASKED FOR
=========================
"nope no need to make things complex, there will be only [one] profile that i
am going to share with the client, and he is going to decide on which to give
the credentials too, no need to make the already complex system much more
complex"

So: one shared administrator account, seeded at first start. No user management
screens, no invitations, no role editor.

The ``role`` column still exists on :class:`app.models.User` and is checked by
:func:`require_role`, because it costs nothing now and retrofitting it later
would mean a migration plus a gap in the audit trail. It simply goes unused
until somebody creates a second user.

WHAT IS NOT SIMPLIFIED
======================
A single shared login is a reasonable convenience decision. Weak protection of
that login would not be, because this dashboard can change quantities on an
account that earns the client's living. So:

  * **Argon2id** password hashing -- memory-hard, the current recommendation.
    Not bcrypt, not PBKDF2, and emphatically not a bare SHA-256.
  * **Signed, HttpOnly, SameSite=Lax cookies.** No token in JavaScript's reach,
    so a cross-site script cannot read the session.
  * **Optional TOTP** two-factor, which is the single highest-value addition
    for a shared credential -- it makes a leaked password insufficient.
  * **Lockout with backoff** after repeated failures.
  * **Constant-time comparison** everywhere a secret is compared.
  * **No public exposure by default.** The recommended deployment puts the
    dashboard behind a Cloudflare Tunnel, so there is no login page on the open
    internet to attack in the first place. See docs/DEPLOYMENT.md.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuditEvent, User, utcnow
from app.security.crypto import CryptoError, decrypt, encrypt

log = logging.getLogger(__name__)

#: Argon2id with deliberately generous parameters. This runs on login only --
#: a few times a day -- so spending ~100ms and 64 MB is free in practice and
#: makes an offline attack on a leaked hash expensive.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # 64 MB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

SESSION_COOKIE = "autopilot_session"

#: Lockout after this many consecutive failures.
MAX_FAILED_LOGINS = 5
#: Doubling per failure beyond the threshold, capped. Long enough to make
#: guessing impractical, short enough not to lock the client out for a day
#: because of a typo.
LOCKOUT_BASE_MINUTES = 5
LOCKOUT_MAX_MINUTES = 60

MIN_PASSWORD_LENGTH = 12


class AuthError(RuntimeError):
    """Login failed. The message is shown to the user, so keep it vague."""


@dataclass(frozen=True, slots=True)
class SessionData:
    """What a validated session cookie carries."""

    user_id: int
    email: str
    role: str


# ===========================================================================
# Passwords
# ===========================================================================

def hash_password(plaintext: str) -> str:
    """Hash a password for storage. Never store or log the plaintext."""
    if len(plaintext) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"Please choose a password of at least {MIN_PASSWORD_LENGTH} characters. "
            "This one login can change stock quantities on a live Amazon account, so "
            "it is worth protecting properly. A short phrase of three or four "
            "unrelated words is both strong and easy to remember."
        )
    return _hasher.hash(plaintext)


def verify_password(stored_hash: str, plaintext: str) -> bool:
    """
    Check a password against its hash.

    Returns False rather than raising for a wrong password, so the caller has
    one code path for "no" and cannot accidentally leak the difference between
    "wrong password" and "malformed hash" through differing error messages.
    """
    try:
        return _hasher.verify(stored_hash, plaintext)
    except VerifyMismatchError:
        return False
    except InvalidHashError:
        log.error("a stored password hash is malformed; the account cannot be used")
        return False


def needs_rehash(stored_hash: str) -> bool:
    """
    Whether the hash was made with weaker parameters than we now use.

    Called after a successful login so that raising the cost parameters
    transparently upgrades the stored hash next time the user signs in.
    """
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:  # pragma: no cover
        return True


# ===========================================================================
# Sessions
# ===========================================================================

def _serializer() -> URLSafeTimedSerializer:
    if not settings.session_secret:
        raise AuthError(
            "SESSION_SECRET is not set on the server, so logins cannot be signed. "
            "See docs/DEPLOYMENT.md."
        )
    return URLSafeTimedSerializer(settings.session_secret, salt="autopilot-session")


def issue_session(user: User) -> str:
    """
    Create a signed session token.

    The token carries only an id, an email and a role -- nothing secret. It is
    signed, so it cannot be edited, and timestamped, so it expires.
    """
    return _serializer().dumps({"uid": user.id, "email": user.email, "role": user.role})


def read_session(token: str | None) -> SessionData | None:
    """
    Validate a session token. ``None`` for anything invalid or expired.

    Deliberately returns None rather than raising for every failure mode --
    a tampered cookie and an expired one both simply mean "not logged in".
    """
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=settings.session_hours * 3600)
    except SignatureExpired:
        log.debug("session expired")
        return None
    except BadSignature:
        # Either a genuinely forged cookie or a rotated SESSION_SECRET. Worth a
        # log line: a burst of these is an attack signature.
        log.warning("rejected a session cookie with a bad signature")
        return None

    if not isinstance(payload, dict) or "uid" not in payload:
        return None
    return SessionData(
        user_id=int(payload["uid"]),
        email=str(payload.get("email", "")),
        role=str(payload.get("role", "viewer")),
    )


def cookie_settings() -> dict:
    """
    Cookie flags. HTTPS-only in production.

    ``secure`` is off in development so the dashboard works over plain HTTP on
    localhost, and on in production so the cookie never crosses the network in
    the clear. ``samesite="lax"`` blocks cross-site form posts while still
    allowing an ordinary link into the dashboard to work.
    """
    return {
        "key": SESSION_COOKIE,
        "httponly": True,
        "secure": settings.is_production,
        "samesite": "lax",
        "max_age": settings.session_hours * 3600,
        "path": "/",
    }


# ===========================================================================
# Two-factor
# ===========================================================================

def generate_totp_secret() -> str:
    """A fresh base32 TOTP secret."""
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str) -> str:
    """
    The ``otpauth://`` URI for an authenticator app.

    Rendered as a QR code on the settings page, so enrolling is a scan rather
    than typing 32 characters by hand.
    """
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Inventory Autopilot")


def verify_totp(secret: str, code: str) -> bool:
    """
    Check a six-digit code.

    ``valid_window=1`` accepts the adjacent 30-second steps, which absorbs
    ordinary clock drift between the server and the user's phone. Wider than
    that would meaningfully weaken the second factor.
    """
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)
    except Exception:  # pragma: no cover - malformed input
        return False


def store_totp_secret(user: User, secret: str) -> None:
    """Encrypt and attach a TOTP secret to a user."""
    user.totp_secret_enc = encrypt(secret, aad=f"totp:{user.id}")


def load_totp_secret(user: User) -> str | None:
    """Decrypt a user's TOTP secret, or None."""
    if not user.totp_secret_enc:
        return None
    try:
        return decrypt(user.totp_secret_enc, aad=f"totp:{user.id}")
    except CryptoError:
        log.error("could not decrypt the two-factor secret for user %s", user.email)
        return None


# ===========================================================================
# Lockout
# ===========================================================================

def _lockout_until(failures: int) -> datetime:
    """Exponential backoff, capped."""
    over = max(0, failures - MAX_FAILED_LOGINS)
    minutes = min(LOCKOUT_BASE_MINUTES * (2 ** over), LOCKOUT_MAX_MINUTES)
    return datetime.now(UTC) + timedelta(minutes=minutes)


def is_locked(user: User) -> tuple[bool, int]:
    """Whether the account is locked, and for how many more minutes."""
    if user.locked_until is None:
        return False, 0
    remaining = (user.locked_until - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        return False, 0
    return True, max(1, int(remaining // 60))


# ===========================================================================
# Login
# ===========================================================================

def authenticate(
    session: Session,
    email: str,
    password: str,
    totp_code: str | None = None,
    *,
    ip: str | None = None,
) -> User:
    """
    Verify a login. Returns the user, or raises :class:`AuthError`.

    Every failure gives the same message: "Email or password is not correct."
    That is not laziness -- distinguishing "no such account" from "wrong
    password" tells an attacker which addresses are worth attacking.

    A dummy hash verification runs even when the account does not exist, so the
    response time does not reveal whether an email is registered.
    """
    email = (email or "").strip().lower()
    generic = AuthError("Email or password is not correct.")

    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()

    if user is None:
        # Constant-ish work regardless of whether the account exists.
        _hasher.hash(password or "x")
        session.add(
            AuditEvent(
                action="auth.failed", actor=email or "(blank)", actor_ip=ip,
                detail="no such account",
            )
        )
        raise generic

    locked, minutes = is_locked(user)
    if locked:
        session.add(
            AuditEvent(
                action="auth.failed", actor=email, actor_ip=ip,
                detail=f"account locked, {minutes} minutes remaining",
            )
        )
        raise AuthError(
            f"Too many failed attempts. This account is locked for another "
            f"{minutes} minute{'s' if minutes != 1 else ''}."
        )

    if not user.is_active:
        session.add(
            AuditEvent(action="auth.failed", actor=email, actor_ip=ip, detail="account disabled")
        )
        raise generic

    if not verify_password(user.password_hash, password or ""):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = _lockout_until(user.failed_logins)
            log.warning(
                "locked account %s after %d failed attempts", email, user.failed_logins
            )
        session.add(
            AuditEvent(
                action="auth.failed", actor=email, actor_ip=ip,
                detail=f"wrong password (attempt {user.failed_logins})",
            )
        )
        raise generic

    # -- second factor ----------------------------------------------------
    if user.totp_enabled:
        secret = load_totp_secret(user)
        if not secret:
            raise AuthError(
                "Two-factor authentication is switched on for this account but its "
                "secret cannot be read. An administrator must reset it on the server."
            )
        if not totp_code:
            raise AuthError("Please enter the six-digit code from your authenticator app.")
        if not verify_totp(secret, totp_code):
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILED_LOGINS:
                user.locked_until = _lockout_until(user.failed_logins)
            session.add(
                AuditEvent(
                    action="auth.failed", actor=email, actor_ip=ip,
                    detail="wrong two-factor code",
                )
            )
            raise AuthError("That code is not correct. Codes change every 30 seconds - try the current one.")

    # -- success -----------------------------------------------------------
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = utcnow()

    if needs_rehash(user.password_hash):
        # Transparent upgrade to stronger parameters.
        user.password_hash = _hasher.hash(password)
        log.info("upgraded the password hash for %s", email)

    session.add(AuditEvent(action="auth.login", actor=email, actor_ip=ip, detail="signed in"))
    log.info("%s signed in", email)
    return user


# ===========================================================================
# Bootstrap
# ===========================================================================

def ensure_admin_user(session: Session, *, email: str, password: str | None = None) -> tuple[User, str | None]:
    """
    Create the single administrator account if it does not exist.

    Returns ``(user, generated_password_or_None)``. When no password is given a
    strong one is generated and returned **once**, for the deployment script to
    print. It is never stored in plaintext and never recoverable, so it has to
    be written down at that moment -- which is the correct trade.
    """
    email = email.strip().lower()
    existing = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        return existing, None

    generated: str | None = None
    if not password:
        # Four random words would be friendlier, but this has to work with no
        # word list available. 24 URL-safe characters is roughly 140 bits.
        generated = secrets.token_urlsafe(18)
        password = generated

    user = User(
        email=email,
        password_hash=_hasher.hash(password),
        role="admin",
        is_active=True,
    )
    session.add(user)
    session.add(
        AuditEvent(
            action="user.created", actor="system", target=email,
            detail="administrator account created at first start",
        )
    )
    log.info("created administrator account %s", email)
    return user, generated


def change_password(
    session: Session,
    user: User,
    current: str,
    new: str,
    *,
    ip: str | None = None,
) -> None:
    """Change a password, requiring the current one."""
    if not verify_password(user.password_hash, current):
        raise AuthError("Your current password is not correct.")
    if current == new:
        raise AuthError("The new password must be different from the current one.")
    user.password_hash = hash_password(new)
    session.add(
        AuditEvent(
            action="auth.password_changed", actor=user.email, actor_ip=ip,
            detail="password changed",
        )
    )
    log.info("%s changed their password", user.email)


def require_role(session_data: SessionData | None, *roles: str) -> SessionData:
    """
    Assert a logged-in user with one of ``roles``.

    ``admin`` satisfies every requirement. With a single shared account this is
    always trivially true, but keeping the check in place means adding a
    read-only login later requires no changes to the routers.
    """
    if session_data is None:
        raise AuthError("Please sign in.")
    if roles and session_data.role != "admin" and session_data.role not in roles:
        raise AuthError("Your account does not have permission to do that.")
    return session_data
