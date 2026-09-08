"""
Encryption for stored secrets, and log redaction.

WHAT IS PROTECTED AND WHY
=========================
The system holds exactly four secrets:

  1. the vendor FTP password
  2. the Amazon Login-with-Amazon client secret
  3. the Amazon refresh token
  4. (optionally) the SMTP password

It deliberately holds *nothing else*: no Seller Central password, no card
details, no customer names or addresses, no order or financial data. That is a
design decision, not an accident -- the Amazon app is not granted the
personal-data role, so even a total compromise of this server gives an attacker
no route to buyer information.

THREAT MODEL
------------
The realistic threat is not a cryptanalytic attack; it is a leaked database
dump -- a backup copied to the wrong place, a `pg_dump` pasted into a chat, a
stolen disk image. So the requirement is: **a database dump on its own must be
useless.**

That is achieved by keeping the key material outside the database entirely.
``MASTER_KEY`` lives in the environment (or the host's secret store). Ciphertext
lives in the ``credentials`` table. Neither half is sufficient.

We use AES-256-GCM: authenticated encryption, so tampering with the ciphertext
is detected rather than silently producing garbage plaintext. Each value gets a
fresh random 96-bit nonce -- never reused, which is the one rule you must not
break with GCM.

Per-record ``aad`` (additional authenticated data) binds a ciphertext to its
key name, so an attacker with write access to the database cannot swap the
FTP password ciphertext into the refresh-token row and learn anything.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

log = logging.getLogger(__name__)

#: AES-256 needs a 32-byte key.
KEY_BYTES = 32
#: GCM's standard nonce length. 12 bytes is the size the construction is
#: designed for; other lengths are legal but weaken the security proof.
NONCE_BYTES = 12

#: Version tag on every ciphertext so the format can change later without
#: making existing rows unreadable.
_PREFIX = "v1"


class CryptoError(RuntimeError):
    """Raised when a secret cannot be encrypted or decrypted."""


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------

def _decode_key(raw: str) -> bytes:
    """
    Accept the master key as base64, hex, or raw text.

    Being permissive here is deliberate: an operator copying a key out of a
    password manager should not have to know which encoding we wanted. Anything
    that is not exactly 32 bytes after decoding is stretched with SHA-256 so a
    short passphrase still produces a full-length key -- but we warn, because a
    human-chosen passphrase has far less entropy than 32 random bytes.
    """
    raw = (raw or "").strip()
    if not raw:
        raise CryptoError(
            "MASTER_KEY is empty. Generate one with:\n"
            '  python -c "import secrets,base64; '
            'print(base64.b64encode(secrets.token_bytes(32)).decode())"'
        )

    # base64 (the documented form)
    try:
        candidate = base64.b64decode(raw, validate=True)
        if len(candidate) == KEY_BYTES:
            return candidate
    except (binascii.Error, ValueError):
        pass

    # hex
    try:
        candidate = bytes.fromhex(raw)
        if len(candidate) == KEY_BYTES:
            return candidate
    except ValueError:
        pass

    log.warning(
        "MASTER_KEY is not 32 decoded bytes; deriving a key from it with SHA-256. "
        "This works, but a randomly generated 32-byte key is stronger. "
        "See docs/SECURITY.md."
    )
    return hashlib.sha256(raw.encode("utf-8")).digest()


_key_cache: bytes | None = None


def _key() -> bytes:
    """The process master key, decoded once and cached."""
    global _key_cache
    if _key_cache is None:
        _key_cache = _decode_key(settings.master_key)
    return _key_cache


def generate_master_key() -> str:
    """A fresh base64 master key. Used by the setup script and the docs."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


# ---------------------------------------------------------------------------
# Encrypt / decrypt
# ---------------------------------------------------------------------------

def encrypt(plaintext: str, *, aad: str = "") -> str:
    """
    Encrypt ``plaintext``, returning ``"v1:<b64 nonce>:<b64 ciphertext>"``.

    ``aad`` should be the credential's key name. It is authenticated but not
    encrypted, and it binds the ciphertext to its slot: moving a ciphertext to
    a different row makes decryption fail loudly instead of succeeding quietly.
    """
    if plaintext is None:
        raise CryptoError("refusing to encrypt None")
    nonce = os.urandom(NONCE_BYTES)
    aesgcm = AESGCM(_key())
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8") or None)
    return f"{_PREFIX}:{base64.b64encode(nonce).decode()}:{base64.b64encode(ct).decode()}"


def decrypt(blob: str, *, aad: str = "") -> str:
    """
    Reverse :func:`encrypt`.

    Raises :class:`CryptoError` on a wrong key, a tampered ciphertext, or a
    mismatched ``aad``. The error message deliberately never includes any part
    of the ciphertext or the key.
    """
    if not blob:
        raise CryptoError("empty ciphertext")
    parts = blob.split(":", 2)
    if len(parts) != 3 or parts[0] != _PREFIX:
        raise CryptoError("unrecognised ciphertext format")
    try:
        nonce = base64.b64decode(parts[1])
        ct = base64.b64decode(parts[2])
    except (binascii.Error, ValueError) as exc:
        raise CryptoError("ciphertext is not valid base64") from exc

    try:
        pt = AESGCM(_key()).decrypt(nonce, ct, aad.encode("utf-8") or None)
    except InvalidTag as exc:
        raise CryptoError(
            "could not decrypt: wrong MASTER_KEY, altered data, or the value was "
            "stored under a different key name"
        ) from exc
    return pt.decode("utf-8")


def hint_of(plaintext: str) -> str:
    """
    A safe display fragment: the last four characters.

    Shown in the dashboard as "ends in 2c7" so an operator can confirm which
    credential is loaded without the value ever leaving the server. Values
    shorter than eight characters return only a length, because "last four of
    six" would give away most of the secret.
    """
    if not plaintext:
        return ""
    if len(plaintext) < 8:
        return f"({len(plaintext)} chars)"
    return f"...{plaintext[-4:]}"


def constant_time_equals(a: str, b: str) -> bool:
    """Timing-safe string comparison, for anything token-shaped."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# Log redaction
# ---------------------------------------------------------------------------
# A secret that reaches a log file has escaped. Logs get copied into tickets,
# pasted into chats, and shipped to third-party aggregators. So we scrub at the
# logging layer as well as being careful at every call site -- defence in depth,
# because "being careful" fails eventually.

#: Patterns for things that must never appear in a log line. Ordered most
#: specific first so a refresh token is not partially matched by a generic rule.
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Amazon refresh token: starts Atzr| and runs for hundreds of characters
    (re.compile(r"Atzr\|[A-Za-z0-9_\-|.]+"), "Atzr|<REDACTED>"),
    # Amazon access token
    (re.compile(r"Atza\|[A-Za-z0-9_\-|.]+"), "Atza|<REDACTED>"),
    # LWA client id / secret and application ids
    (re.compile(r"amzn1\.application-oa2-client\.[0-9a-f]{8,}"), "amzn1.application-oa2-client.<REDACTED>"),
    (re.compile(r"amzn1\.sp\.solution\.[0-9a-f-]{8,}"), "amzn1.sp.solution.<REDACTED>"),
    # Bearer / x-amz-access-token headers however they are formatted
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-|.]{20,}"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(x-amz-access-token[\"'\s:=]+)[A-Za-z0-9_\-|.]{20,}"), r"\1<REDACTED>"),
    # Anything that names itself a secret in a key/value context
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|client_secret|refresh_token|access_token|"
            r"api_key|apikey|token|master_key|session_secret)\b"
            r"(\s*[:=]\s*|\"\s*:\s*\"?)([^\s,;\"'})\]]+)"
        ),
        r"\1\2<REDACTED>",
    ),
    # FTP URLs carrying inline credentials
    (re.compile(r"(?i)\b(ftps?|sftp)://([^:/@\s]+):([^@\s]+)@"), r"\1://\2:<REDACTED>@"),
]


def redact(text: str) -> str:
    """
    Remove anything secret-shaped from ``text``.

    Cheap enough to run on every log record. Not a substitute for not logging
    secrets -- it is the net under the tightrope.

    >>> redact("token=Atzr|IwEBIabcdefghijklmnop")
    'token=<REDACTED>'
    >>> redact('{"client_secret": "abc123def456"}')
    '{"client_secret": "<REDACTED>"}'
    >>> redact("connecting to ftps://user:hunter2@ftp.example.com")
    'connecting to ftps://user:<REDACTED>@ftp.example.com'
    """
    if not text:
        return text
    out = text
    for pattern, replacement in _REDACT_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


class RedactingFilter(logging.Filter):
    """
    Logging filter that scrubs every record before it is emitted.

    Installed on the root logger in :mod:`app.logging_setup`, so it applies to
    application logs, uvicorn access logs and third-party library logs alike.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        # Interpolate FIRST, redact the finished line, then drop the arguments.
        #
        # WHY IN THIS ORDER, AND NOT SEPARATELY
        # =====================================
        # Redacting the template and the arguments independently looks
        # equivalent to this and is not. The "names itself a secret" rule in
        # _REDACT_PATTERNS matches `password : %s` and replaces the *value*
        # part -- which in a template is the format placeholder itself. So the
        # template came out of redaction one `%s` short while record.args still
        # held every argument, and the record then died inside the handler with
        # "not all arguments converted during string formatting".
        #
        # That failure inverted the whole point of this filter. Python's logging
        # error path prints the unformatted `Message:` and then `Arguments:`,
        # and the arguments tuple still contained the raw secret -- so the
        # message was destroyed AND the secret was printed. Twice as bad as not
        # redacting at all.
        #
        # It also silently broke the single most important log line on a new
        # install: the banner carrying the generated administrator password,
        # which is emitted exactly once, is stored nowhere, and cannot be
        # recovered. Observed on the first real deployment.
        #
        # Interpolating first means the patterns see real values instead of
        # placeholders. Format specifiers keep working -- `%d` included, which a
        # previous version of this method broke by calling str() on every
        # argument -- and a secret is caught wherever it came from, the template
        # or an argument, because by then there is only one string.
        try:
            rendered = record.getMessage()
        except Exception:
            # A broken format string is the caller's bug and will surface in the
            # handler either way. Redact what can be redacted rather than
            # letting this filter become a second, deeper failure inside
            # logging -- the place where a failure is hardest to see.
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            return True

        record.msg = redact(rendered)
        record.args = ()
        return True
