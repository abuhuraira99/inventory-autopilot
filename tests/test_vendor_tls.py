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
import pytest

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


# ---------------------------------------------------------------------------
# Working out what an unlabelled directory entry actually is
# ---------------------------------------------------------------------------
# The real server returned "Bib" and "Invent" with no type information, and the
# code defaulted them to files -- producing "it holds 2 other files: Bib,
# Invent" and the advice to ask the vendor which of those files was the stock
# feed. They were folders. MLSD does not have to supply a type and NLST never
# does, so unlabelled means unknown, and unknown gets checked.


class _StubFtp:
    """Enough of ftplib.FTP to exercise the type probe. Records every call."""

    def __init__(self, dirs: set[str], *, cwd_back_fails: bool = False) -> None:
        self.dirs = dirs
        self.cwd_back_fails = cwd_back_fails
        self.here = "/"
        self.calls: list[str] = []

    def pwd(self) -> str:
        return self.here

    def cwd(self, path: str) -> None:
        self.calls.append(path)
        if path == self.here:
            if self.cwd_back_fails:
                raise OSError("connection reset")
            return
        if path not in self.dirs:
            raise Exception("550 Not a directory")


def _client_with(stub: _StubFtp):
    from app.vendor.ftp_client import VendorCredentials, _FtpsClient

    client = _FtpsClient(
        VendorCredentials(host="h", port=21, username="u", password="p")
    )
    client._ftp = stub  # type: ignore[assignment]
    return client


def test_an_unlabelled_entry_that_can_be_entered_is_a_folder() -> None:
    """
    The exact case from the real server: two folders, no type information.

    Trying to change into it and back is the one test that works on every FTP
    server whatever it supports, and it is read-only.
    """
    stub = _StubFtp(dirs={"Bib", "Invent"})
    client = _client_with(stub)

    resolved = client._resolve_unknown_kinds(
        [("Bib", "unknown"), ("Invent", "unknown"), ("notes.txt", "unknown")]
    )

    assert dict(resolved) == {"Bib": "dir", "Invent": "dir", "notes.txt": "file"}


def test_the_probe_always_returns_to_the_directory_it_started_in() -> None:
    """
    Leaving the connection somewhere else would silently corrupt the listing.

    Every successful step into a folder must be followed by a step back, or the
    next probe measures the wrong directory and the whole answer is quietly
    wrong -- which is the failure this method exists to stop making.
    """
    stub = _StubFtp(dirs={"Bib", "Invent"})
    client = _client_with(stub)

    client._resolve_unknown_kinds([("Bib", "unknown"), ("Invent", "unknown")])

    assert stub.calls == ["Bib", "/", "Invent", "/"]


def test_the_probe_stops_rather_than_reporting_from_the_wrong_directory() -> None:
    """
    If the step back fails, stop. Do not carry on measuring from elsewhere.

    Everything not yet probed falls back to "file", which is honest: it is
    unknown, and this method promises never to *claim* a folder it has not
    confirmed.
    """
    stub = _StubFtp(dirs={"Bib", "Invent"}, cwd_back_fails=True)
    client = _client_with(stub)

    resolved = dict(
        client._resolve_unknown_kinds([("Bib", "unknown"), ("Invent", "unknown")])
    )

    assert resolved["Bib"] == "dir"      # confirmed before the failure
    assert resolved["Invent"] == "file"  # never probed, so not claimed
    assert stub.calls == ["Bib", "/"]    # and it stopped


def test_types_the_server_did_state_are_never_probed() -> None:
    """
    A server that answered the question is not asked it again.

    Probing costs two commands per entry against a vendor who asked to be
    polled gently.
    """
    stub = _StubFtp(dirs={"delta"})
    client = _client_with(stub)

    resolved = client._resolve_unknown_kinds([("delta", "dir"), ("a.csv", "file")])

    assert dict(resolved) == {"delta": "dir", "a.csv": "file"}
    assert stub.calls == []


def test_probing_is_capped() -> None:
    """
    A folder of hundreds of unlabelled entries must not become a flood.

    Past the cap the answer is already clear from what was probed.
    """
    from app.vendor.ftp_client import _FtpsClient

    stub = _StubFtp(dirs=set())
    client = _client_with(stub)
    many = [(f"entry{i}", "unknown") for i in range(_FtpsClient.PROBE_LIMIT + 40)]

    resolved = client._resolve_unknown_kinds(many)

    assert len(stub.calls) == _FtpsClient.PROBE_LIMIT
    assert all(kind == "file" for _name, kind in resolved)


# ---------------------------------------------------------------------------
# A dropped connection must not destroy work already done
# ---------------------------------------------------------------------------
# The vendor closes an idle control connection, and it sits idle for as long as
# the previous file takes to process -- seventeen minutes for the
# 1.15-million-row full feed. So the download of the NEXT file raised
# ConnectionResetError, which is not a VendorConnectionError, so it sailed past
# the handler that exists to quarantine one file and carry on. The whole run
# failed and the full feed that had just been read successfully was rolled back
# with it. Every day, on the one file the entire system depends on.


class _DeadConnection:
    """An ftplib.FTP whose transfer fails the way a closed connection does."""

    def retrbinary(self, cmd, callback, blocksize=8192):
        raise ConnectionResetError(
            10054, "An existing connection was forcibly closed by the remote host"
        )


def test_a_dropped_connection_becomes_a_vendor_error(tmp_path) -> None:
    """
    The exact failure from the live server, at the exact boundary.

    Asserted as VendorConnectionError because that is the type the pipeline
    catches to quarantine one file and keep going. Raised raw, it takes the
    run down and everything the run had already stored with it.
    """
    from app.vendor.ftp_client import VendorConnectionError, VendorCredentials, _FtpsClient

    client = _FtpsClient(
        VendorCredentials(host="ftp.example.test", port=21, username="u", password="p")
    )
    client._ftp = _DeadConnection()  # type: ignore[assignment]

    with pytest.raises(VendorConnectionError) as caught:
        client.download("DELTA_FEED_110721_20260909_24.zip", tmp_path / "d.zip")

    message = str(caught.value)
    assert "connection to ftp.example.test was lost" in message
    assert "retried on the next run" in message
    # Names the underlying cause: "connection lost" alone does not say whether
    # it was the vendor, the network, or this machine.
    assert "ConnectionResetError" in message


def test_a_failed_download_leaves_no_partial_file(tmp_path) -> None:
    """
    A .part left behind would be mistaken for a real file by the next run.

    The download writes to <name>.part and renames on success precisely so a
    truncated transfer can never be read as a complete feed -- which for a full
    feed would read as "the vendor has sold out of everything".
    """
    from app.vendor.ftp_client import VendorConnectionError, VendorCredentials, _FtpsClient

    client = _FtpsClient(
        VendorCredentials(host="ftp.example.test", port=21, username="u", password="p")
    )
    client._ftp = _DeadConnection()  # type: ignore[assignment]
    destination = tmp_path / "FULL_FEED_110721_20260909.zip"

    with pytest.raises(VendorConnectionError):
        client.download(destination.name, destination)

    assert list(tmp_path.iterdir()) == [], "a partial download was left on disk"
