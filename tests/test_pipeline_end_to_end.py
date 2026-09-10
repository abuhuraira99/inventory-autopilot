"""
A whole run, end to end, with the vendor and Amazon both faked at their edges.

WHY AN END-TO-END TEST AND NOT MORE UNIT TESTS
==============================================
``execute_run`` is the orchestrator: it decides the order of the eight stages,
where the transaction checkpoints fall, which failures abort a run and which are
recorded and stepped over, and what the operator is told afterwards. None of
that is visible from a unit test of any single stage -- the bugs live in the
seams.

It was also the least-covered module in the project, which is the wrong place to
have a gap: a mistake in a stage produces one wrong number, while a mistake in
the orchestrator produces a wrong catalogue.

WHAT IS FAKED, AND WHERE
========================
Exactly two things, both at the process boundary:

  * the vendor's FTP server -- ``connect`` is replaced by a fake that lists one
    archive and copies a real zip built by the test
  * Amazon -- an ``httpx.MockTransport`` under a real ``SpApiClient``

Everything in between is the production code path: filename parsing, archive
verification, the streaming parser, the upsert, the barcode matching with its
zero-padding rule, the decision rules, the guardrails, the change limit, batch
creation, and the report writer.

The feed rows and SKUs are the client's real ones, so the padding rule is
genuinely exercised: the vendor sends ``15047810567`` and the account holds
``HA-AMS-0015047810567``.
"""

from __future__ import annotations

import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.amazon.client import SpApiClient
from app.core import settings_store
from app.engine.pipeline import execute_run
from app.models import (
    AmazonListing,
    BatchStatus,
    PushBatch,
    RunStatus,
    RunTrigger,
    SyncMode,
    VendorProduct,
    utcnow,
)
from app.vendor.ftp_client import RemoteFile, VendorCredentials

# ---------------------------------------------------------------------------
# The vendor's real data, in the vendor's real format
# ---------------------------------------------------------------------------

HEADER = "barcode|artist|title|price|stock|format"

#: Note the stripped leading zeros -- that is the vendor's own output. The
#: matching SKUs on the account are padded to 13 digits.
ROWS = [
    "15047810567|FOSTER,RUTHIE|MILEAGE (BABY BLUE VINYL)|17.73|0|LP",     # 131 -> 0
    "8811060626|CARLISLE,BELINDA|HER GREATEST HITS|9.84|6|CD",            # 2 -> 6
    "5413356068320|GARNIER,LAURENT|RETROSPECTIVE|12.12|4|CD",             # 4 -> 4, no change
]

#: barcode as the vendor sends it -> the SKU actually on the account
SKU_FOR = {
    "15047810567": "HA-AMS-0015047810567",
    "8811060626": "HA-AMS-0008811060626",
    "5413356068320": "HA-AMS-5413356068320",
}


class _FakeVendor:
    """The vendor's FTP directory, as a local file copy."""

    def __init__(self, archive: Path) -> None:
        self.archive = archive
        self.downloads: list[str] = []

    def list_files(self, *, suffix: str = ".zip") -> list[RemoteFile]:
        return [
            RemoteFile(
                name=self.archive.name,
                size=self.archive.stat().st_size,
                modified_at=datetime.now(UTC),
            )
        ]

    def list_entries(self) -> list[tuple[str, str]]:
        # Part of the VendorClient interface. Never reached in these tests,
        # because list_files always returns a file -- but a fake that does not
        # implement the whole interface stops being a substitute for it.
        return [(self.archive.name, "file")]

    def download(self, name: str, destination: Path) -> int:
        self.downloads.append(name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.archive.read_bytes())
        return destination.stat().st_size


class _StubTokens:
    def token(self) -> str:
        return "test-access-token"

    def invalidate(self) -> None:
        pass


def _amazon(handler) -> SpApiClient:
    client = SpApiClient(
        _StubTokens(),
        endpoint="https://sellingpartnerapi-na.amazon.com",
        marketplace_id="ATVPDKIKX0DER",
        seller_id="A1TESTSELLER",
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def _accept(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "ACCEPTED", "submissionId": "s"})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def feed_archive(tmp_path) -> Path:
    """
    A full feed named for today, because the "today only" rule reads the date
    out of the filename rather than trusting a server clock.
    """
    today = datetime.now(UTC).strftime("%Y%m%d")
    path = tmp_path / f"FULL_FEED_110708_{today}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("feed.txt", "\n".join([HEADER, *ROWS]) + "\n")
    return path


@pytest.fixture
def account(session) -> None:
    """
    The account as the All Listings Report describes it: SKUs padded to 13
    digits, with the quantities Amazon currently shows.
    """
    for barcode, quantity in (("15047810567", 131), ("8811060626", 2), ("5413356068320", 4)):
        sku = SKU_FOR[barcode]
        session.add(
            AmazonListing(
                seller_sku=sku,
                sku_prefix="HA-AMS-",
                barcode=sku.rsplit("-", 1)[-1],  # the PADDED form, as Amazon holds it
                quantity=quantity,
                product_type="SOUND_AND_RECORDING",
                lead_time_to_ship_days=2,
                # Both of these are load-bearing, and both are checked before a
                # listing is ever touched: only Active, merchant-fulfilled
                # listings are in scope. An FBA listing's quantity is Amazon's
                # to manage, not ours.
                status="Active",
                fulfillment_channel="DEFAULT",
                blacklisted=False,
                synced_at=utcnow(),
                present_in_last_sync=True,
            )
        )
    session.flush()


@pytest.fixture
def toy_catalogue_guardrails(session):
    """
    Relax the two proportional circuit breakers, and say why.

    They are calibrated for the real account: 45,511 in-scope listings, of which
    a normal run changes a few hundred. This fixture's catalogue holds three
    listings, so changing two of them is 66% and trips the 25% limit -- which is
    the guardrail working exactly as designed, and is asserted directly in
    ``TestSafety``.

    Tests that are about something else raise the limits here rather than
    padding the fixture with hundreds of filler listings, which would obscure
    the data the assertions are actually about.
    """
    settings_store.set_value(session, "guardrail_max_percent_changed", 100, actor="test")
    settings_store.set_value(session, "guardrail_max_zeroing", 1000, actor="test")
    session.flush()


@pytest.fixture
def wired(session, feed_archive, monkeypatch, tmp_path):
    """
    Point the pipeline at the fake vendor and give it somewhere to write.

    Returns the fake so a test can assert on what was downloaded.
    """
    vendor = _FakeVendor(feed_archive)

    @contextmanager
    def fake_connect(_creds):
        yield vendor

    import app.engine.pipeline as pipeline

    monkeypatch.setattr(pipeline, "connect", fake_connect)
    # quarantine_dir, reports_dir and backups_dir all derive from data_dir, so
    # redirecting the one field keeps every write inside the test's tmp_path.
    monkeypatch.setattr(pipeline.app_settings, "data_dir", tmp_path / "data")
    (tmp_path / "data" / "quarantine").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "reports").mkdir(parents=True, exist_ok=True)
    return vendor


CREDS = VendorCredentials(
    host="ftp.vendor.example.com", port=21, username="u", password="p", mode="ftps"
)


# ===========================================================================
# Practice mode
# ===========================================================================


class TestPracticeMode:
    def test_a_full_run_decides_correctly_and_sends_nothing(
        self, session, wired, account, toy_catalogue_guardrails
    ):
        """
        The rehearsal the client is meant to spend weeks in. Every stage runs;
        no request reaches Amazon.

        Expected decisions from the fixture data:
            0015047810567  Amazon 131, vendor 0   -> change to 0
            0008811060626  Amazon   2, vendor 6   -> change to 6
            5413356068320  Amazon   4, vendor 4   -> no change
        """
        settings_store.set_value(session, "sync_mode", SyncMode.DRY_RUN.value, actor="test")
        session.flush()

        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _accept(request)

        outcome = execute_run(
            session,
            client=_amazon(handler),
            vendor_credentials=CREDS,
            trigger=RunTrigger.MANUAL,
            triggered_by="test",
        )

        assert outcome.status is RunStatus.DRY_RUN_COMPLETE, outcome.message
        assert calls == [], "practice mode must not reach Amazon"
        assert outcome.proposed == 2, f"expected 2 changes, got {outcome.proposed}"

        batch = session.get(PushBatch, outcome.batch_id)
        assert batch.status is BatchStatus.PENDING
        planned = {i.seller_sku: (i.previous_quantity, i.new_quantity) for i in batch.items}
        assert planned == {
            "HA-AMS-0015047810567": (131, 0),
            "HA-AMS-0008811060626": (2, 6),
        }

    def test_the_padding_rule_is_what_makes_the_match_work(
        self, session, wired, account, toy_catalogue_guardrails
    ):
        """
        The whole project hinges on this. The vendor sends ``15047810567``; the
        SKU on the account is ``HA-AMS-0015047810567``. Without padding to 13
        digits the constructed SKU does not exist, Amazon reports success, and
        nothing changes -- silently.
        """
        settings_store.set_value(session, "sync_mode", SyncMode.DRY_RUN.value, actor="test")
        session.flush()

        outcome = execute_run(
            session, client=_amazon(_accept), vendor_credentials=CREDS,
            trigger=RunTrigger.MANUAL, triggered_by="test",
        )

        batch = session.get(PushBatch, outcome.batch_id)
        matched = {i.seller_sku for i in batch.items}
        assert "HA-AMS-0015047810567" in matched, (
            "the padded SKU was not matched: the naive form HA-AMS-15047810567 "
            "does not exist on the account"
        )

    def test_the_vendor_data_is_stored_whatever_happens_to_amazon(
        self, session, wired, account
    ):
        """
        Stages 1-3 have value on their own -- they are Phase 1 of the rollout,
        and they produce the reports the client's team already uses. They must
        not be contingent on the Amazon side succeeding.
        """
        settings_store.set_value(session, "sync_mode", SyncMode.DRY_RUN.value, actor="test")
        session.flush()

        execute_run(
            session, client=None, vendor_credentials=CREDS,
            trigger=RunTrigger.MANUAL, triggered_by="test",
        )

        stored = {p.barcode: p.stock for p in session.query(VendorProduct).all()}
        assert stored["0015047810567"] == 0
        assert stored["0008811060626"] == 6
        assert stored["5413356068320"] == 4

    def test_a_run_with_no_amazon_credentials_still_completes(
        self, session, wired, account
    ):
        """Phase 1: real value with Amazon untouched."""
        outcome = execute_run(
            session, client=None, vendor_credentials=CREDS,
            trigger=RunTrigger.MANUAL, triggered_by="test",
        )

        assert outcome.status is RunStatus.COMPLETED
        assert "not configured" in (outcome.message or "")


# ===========================================================================
# The kill switch and the guardrails
# ===========================================================================


class TestSafety:
    def test_pause_stops_the_run_before_anything_is_read(self, session, wired, account):
        """
        The client's one big red button. It must take effect before the vendor
        is contacted, not somewhere in the middle.
        """
        settings_store.set_value(session, "paused", True, actor="test")
        session.flush()

        outcome = execute_run(
            session, client=_amazon(_accept), vendor_credentials=CREDS,
            trigger=RunTrigger.SCHEDULE, triggered_by="scheduler",
        )

        assert outcome.status is RunStatus.PAUSED
        assert wired.downloads == [], "paused, yet the vendor was still contacted"

    def test_a_catalogue_wide_wipe_is_halted(self, session, wired, account):
        """
        The circuit breaker that matters most. Lowering the "too many going to
        zero" threshold to 1 simulates a feed that would take the catalogue
        down; the run must stop with nothing sent.
        """
        settings_store.set_value(session, "sync_mode", SyncMode.AUTOMATIC.value, actor="test")
        settings_store.set_value(session, "guardrail_max_zeroing", 1, actor="test")
        session.flush()

        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _accept(request)

        outcome = execute_run(
            session, client=_amazon(handler), vendor_credentials=CREDS,
            trigger=RunTrigger.SCHEDULE, triggered_by="scheduler",
        )

        assert outcome.status is RunStatus.HALTED_BY_GUARDRAIL
        assert calls == [], "a halted run must not send anything"
        assert outcome.guardrail_halt

    def test_approval_mode_prepares_the_batch_and_waits(
        self, session, wired, account, toy_catalogue_guardrails
    ):
        """
        The middle setting, and the one the first live test should use: the work
        is done and nothing moves until a human presses Approve.
        """
        settings_store.set_value(
            session, "sync_mode", SyncMode.NEEDS_APPROVAL.value, actor="test"
        )
        session.flush()

        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _accept(request)

        outcome = execute_run(
            session, client=_amazon(handler), vendor_credentials=CREDS,
            trigger=RunTrigger.SCHEDULE, triggered_by="scheduler",
        )

        assert outcome.status is RunStatus.AWAITING_APPROVAL
        assert calls == []
        assert session.get(PushBatch, outcome.batch_id).status is BatchStatus.AWAITING_APPROVAL


# ===========================================================================
# Automatic mode
# ===========================================================================


class TestAutomaticMode:
    def test_changes_are_sent_and_the_batch_is_recorded(
        self, session, wired, account, toy_catalogue_guardrails
    ):
        settings_store.set_value(session, "sync_mode", SyncMode.AUTOMATIC.value, actor="test")
        settings_store.set_value(session, "verify_after_push", False, actor="test")
        session.flush()

        patched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PATCH":
                patched.append(str(request.url).rsplit("/", 1)[-1].split("?")[0])
            return _accept(request)

        outcome = execute_run(
            session, client=_amazon(handler), vendor_credentials=CREDS,
            trigger=RunTrigger.SCHEDULE, triggered_by="scheduler",
        )

        assert outcome.status is RunStatus.COMPLETED, outcome.message
        assert outcome.pushed == 2
        assert set(patched) == {"HA-AMS-0015047810567", "HA-AMS-0008811060626"}

        batch = session.get(PushBatch, outcome.batch_id)
        assert batch.status is BatchStatus.SENT
        assert all(i.previous_quantity is not None for i in batch.items), (
            "every item must be reversible"
        )

    def test_a_second_run_over_the_same_feed_changes_nothing_more(
        self, session, wired, account, toy_catalogue_guardrails
    ):
        """
        IDEMPOTENCE.

        Decisions are made against Amazon's reported quantity, not against the
        previous feed. Once verification has confirmed the new values, a second
        run over the same data has nothing to do -- which is what makes a failed
        push self-healing rather than permanently lost.
        """
        settings_store.set_value(session, "sync_mode", SyncMode.AUTOMATIC.value, actor="test")
        settings_store.set_value(session, "verify_after_push", True, actor="test")
        session.flush()

        # Amazon reports back whatever it was last told.
        state = {SKU_FOR["15047810567"]: 131, SKU_FOR["8811060626"]: 2}

        def handler(request: httpx.Request) -> httpx.Response:
            sku = str(request.url).split("?")[0].rsplit("/", 1)[-1]
            if request.method == "PATCH":
                import json

                body = json.loads(request.content)
                state[sku] = body["patches"][0]["value"][0]["quantity"]
                return _accept(request)
            return httpx.Response(
                200,
                json={
                    "sku": sku,
                    "fulfillmentAvailability": [
                        {"fulfillmentChannelCode": "DEFAULT", "quantity": state.get(sku, 0),
                         "leadTimeToShipMaxDays": 2}
                    ],
                    "productTypes": [{"productType": "SOUND_AND_RECORDING"}],
                },
            )

        first = execute_run(
            session, client=_amazon(handler), vendor_credentials=CREDS,
            trigger=RunTrigger.MANUAL, triggered_by="test",
        )
        assert first.pushed == 2, first.message
        assert state[SKU_FOR["15047810567"]] == 0

        # Force a re-decision over the whole catalogue rather than only what the
        # newest feed mentioned, so this really does re-examine everything.
        second = execute_run(
            session, client=_amazon(handler), vendor_credentials=CREDS,
            trigger=RunTrigger.MANUAL, triggered_by="test",
            force_full_reconcile=True,
        )

        assert second.status is RunStatus.NO_CHANGES, (
            f"the second run wanted to change {second.proposed} more products; "
            "decisions are not idempotent"
        )


# ===========================================================================
# Duplicate work
# ===========================================================================


class TestDuplicateWork:
    def test_the_same_file_is_not_processed_twice(self, session, wired, account):
        """
        Deduplicated by content hash, not by name, so a vendor re-upload under a
        new name is still recognised. The hash record is committed at the vendor
        checkpoint -- without that commit a later failure would roll it back and
        the file would be reprocessed every cycle.
        """
        settings_store.set_value(session, "sync_mode", SyncMode.DRY_RUN.value, actor="test")
        session.flush()

        first = execute_run(
            session, client=_amazon(_accept), vendor_credentials=CREDS,
            trigger=RunTrigger.MANUAL, triggered_by="test",
        )
        assert first.files_processed == 1

        second = execute_run(
            session, client=_amazon(_accept), vendor_credentials=CREDS,
            trigger=RunTrigger.MANUAL, triggered_by="test",
        )
        assert second.files_processed == 0, "the same file was processed twice"


# ===========================================================================
# The vendor connection is not held while files are read
# ===========================================================================


class TestTheConnectionIsClosedBeforeParsing:
    """
    Reading a feed used to happen inside the open FTP connection.

    The control connection therefore sat idle for exactly as long as parsing
    took: a second for a delta, and seventeen minutes for the 1.15-million-row
    full feed. The vendor closes a connection idle that long -- correctly, it
    is their bandwidth -- so the download of the next file died with
    "[Errno 10054] An existing connection was forcibly closed by the remote
    host", and every full-feed run failed at the last step, after doing all of
    the work, and rolled it all back.

    Ordering is the whole fix, so ordering is what this asserts. A test of the
    error handling alone would pass just as happily against code that still
    holds the connection open for a quarter of an hour.
    """

    def test_the_download_finishes_and_the_connection_closes_before_any_parse(
        self, session, wired, account, toy_catalogue_guardrails, monkeypatch
    ) -> None:
        import app.engine.pipeline as pipeline

        order: list[str] = []

        real_download = wired.download

        def watched_download(name, destination):
            order.append("download")
            return real_download(name, destination)

        monkeypatch.setattr(wired, "download", watched_download)

        # The fake vendor is yielded by a context manager, so "close" is the
        # moment that manager exits -- which is what the real client does.
        @contextmanager
        def closing_connect(_creds):
            try:
                yield wired
            finally:
                order.append("close")

        monkeypatch.setattr(pipeline, "connect", closing_connect)

        real_parse = pipeline._parse_and_store

        def watched_parse(*args, **kwargs):
            order.append("parse")
            return real_parse(*args, **kwargs)

        monkeypatch.setattr(pipeline, "_parse_and_store", watched_parse)

        execute_run(
            session,
            client=None,
            vendor_credentials=CREDS,
            trigger=RunTrigger.MANUAL,
            triggered_by="test",
        )

        assert "parse" in order, "nothing was parsed, so the ordering proves nothing"
        assert "close" in order, "the connection was never closed"
        assert order.index("close") < order.index("parse"), (
            f"the connection was still open while parsing: {order}"
        )
        assert order.index("download") < order.index("close"), (
            f"parsing was reordered ahead of the download: {order}"
        )
