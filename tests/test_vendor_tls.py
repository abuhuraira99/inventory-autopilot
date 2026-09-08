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
# "Connected, but there are no feed files here"
# ---------------------------------------------------------------------------
# The most confusing result the connection-test button can produce: every
# credential the operator typed was correct, the connection genuinely worked,
# and the screen still says no. There is no error text to search for, so the
# message has to carry the whole diagnosis.
#
# Both wordings this has already had were wrong on the same deployment, in the
# same hour. The first said "check the folder path in Settings" -- a page with
# no folder field, because the folder is VENDOR_FTP_PATH in .env. The second
# reported subfolders only, so it could say "no zip files, and no subfolders
# either", which sounds conclusive and would say exactly that about a folder
# holding forty CSV files. These tests pin all three answers apart.


class _Directory:
    """A vendor server with a directory we control, and no feed files."""

    def __init__(self, entries: list[tuple[str, str]]) -> None:
        self.entries = entries

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def list_files(self, *, suffix: str = ".zip") -> list:
        return []

    def list_entries(self) -> list[tuple[str, str]]:
        return self.entries

    def download(self, name: str, destination) -> int:  # pragma: no cover
        raise AssertionError("a connection test must never download anything")


def _test_against(monkeypatch, client, *, files: int = 0) -> tuple[bool, str]:
    """
    Run test_connection against a substituted transport.

    ``files`` is how many entries the caller expects back, so a test cannot
    quietly pass while the returned list is wrong.
    """
    from contextlib import contextmanager

    from app.vendor import ftp_client

    @contextmanager
    def fake_connect(_creds):
        yield client

    monkeypatch.setattr(ftp_client, "connect", fake_connect)
    creds = ftp_client.VendorCredentials(
        host="ftp.example.test", port=21, username="u", password="p", remote_path="/"
    )
    ok, message, found = ftp_client.test_connection(creds)
    assert len(found) == files
    return ok, message


def test_subfolders_are_named_along_with_the_env_key_to_change(monkeypatch) -> None:
    """
    When the feeds are one level down, say so and say where to set it.

    The vendor's own credentials sheet said the feeds were in the login
    directory. Guessing subfolder names is not something the operator should
    have to do from a dead end, and it is not something the dashboard can help
    with at all.
    """
    ok, message = _test_against(
        monkeypatch, _Directory([("delta", "dir"), ("full", "dir")])
    )

    assert ok is True                        # the connection worked; say so
    assert "delta" in message and "full" in message
    assert "VENDOR_FTP_PATH=/delta" in message   # the key, the file, a worked example
    assert ".env" in message
    assert "in Settings" not in message      # the first wrong wording, kept out


def test_a_folder_of_unexpected_files_is_not_reported_as_empty(monkeypatch) -> None:
    """
    Files that are not zips must be listed, not silently discounted.

    This is the case the previous wording got wrong: no zips and no subfolders
    produced "this is the right kind of empty", which would have sent someone
    to the vendor to ask why a folder holding real data was empty. The names
    are the whole diagnosis -- they say whether the extension changed, the
    naming changed, or this is the wrong account.
    """
    ok, message = _test_against(
        monkeypatch,
        _Directory([("STOCK_20260908.csv", "file"), ("readme.txt", "file")]),
    )

    assert ok is True
    assert "2 other files" in message
    assert "STOCK_20260908.csv" in message and "readme.txt" in message
    assert "It is not empty" in message
    # The two phrasings that would misrepresent a folder holding real data.
    assert "completely empty" not in message
    assert "right kind of empty" not in message
    assert "VENDOR_FTP_PATH" not in message   # the folder is not the problem


def test_a_truly_empty_folder_says_completely_empty_and_asks_the_vendor(
    monkeypatch,
) -> None:
    """
    Nothing at all is a real answer, and it belongs to the vendor.

    Suggesting a folder or a file name here would send the operator round a
    loop editing .env to values that cannot help.
    """
    ok, message = _test_against(monkeypatch, _Directory([]))

    assert ok is True
    assert "completely empty" in message
    assert "no files of any kind and no subfolders" in message
    assert "how long are they kept" in message
    assert "VENDOR_FTP_PATH" not in message


def test_a_connection_test_never_downloads(monkeypatch) -> None:
    """
    Guard on the shape of the button, not just its wording.

    ``_Directory.download`` raises. A future "helpfully fetch the newest file
    to check it parses" would turn a two-second diagnostic into a 27 MB
    transfer the operator did not ask for, on a link the vendor asked us not to
    hammer.
    """
    ok, _message = _test_against(monkeypatch, _Directory([("delta", "dir")]))

    assert ok is True


class _WithFeeds:
    """A vendor server that does have feed files."""

    def __init__(self, names: list[str]) -> None:
        self.names = names

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def list_files(self, *, suffix: str = ".zip") -> list:
        from datetime import UTC, datetime

        from app.vendor.ftp_client import RemoteFile

        return [
            RemoteFile(
                name=name,
                size=1024,
                modified_at=datetime(2026, 9, 8, 12, i, tzinfo=UTC),
            )
            for i, name in enumerate(self.names)
        ]

    def list_entries(self) -> list[tuple[str, str]]:  # pragma: no cover
        raise AssertionError(
            "the diagnostic listing costs a second command and must only be "
            "asked for when there is nothing to report"
        )

    def download(self, name: str, destination) -> int:  # pragma: no cover
        raise AssertionError("a connection test must never download anything")


def test_the_happy_path_reports_the_count_and_the_newest_file(monkeypatch) -> None:
    """
    The result the operator is actually hoping for.

    This had no test, which is how a refactor of the empty-folder branch was
    able to delete the success branch entirely and leave the function returning
    None on the one path that matters. mypy caught it; a test should have.

    It also pins the "only when empty" rule from the other side: ``_WithFeeds``
    raises if the diagnostic listing is requested when there are files.
    """
    ok, message = _test_against(
        monkeypatch, _WithFeeds(["FEED_20260908_01.zip", "FEED_20260908_02.zip"]), files=2
    )

    assert ok is True
    assert "Found 2 files" in message
    assert "The newest is FEED_20260908_02.zip." in message
