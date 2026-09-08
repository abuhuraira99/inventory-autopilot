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
