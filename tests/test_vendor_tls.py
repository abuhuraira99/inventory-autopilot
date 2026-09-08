"""
The FTPS trust store must come from the code, not from the machine.

WHY THIS FILE EXISTS
====================
The first real deployment could talk to Amazon perfectly and could not talk to
the vendor at all. The vendor's certificate was valid, its chain was complete,
and the credentials were right; Windows Server 2016 simply did not have the
root it chained up to in its certificate store, and Python trusts that store
rather than doing what a browser does and fetching the missing root on demand.

The fix was to trust the certifi bundle -- the same roots the Amazon side of
the system has always used, via httpx -- so that the answer to "is this
certificate trusted?" is a property of the release rather than a property of
whichever machine it was installed on.

These tests hold that in place, and hold the shape of the fix in place too: the
tempting way to make a certificate error go away is to switch verification off,
and that must never be how this problem is solved on a client's live seller
account.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import certifi

from app.vendor.ftp_client import _tls_context


def _subjects(context: ssl.SSLContext) -> set[str]:
    """The set of root subjects a context will accept, as comparable strings."""
    return {str(cert.get("subject")) for cert in context.get_ca_certs()}


def test_the_ftps_trust_store_is_certifi_and_not_the_machines_own() -> None:
    """
    The roots loaded must be certifi's, exactly.

    Compared as a set rather than by counting, and compared against a context
    built from certifi here in the test rather than against a hard-coded
    number, so that upgrading the certifi pin does not turn into a failing test
    with nothing actually wrong.

    This is the assertion that fails on the unfixed code: a context built from
    the operating system's store carries a different set of roots on every
    machine, and on the deployment machine it was missing the one that
    mattered.
    """
    expected = ssl.create_default_context(cafile=certifi.where())

    assert _subjects(_tls_context()) == _subjects(expected)


def test_certificate_verification_stays_switched_on() -> None:
    """
    Chain verification and the hostname check must both remain mandatory.

    The vendor is reached over the public internet with a password that gives
    read access to the client's supply feed. A context that skips either check
    would accept anyone able to intercept the connection, and it would do so
    silently -- the tests would pass and the button would go green.
    """
    context = _tls_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_certifi_is_pinned_as_a_direct_dependency() -> None:
    """
    certifi is imported by name, so it must have its own line in requirements.

    Exactly the rule that python-dotenv and httpx[http2] are already there to
    enforce. A transitive dependency is not a promise: httpx could drop or
    replace certifi in a future release and the FTPS connection would break at
    import time, on a machine nobody is watching, at three in the morning.
    """
    requirements = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(
        encoding="utf-8"
    )

    assert any(
        line.strip().startswith("certifi==") for line in requirements.splitlines()
    ), "certifi must be pinned explicitly in requirements.txt"


# ---------------------------------------------------------------------------
# "Connected, but there are no files here"
# ---------------------------------------------------------------------------
# The most confusing result the connection-test button can produce: every
# credential the operator typed was correct, the connection genuinely worked,
# and the screen still says no. On the first real deployment that message sent
# the operator looking for a folder setting on the settings page -- where there
# has never been one, because the folder is VENDOR_FTP_PATH in .env.


class _EmptyDirectory:
    """A vendor server whose login directory holds subfolders and no feeds."""

    def __init__(self, subfolders: list[str]) -> None:
        self.subfolders = subfolders

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def list_files(self, *, suffix: str = ".zip") -> list:
        return []

    def list_directories(self) -> list[str]:
        return self.subfolders

    def download(self, name: str, destination) -> int:  # pragma: no cover
        raise AssertionError("a connection test must never download anything")


def _test_against(monkeypatch, client) -> tuple[bool, str]:
    """Run test_connection against a substituted transport."""
    from contextlib import contextmanager

    from app.vendor import ftp_client

    @contextmanager
    def fake_connect(_creds):
        yield client

    monkeypatch.setattr(ftp_client, "connect", fake_connect)
    creds = ftp_client.VendorCredentials(
        host="ftp.example.test", port=21, username="u", password="p", remote_path="/"
    )
    ok, message, files = ftp_client.test_connection(creds)
    assert files == []
    return ok, message


def test_an_empty_folder_names_the_subfolders_and_the_env_key(monkeypatch) -> None:
    """
    The message must point at where the files actually are.

    The vendor's own credentials sheet said the feeds were in the login
    directory. They were not -- the login directory held subfolders. Guessing
    their names is not something the operator should have to do from a dead
    end, and it is not something they can do from the dashboard at all.
    """
    ok, message = _test_against(monkeypatch, _EmptyDirectory(["full", "delta"]))

    assert ok is True                        # the connection worked; say so
    assert "delta" in message and "full" in message
    assert "VENDOR_FTP_PATH" in message      # the real key
    assert ".env" in message                 # the real file
    assert "in Settings" not in message      # the wrong place, removed


def test_a_genuinely_empty_folder_says_so_rather_than_inventing_a_folder(
    monkeypatch,
) -> None:
    """
    No files and no subfolders is a different answer, and needs the vendor.

    Suggesting a folder here would send the operator round a loop changing
    .env to values that cannot help.
    """
    ok, message = _test_against(monkeypatch, _EmptyDirectory([]))

    assert ok is True
    assert "no subfolders" in message
    assert "ask the vendor" in message
    assert "VENDOR_FTP_PATH" not in message


def test_a_connection_test_never_downloads(monkeypatch) -> None:
    """
    Guard on the shape of the button, not just its wording.

    ``_EmptyDirectory.download`` raises. A future "helpfully fetch the newest
    file to check it parses" would turn a two-second diagnostic into a 27 MB
    transfer that the operator did not ask for, on a link the vendor asked us
    not to hammer.
    """
    ok, _message = _test_against(monkeypatch, _EmptyDirectory(["delta"]))

    assert ok is True
