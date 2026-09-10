"""
Fetching files from the vendor's server.

THE VENDOR'S ACTUAL SETUP
=========================
From the credentials the client supplied:

    host: ftp.vendor.example.com
    port: 21  --  "Explicit FTP over TLS"
    user: <supplied by the vendor>

"Explicit FTP over TLS" is FTPS in explicit mode: the connection opens as
plaintext on port 21 and is then upgraded with an ``AUTH TLS`` command before
the password is sent. That is genuinely encrypted -- it is NOT the same as
plain FTP, which would put the password on the wire in the clear.

Python's :class:`ftplib.FTP_TLS` speaks exactly this. The one thing it does not
do by default is reuse the control connection's TLS session for the data
connection, which many servers now require; :class:`_ReusingFTP_TLS` below
fixes that. Without it, directory listings fail with a confusing
"unexpected EOF" that looks like a network problem.

WHY THE MODES ARE EXPLICIT
==========================
``ftp`` (plaintext) is refused unless the operator deliberately sets
``ALLOW_PLAINTEXT_FTP=true``. A misconfiguration must not be able to silently
downgrade the connection and leak the vendor password.

BEING A GOOD CITIZEN
====================
The client checked and believes there is no rate limiting, and we hold one
connection for one cycle rather than reconnecting per file. Even so:

  * one login per run, not per file
  * the connection is always closed, including on error
  * nothing is ever deleted or renamed on the vendor's server -- this client
    has no code path that can modify anything remotely
  * a passive-mode transfer, which is what works through NAT
"""

from __future__ import annotations

import ftplib
import logging
import ssl
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import certifi

if TYPE_CHECKING:
    # paramiko is imported lazily inside _SftpClient.open so that an install
    # using FTPS -- which is every install today -- never pays for it. The
    # annotations below still need the name, and `from __future__ import
    # annotations` means they are never evaluated at runtime.
    import paramiko

log = logging.getLogger(__name__)

#: Generous enough for a 27 MB download on a poor link, short enough that a
#: hung socket does not hold the run lock for the whole scheduling interval.
DEFAULT_TIMEOUT = 180


class VendorConnectionError(RuntimeError):
    """
    Could not reach or authenticate to the vendor.

    Always surfaced to the operator with the vendor's own message, because the
    difference between "wrong password", "server down" and "IP not allowed"
    determines who has to fix it.
    """


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """One entry in the vendor's directory."""

    name: str
    size: int | None
    #: Server-side modification time, from MLSD or MDTM. Recorded for the audit
    #: trail. Note that the *authoritative* date for the "today only" rule comes
    #: from the filename, not from here -- filenames carry an unambiguous
    #: calendar date, whereas this timestamp depends on the server's clock and
    #: timezone.
    modified_at: datetime | None


@dataclass(frozen=True, slots=True)
class VendorCredentials:
    """Everything needed to connect. Passwords are never logged."""

    host: str
    port: int
    username: str
    password: str
    #: "ftps" (explicit TLS, this vendor) | "sftp" | "ftp" (refused by default)
    mode: str = "ftps"
    remote_path: str = "/"
    timeout: int = DEFAULT_TIMEOUT

    def __repr__(self) -> str:  # pragma: no cover - safety net for logs
        return (
            f"VendorCredentials(host={self.host!r}, port={self.port}, "
            f"username={self.username!r}, password=<REDACTED>, mode={self.mode!r})"
        )


class _ReusingFTP_TLS(ftplib.FTP_TLS):
    """
    ``FTP_TLS`` that reuses the control session on the data channel.

    Many FTPS servers -- including a lot of managed hosting -- require the data
    connection to resume the TLS session negotiated on the control connection.
    Stock ``ftplib`` starts a fresh handshake, which those servers reject.
    The failure looks like a truncated transfer or an SSL EOF error, so it is
    worth fixing properly rather than retrying.

    This is the well-established workaround for CPython's ``ftplib``.
    """

    def ntransfercmd(self, cmd: str, rest: int | str | None = None):  # noqa: ANN201
        # noqa S321 below: the linter flags any ftplib call as insecure FTP. This
        # is the FTP_TLS path -- the connection is already TLS-protected by the
        # AUTH TLS handshake in open(), and this line only borrows the base
        # class's socket setup before wrapping the data channel in TLS as well.
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)  # noqa: S321
        if self._prot_p:  # type: ignore[attr-defined]
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=self.sock.session,  # type: ignore[union-attr]
            )
        return conn, size


@contextmanager
def connect(creds: VendorCredentials) -> Iterator[VendorClient]:
    """
    Open a connection, yield a client, and always close it.

    Usage::

        with connect(creds) as client:
            for f in client.list_files():
                ...
    """
    if creds.mode == "sftp":
        client: VendorClient = _SftpClient(creds)
    elif creds.mode == "ftps":
        client = _FtpsClient(creds)
    elif creds.mode == "ftp":
        client = _FtpsClient(creds, use_tls=False)
    else:  # pragma: no cover - validated in config
        raise VendorConnectionError(f"unknown transfer mode {creds.mode!r}")

    try:
        client.open()
        yield client
    finally:
        client.close()


class VendorClient(Protocol):
    """
    Interface both transports implement. Read-only by design.

    A Protocol rather than a plain class with ``...`` bodies. As a plain class
    its methods silently returned None, so a transport that forgot to implement
    ``list_files`` would report "the vendor's folder is empty" -- which this
    system would read as "the vendor has withdrawn every product". A Protocol
    makes that a type error instead, and lets the tests substitute a fake
    without inheriting anything.

    Note what is absent: there is no delete, no rename, no upload. The vendor's
    server is treated as read-only at the level of the interface, so no amount
    of later carelessness can put a destructive call into a code path that runs
    against it.
    """

    def open(self) -> None: ...
    def close(self) -> None: ...
    def list_files(self, *, suffix: str = ".zip") -> list[RemoteFile]: ...
    def list_entries(self) -> list[tuple[str, str]]: ...
    def download(self, name: str, destination: Path) -> int: ...


# ---------------------------------------------------------------------------
# FTPS (this vendor)
# ---------------------------------------------------------------------------

def _tls_context() -> ssl.SSLContext:
    """
    The TLS context used for FTPS, trusting the certifi root bundle.

    WHY NOT ``ssl.create_default_context()`` ON ITS OWN
    ===================================================
    On Windows, the default context trusts whatever is physically sitting in
    the machine's certificate store. That store is not a fixed thing: a fresh
    Windows Server installation ships with a small set of roots and fetches the
    rest on demand, and that on-demand fetch happens for the Windows TLS stack,
    not for Python's. So Chrome on the same machine can load a site happily
    while Python refuses it.

    That is exactly what happened on the first real deployment. The vendor's
    certificate is valid and its chain is complete, but it chains up through
    "Sectigo Public Server Authentication Root R46" -- a root created in 2021,
    absent from a Windows Server 2016 store -- so every FTPS connection died
    with CERTIFICATE_VERIFY_FAILED / "unable to get local issuer certificate".
    Nothing was wrong with the vendor, the credentials or the code; the machine
    simply did not know that root.

    Meanwhile the Amazon half of the system worked first time, because httpx
    builds *its* default context from certifi rather than from the OS store.
    Two different trust stores in one application is the actual defect here.
    This makes both halves trust the same one -- a bundle that is pinned in
    requirements.txt, versioned with the code, and identical on every machine
    the system is ever deployed to.

    Verification is NOT weakened. certifi is the Mozilla root programme, the
    hostname check stays on, and a self-signed or expired certificate still
    fails loudly -- which remains the correct outcome, and should stay an
    explicit, documented decision rather than a silent default.
    """
    return ssl.create_default_context(cafile=certifi.where())


class _FtpsClient(VendorClient):
    """Explicit FTP over TLS, which is what All Media Supply provides."""

    #: How many unlabelled directory entries list_entries will probe. See
    #: _resolve_unknown_kinds for why it is capped at all.
    PROBE_LIMIT = 25

    def __init__(self, creds: VendorCredentials, *, use_tls: bool = True) -> None:
        self.creds = creds
        self.use_tls = use_tls
        self._ftp: ftplib.FTP | None = None

    def open(self) -> None:
        c = self.creds
        log.info(
            "connecting to vendor %s:%s as %s (mode=%s)",
            c.host, c.port, c.username, "ftps" if self.use_tls else "ftp-plaintext",
        )
        ftp: ftplib.FTP
        try:
            if self.use_tls:
                # Verifies the certificate chain and the hostname, against
                # the certifi roots rather than the machine's own store. See
                # _tls_context for why that distinction cost a deployment.
                context = _tls_context()
                ftp = _ReusingFTP_TLS(context=context, timeout=c.timeout)
                ftp.connect(host=c.host, port=c.port, timeout=c.timeout)
                ftp.auth()          # AUTH TLS: upgrade before sending the password
                ftp.login(c.username, c.password)
                ftp.prot_p()        # encrypt the data channel too
            else:
                # noqa below: the linter objects to plaintext FTP on principle,
                # and it is right to. This branch is unreachable unless the
                # operator has explicitly set ALLOW_PLAINTEXT_FTP=true; see
                # Settings.startup_problems() and services.vendor_credentials().
                ftp = ftplib.FTP(timeout=c.timeout)  # noqa: S321
                ftp.connect(host=c.host, port=c.port, timeout=c.timeout)
                ftp.login(c.username, c.password)

            ftp.set_pasv(True)      # passive mode works through NAT
            if c.remote_path and c.remote_path not in ("/", ""):
                ftp.cwd(c.remote_path)
            self._ftp = ftp
            log.info("vendor connection established; working directory %s", ftp.pwd())

        except ftplib.error_perm as exc:
            # 5xx: the server understood us and said no. Almost always
            # credentials or permissions.
            raise VendorConnectionError(
                f"The vendor rejected the login for user {c.username!r}: {exc}. "
                "Check the username and password in Settings. If they are correct, "
                "ask the vendor whether the account is still active or whether they "
                "restrict connections by IP address."
            ) from exc
        except ssl.SSLError as exc:
            raise VendorConnectionError(
                f"The secure connection to {c.host} failed: {exc}. The vendor's "
                "certificate may have expired, or they may have changed their TLS "
                "settings."
            ) from exc
        except (TimeoutError, OSError, ftplib.all_errors) as exc:  # type: ignore[misc]
            raise VendorConnectionError(
                f"Could not reach {c.host}:{c.port} ({exc}). The vendor's server may "
                "be down, or this machine's outbound connection may be blocked."
            ) from exc

    def close(self) -> None:
        if self._ftp is None:
            return
        try:
            self._ftp.quit()      # polite: sends QUIT
        except Exception:
            # Closing a connection is best-effort by nature: the server may
            # already have dropped it. Failing to hang up politely must never
            # fail a run that has already done its work.
            with suppress(Exception):  # pragma: no cover
                self._ftp.close()     # rude: just drops the socket
        finally:
            self._ftp = None

    def list_files(self, *, suffix: str = ".zip") -> list[RemoteFile]:
        """
        List the directory, preferring MLSD for its machine-readable timestamps.

        MLSD is the modern command and gives size and mtime in a defined
        format. Older servers only support NLST, which returns bare names; in
        that case we ask for size and mtime per file, which is slower but works.
        """
        ftp = self._require()
        out: list[RemoteFile] = []

        try:
            for name, facts in ftp.mlsd():
                if facts.get("type") not in (None, "file"):
                    continue
                if suffix and not name.lower().endswith(suffix.lower()):
                    continue
                out.append(
                    RemoteFile(
                        name=name,
                        size=int(facts["size"]) if facts.get("size", "").isdigit() else None,
                        modified_at=_parse_mlsd_time(facts.get("modify")),
                    )
                )
            log.info("vendor directory listed via MLSD: %d matching files", len(out))
            return out
        except (ftplib.error_perm, ftplib.error_proto) as exc:
            log.info("MLSD unavailable (%s); falling back to NLST", exc)

        try:
            names = ftp.nlst()
        except ftplib.all_errors as exc:
            raise VendorConnectionError(f"Could not list the vendor directory: {exc}") from exc

        for raw in names:
            name = raw.rsplit("/", 1)[-1]
            if suffix and not name.lower().endswith(suffix.lower()):
                continue
            out.append(RemoteFile(name=name, size=self._size_of(name), modified_at=self._mtime_of(name)))
        log.info("vendor directory listed via NLST: %d matching files", len(out))
        return out

    def list_entries(self) -> list[tuple[str, str]]:
        """
        Everything in the current directory as ``(name, "dir" | "file")``.

        Diagnostic only -- nothing in a run calls this. It exists so that
        "connected, but there are no feed files here" can describe what *is*
        there, which on the first real deployment was the difference between a
        question the operator could act on and a dead end.

        WHY IT REPORTS EVERYTHING, NOT JUST THE SUBFOLDERS
        ==================================================
        The first version listed subfolders only, so its message could say "no
        zip files, and no subfolders either" -- which sounds conclusive and is
        not. A folder holding forty ``.csv`` files and no subfolders produces
        exactly that sentence. An unfiltered listing cannot mislead that way.

        WHY THE TYPE IS PROBED RATHER THAN TRUSTED
        ==========================================
        The second version trusted the listing's own type field and defaulted
        anything unlabelled to "file". On the real server that turned two
        folders into "it holds 2 other files: Bib, Invent" -- and "ask the
        vendor which of these files is the stock feed" is a slightly
        embarrassing question to send about two directories. ``MLSD`` need not
        supply ``type``, and the ``NLST`` fallback supplies nothing at all, so
        an unlabelled entry means *unknown*, never *file*.

        Unknown entries are resolved by trying to ``CWD`` into them and
        changing straight back, which is the one test that works on every FTP
        server regardless of what it supports. It is read-only, it costs two
        commands per unknown entry, and it only ever runs when the operator has
        pressed the test button and there was nothing to report -- so the
        happy path pays nothing, and the confusing path gets a real answer.

        Returns an empty list rather than raising. A server that will not
        enumerate its own directory is a worse message, not a failed
        connection test -- the connection plainly worked, or we would not have
        got this far.
        """
        ftp = self._require()
        entries: list[tuple[str, str]] = []
        try:
            for name, facts in ftp.mlsd():
                if name in (".", ".."):
                    continue
                declared = facts.get("type")
                if declared == "dir":
                    kind = "dir"
                elif declared == "file":
                    kind = "file"
                else:
                    kind = "unknown"
                entries.append((name, kind))
        except Exception as exc:
            # NLST is the fallback for the same reason list_files has one:
            # MLSD is optional. It gives bare names and no types at all, so
            # every entry starts out unknown and gets probed below.
            log.info("MLSD unavailable for the diagnostic listing (%s); trying NLST", exc)
            try:
                entries = [
                    (raw.rsplit("/", 1)[-1], "unknown")
                    for raw in ftp.nlst()
                    if raw.rsplit("/", 1)[-1] not in (".", "..")
                ]
            except Exception as exc2:  # pragma: no cover - diagnostic path
                log.info("could not list the directory at all (%s)", exc2)
                return []

        return sorted(self._resolve_unknown_kinds(entries))

    def _resolve_unknown_kinds(
        self, entries: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """
        Decide "dir" or "file" for entries the server did not label.

        Capped at PROBE_LIMIT. Past that the answer is already obvious from the
        ones that were probed, and a folder of nine hundred unlabelled entries
        should not turn a button press into eighteen hundred commands against a
        vendor who asked to be polled gently. Anything left over is reported as
        a file, which is what an unlabelled entry in a large listing almost
        always is.
        """
        ftp = self._require()
        unknown = [name for name, kind in entries if kind == "unknown"]
        if not unknown:
            return entries

        try:
            origin = ftp.pwd()
        except Exception as exc:  # pragma: no cover - diagnostic path
            log.info("cannot read the working directory (%s); not probing types", exc)
            return [(name, "file" if kind == "unknown" else kind) for name, kind in entries]

        resolved: dict[str, str] = {}
        for name in unknown[: self.PROBE_LIMIT]:
            try:
                ftp.cwd(name)
            except Exception:
                resolved[name] = "file"
                continue
            resolved[name] = "dir"
            try:
                ftp.cwd(origin)
            except Exception as exc:  # pragma: no cover - diagnostic path
                # Left somewhere else on the server. Stop probing rather than
                # report types measured from the wrong directory, and say so:
                # a silently wrong listing is what this whole method exists to
                # stop producing.
                log.warning("could not return to %s after probing %s (%s)", origin, name, exc)
                break

        return [
            (name, resolved.get(name, "file") if kind == "unknown" else kind)
            for name, kind in entries
        ]

    def download(self, name: str, destination: Path) -> int:
        """
        Download one file. Returns the number of bytes written.

        Writes to ``<destination>.part`` and renames on success, so a partial
        download can never be mistaken for a complete file -- not even if the
        process is killed mid-transfer. The caller then verifies the archive's
        CRC before parsing, giving two independent defences against the
        truncated-file failure that would otherwise zero the catalogue.
        """
        ftp = self._require()
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")

        written = 0
        try:
            with part.open("wb") as fh:
                def _chunk(block: bytes) -> None:
                    nonlocal written
                    fh.write(block)
                    written += len(block)

                # 1 MB blocks: fewer syscalls than the 8 KB default, which
                # matters on a 27 MB file.
                ftp.retrbinary(f"RETR {name}", _chunk, blocksize=1024 * 1024)

            if written == 0:
                raise VendorConnectionError(f"{name} downloaded as 0 bytes; treating as a failure")

            part.replace(destination)   # atomic on the same filesystem
            log.info("downloaded %s (%s bytes)", name, f"{written:,}")
            return written

        except VendorConnectionError:
            part.unlink(missing_ok=True)
            raise

        except (ftplib.Error, OSError, EOFError) as exc:
            # WRAPPED, NOT RE-RAISED RAW. The caller catches
            # VendorConnectionError so it can quarantine one file and carry on
            # with the rest of the run; a bare ConnectionResetError sails past
            # that handler and destroys the whole run instead.
            #
            # That is not theoretical. The vendor closes an idle control
            # connection, and the connection sits idle for as long as the
            # previous file takes to process -- seventeen minutes for the
            # 1.15-million-row full feed. So the download of the NEXT file
            # raised "[Errno 10054] An existing connection was forcibly closed
            # by the remote host", the run failed, and the full feed that had
            # just been read successfully was rolled back with it. Every day,
            # on the one file the whole system depends on.
            #
            # Losing a small delta file here costs nothing: it is not marked
            # processed, so the next run collects it. Losing the full feed
            # because of it costs the entire catalogue.
            part.unlink(missing_ok=True)
            raise VendorConnectionError(
                f"The connection to {self.creds.host} was lost while downloading "
                f"{name} ({type(exc).__name__}: {exc}). This file will be retried "
                f"on the next run; anything already read in this run is kept."
            ) from exc

        except Exception:
            part.unlink(missing_ok=True)
            raise

    # -- internals ---------------------------------------------------------
    def _require(self) -> ftplib.FTP:
        if self._ftp is None:
            raise VendorConnectionError("not connected; call open() first")
        return self._ftp

    def _size_of(self, name: str) -> int | None:
        try:
            return self._require().size(name)
        except Exception:  # pragma: no cover - server may not support SIZE
            return None

    def _mtime_of(self, name: str) -> datetime | None:
        try:
            resp = self._require().sendcmd(f"MDTM {name}")
        except Exception:  # pragma: no cover
            return None
        # "213 20260904071400"
        parts = resp.split()
        return _parse_mlsd_time(parts[1]) if len(parts) >= 2 else None


def _parse_mlsd_time(value: str | None) -> datetime | None:
    """
    Parse an FTP timestamp (``YYYYMMDDHHMMSS``) as UTC.

    RFC 3659 says MLSD and MDTM times are UTC, so we tag them as such rather
    than leaving a naive datetime to be misinterpreted later. Fractional
    seconds are tolerated and discarded.
    """
    if not value:
        return None
    v = value.strip().split(".")[0]
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# SFTP (not needed today, but one setting away)
# ---------------------------------------------------------------------------

class _SftpClient(VendorClient):
    """
    SSH file transfer.

    Not used by this vendor, who offers FTPS on port 21. It exists so that if
    the vendor ever moves to SFTP -- or a second vendor is added who uses it --
    that is a change to one dashboard setting rather than a code change and a
    deploy.
    """

    def __init__(self, creds: VendorCredentials) -> None:
        self.creds = creds
        self._transport: paramiko.Transport | None = None
        self._sftp: paramiko.SFTPClient | None = None

    def open(self) -> None:
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover
            raise VendorConnectionError("SFTP support needs the paramiko package") from exc

        c = self.creds
        log.info("connecting to vendor %s:%s as %s (mode=sftp)", c.host, c.port, c.username)
        try:
            transport = paramiko.Transport((c.host, c.port))
            transport.connect(username=c.username, password=c.password)
            self._transport = transport
            sftp = paramiko.SFTPClient.from_transport(transport)
            if sftp is None:
                # from_transport returns None rather than raising when the
                # channel cannot be opened. Without this check the next line
                # fails with "NoneType has no attribute chdir", which tells an
                # operator nothing about what to fix.
                raise VendorConnectionError(
                    f"Connected to {c.host} over SSH, but the SFTP subsystem could "
                    "not be opened. The account may not have SFTP enabled."
                )
            self._sftp = sftp
            if c.remote_path and c.remote_path not in ("/", ""):
                sftp.chdir(c.remote_path)
        except Exception as exc:
            raise VendorConnectionError(f"SFTP connection to {c.host} failed: {exc}") from exc

    def close(self) -> None:
        for obj in (self._sftp, self._transport):
            # Best-effort teardown, as above.
            with suppress(Exception):  # pragma: no cover
                if obj is not None:
                    obj.close()
        self._sftp = self._transport = None

    def list_files(self, *, suffix: str = ".zip") -> list[RemoteFile]:
        if self._sftp is None:
            raise VendorConnectionError("not connected")
        out = []
        for attr in self._sftp.listdir_attr("."):
            if suffix and not attr.filename.lower().endswith(suffix.lower()):
                continue
            out.append(
                RemoteFile(
                    name=attr.filename,
                    size=attr.st_size,
                    modified_at=(
                        datetime.fromtimestamp(attr.st_mtime, tz=UTC)
                        if attr.st_mtime
                        else None
                    ),
                )
            )
        return out

    def list_entries(self) -> list[tuple[str, str]]:
        """Everything in the current directory. See the FTPS version."""
        if self._sftp is None:
            raise VendorConnectionError("not connected")
        import stat

        entries: list[tuple[str, str]] = []
        try:
            for attr in self._sftp.listdir_attr("."):
                is_dir = attr.st_mode is not None and stat.S_ISDIR(attr.st_mode)
                entries.append((attr.filename, "dir" if is_dir else "file"))
        except Exception as exc:  # pragma: no cover - diagnostic path
            log.info("could not list the directory (%s)", exc)
            return []
        return sorted(entries)

    def download(self, name: str, destination: Path) -> int:
        if self._sftp is None:
            raise VendorConnectionError("not connected")
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")
        try:
            self._sftp.get(name, str(part))
            size = part.stat().st_size
            if size == 0:
                raise VendorConnectionError(f"{name} downloaded as 0 bytes")
            part.replace(destination)
            return size
        except Exception:
            part.unlink(missing_ok=True)
            raise


# ---------------------------------------------------------------------------
# Connection test, for the settings page
# ---------------------------------------------------------------------------

def _empty_folder_message(
    creds: VendorCredentials, entries: list[tuple[str, str]]
) -> str:
    """
    Explain a folder that connected but produced no feed files.

    "Connected, but empty" is the single most confusing result the test button
    can give: everything the operator typed was correct, the login succeeded,
    and the screen still says no. There is no error text to search for, so the
    message has to carry the whole diagnosis itself.

    Three genuinely different situations produce it, and conflating any two of
    them wastes somebody's afternoon:

      1. subfolders present     -- the feeds are in one of them; change .env
      2. other files present    -- the folder is NOT empty; the naming or the
                                   extension is not what we expect, which is a
                                   precise question for the vendor
      3. nothing at all         -- genuinely empty; a different question for
                                   the vendor, about this account and about how
                                   long files are kept

    The folder is ``VENDOR_FTP_PATH`` in ``.env``, NOT a field on the settings
    page. An earlier version of this message said "check the folder path in
    Settings" and sent the operator hunting through a page that has never had
    one.
    """
    here = creds.remote_path or "/"
    opening = (
        f"Connected to {creds.host} successfully, and signed in. "
        f"The folder {here} contains no zip files."
    )

    dirs = [name for name, kind in entries if kind == "dir"]
    others = [name for name, kind in entries if kind != "dir"]

    if dirs:
        listed = ", ".join(dirs[:12])
        more = f" (and {len(dirs) - 12} more)" if len(dirs) > 12 else ""
        target = f"{here.rstrip('/')}/{dirs[0]}"
        return (
            f"{opening} It does contain these subfolders: {listed}{more}. The feeds are "
            f"most likely inside one of them, so set VENDOR_FTP_PATH in the .env file to "
            f"that folder - for example VENDOR_FTP_PATH={target} - then restart and test "
            f"again."
        )

    if others:
        # Deliberately reports the count and real names. "No zip files" on its
        # own reads as "empty", and a folder full of files that simply are not
        # named the way we expect is the opposite of empty.
        listed = ", ".join(others[:8])
        more = f" (and {len(others) - 8} more)" if len(others) > 8 else ""
        return (
            f"{opening} It is not empty though - it holds {len(others)} other "
            f"file{'s' if len(others) != 1 else ''}: {listed}{more}. So the connection and "
            f"the folder are both right, and the file names are not what was expected. "
            f"Send that list to the vendor and ask which of these is the stock feed, and "
            f"whether the zip files go somewhere else."
        )

    return (
        f"{opening} The folder is completely empty - no files of any kind and no "
        f"subfolders. The connection, the sign-in and the folder are all working, so this "
        f"is a question for the vendor: should this account see feed files in {here}, and "
        f"how long are they kept before being removed?"
    )


def test_connection(creds: VendorCredentials) -> tuple[bool, str, list[RemoteFile]]:
    """
    Try to connect and list. Returns ``(ok, human message, files)``.

    Powers the "Test connection" button next to the vendor credentials, so the
    client gets an immediate, plain-language answer instead of discovering a
    typo when the next scheduled run fails.
    """
    try:
        with connect(creds) as client:
            files = client.list_files()
            # Only asked for when there is nothing to report, so a working
            # connection costs one command rather than two.
            entries = client.list_entries() if not files else []
        if not files:
            return True, _empty_folder_message(creds, entries), []

        # Narrowed to a list first so the key function cannot be handed a None
        # timestamp: MLSD is optional and some servers omit it entirely, in
        # which case max() would raise mid-comparison.
        timestamped = [(f.modified_at, f) for f in files if f.modified_at is not None]
        newest = max(timestamped)[1] if timestamped else None
        detail = f" The newest is {newest.name}." if newest else ""
        return True, f"Connected to {creds.host}. Found {len(files)} files.{detail}", files
    except VendorConnectionError as exc:
        return False, str(exc), []
    except Exception as exc:  # pragma: no cover - unexpected
        log.exception("unexpected error testing the vendor connection")
        return False, f"Unexpected problem connecting to {creds.host}: {exc}", []
