"""
The write path: sending, verifying, undoing, and recovering.

WHY THIS FILE EXISTS
====================
Everything tested here can change quantities on a live Amazon account, and
before this file existed it was the least-covered code in the project. The
well-covered modules were the pure ones -- barcode rules, decision rules,
guardrails -- which are the easy ones. Coverage was inverted with respect to
risk.

HOW AMAZON IS FAKED
===================
At the HTTP transport, with :class:`httpx.MockTransport`, and nowhere higher.
Everything above the socket therefore runs for real: the rate limiter, the retry
and backoff logic, the 401 token refresh, the error extraction, the payload
builders in :mod:`app.amazon.listings`, and -- most importantly -- the price
guard in :mod:`app.amazon.guard`, which is invoked inside
``SpApiClient.request``. Faking at the client level instead would bypass the one
check the client cares most about.

WHAT EACH TEST IS REALLY ASSERTING
==================================
Not "the function returns the right value" but "this specific way of losing the
client money cannot happen":

  * a batch is durable before anything is sent, so a crash cannot leave Amazon
    changed with no record of what it was
  * one broken SKU cannot abort the other 4,999
  * a missing role stops the run instead of producing 5,000 identical errors
  * "Amazon said ACCEPTED but the quantity did not change" is caught, because
    that is the silent failure that has been costing this account stock
  * undo restores the exact previous quantity, taken from Amazon
  * a batch stranded mid-send by a crash is settled rather than left to rot
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.amazon.client import SpApiClient, SpApiPermissionError
from app.amazon.guard import PriceFieldRefused
from app.core import settings_store
from app.engine.decision import Decision, Direction
from app.engine.pipeline import RunOutcome, _recover_interrupted_batches
from app.engine.pusher import create_batch, send_batch, verify_batch
from app.engine.rollback import RollbackError, execute_rollback, plan_rollback
from app.models import (
    AmazonListing,
    Base,
    BatchStatus,
    ItemResult,
    PushBatch,
    PushItem,
    Run,
    RunStatus,
    RunTrigger,
    SyncMode,
    utcnow,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _StubTokens:
    """Stands in for the LWA token provider. Never reaches Amazon."""

    def __init__(self) -> None:
        self.invalidations = 0

    def token(self) -> str:
        return "test-access-token"

    def invalidate(self) -> None:
        self.invalidations += 1


def _client(handler, *, dry_run: bool = False) -> SpApiClient:
    """
    A real SpApiClient wired to a mock transport.

    ``handler`` receives an :class:`httpx.Request` and returns an
    :class:`httpx.Response`.
    """
    client = SpApiClient(
        _StubTokens(),
        endpoint="https://sellingpartnerapi-na.amazon.com",
        marketplace_id="ATVPDKIKX0DER",
        seller_id="A1TESTSELLER",
        dry_run=dry_run,
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def _accept_everything(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "ACCEPTED", "submissionId": "sub-1"})


def _listing_body(quantity: int) -> dict:
    """Amazon's GET listings shape, as observed."""
    return {
        "sku": "x",
        "fulfillmentAvailability": [
            {
                "fulfillmentChannelCode": "DEFAULT",
                "quantity": quantity,
                "leadTimeToShipMaxDays": 2,
            }
        ],
        "productTypes": [{"productType": "SOUND_AND_RECORDING"}],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def run(session) -> Run:
    r = Run(
        trigger=RunTrigger.MANUAL,
        triggered_by="test",
        mode=SyncMode.AUTOMATIC,
        status=RunStatus.RUNNING,
    )
    session.add(r)
    session.flush()
    return r


def _decision(sku: str, *, now: int, want: int) -> Decision:
    return Decision(
        seller_sku=sku,
        barcode=sku.rsplit("-", 1)[-1],
        current_quantity=now,
        desired_quantity=want,
        vendor_stock=want,
        direction=Direction.DOWN if want < now else Direction.UP,
        reason=f"vendor has {want}",
    )


@pytest.fixture
def listings(session) -> None:
    """Amazon's side of the picture, so verification has something to update."""
    for sku, qty in (
        ("HA-AMS-0008811065126", 7),
        ("HA-AMS-0008811096427", 4),
    ):
        session.add(
            AmazonListing(
                seller_sku=sku,
                sku_prefix="HA-AMS-",
                barcode=sku.rsplit("-", 1)[-1],
                quantity=qty,
                product_type="SOUND_AND_RECORDING",
                lead_time_to_ship_days=2,
                blacklisted=False,
                synced_at=utcnow(),
                present_in_last_sync=True,
            )
        )
    session.flush()


# ===========================================================================
# The undo trail
# ===========================================================================


class TestTheUndoTrail:
    def test_every_item_records_the_quantity_amazon_had(self, session, run):
        """
        Undo is only possible because of this column. If ``previous_quantity``
        is ever null, that product cannot be put back.
        """
        batch = create_batch(
            session,
            run.id,
            [
                _decision("HA-AMS-0008811065126", now=7, want=0),
                _decision("HA-AMS-0008811096427", now=4, want=11),
            ],
        )
        items = session.execute(
            select(PushItem).where(PushItem.batch_id == batch.id)
        ).scalars().all()

        assert len(items) == 2
        assert all(i.previous_quantity is not None for i in items)
        assert {i.previous_quantity for i in items} == {7, 4}

    def test_the_batch_is_committed_before_anything_is_sent(self, tmp_path, run):
        """
        THE DURABILITY BARRIER.

        A file-backed database is used here, not the in-memory one, precisely so
        that a second connection cannot see uncommitted work. The mock transport
        opens its own session and asserts that by the time Amazon is first
        called, the batch and every previous quantity are already on disk.

        Before this was fixed, the whole run -- ingest, decide, send, verify --
        was a single transaction committed at the very end. A container killed
        mid-send (a deploy, an OOM, a reboot) left Amazon changed and no record
        of what it had been, so those products could not be put back. That is the
        one promise this system makes.
        """
        url = f"sqlite+pysqlite:///{tmp_path / 'durability.db'}"
        engine = create_engine(url)
        Base.metadata.create_all(engine)
        Factory = sessionmaker(bind=engine, expire_on_commit=False)

        session = Factory()
        r = Run(
            trigger=RunTrigger.MANUAL, triggered_by="test",
            mode=SyncMode.AUTOMATIC, status=RunStatus.RUNNING,
        )
        session.add(r)
        session.flush()
        batch = create_batch(session, r.id, [_decision("HA-AMS-0008811065126", now=7, want=0)])
        batch_id = batch.id

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            # A genuinely independent connection to the same database file.
            observer = Factory()
            try:
                found = observer.get(PushBatch, batch_id)
                seen["batch_visible"] = found is not None
                seen["status"] = found.status if found else None
                seen["previous_quantities"] = [
                    i.previous_quantity
                    for i in observer.execute(
                        select(PushItem).where(PushItem.batch_id == batch_id)
                    ).scalars()
                ]
            finally:
                observer.close()
            return _accept_everything(request)

        client = _client(handler)
        send_batch(
            session, client, batch,
            decisions_by_sku={"HA-AMS-0008811065126": _decision(
                "HA-AMS-0008811065126", now=7, want=0
            )},
        )

        assert seen["batch_visible"], (
            "the batch was not durable when Amazon was first called: a crash here "
            "would change Amazon with no record of the previous quantities"
        )
        assert seen["status"] is BatchStatus.SENDING, (
            "the batch must be committed as SENDING before the first call, so a "
            "crash is distinguishable from 'nothing was sent'"
        )
        assert seen["previous_quantities"] == [7]

        session.close()
        engine.dispose()


# ===========================================================================
# Sending
# ===========================================================================


class TestSending:
    def test_one_rejected_sku_does_not_stop_the_others(self, session, run, listings):
        """
        A batch is up to 5,000 products. Amazon rejects individual SKUs for
        reasons peculiar to that listing -- 8541 invalid values, 8684 linked to
        more than one GCID. Aborting the batch would mean one broken listing
        holding up the whole catalogue.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            if "0008811096427" in str(request.url):
                return httpx.Response(
                    400,
                    json={"errors": [{"code": "8541", "message": "Invalid value"}]},
                )
            return _accept_everything(request)

        decisions = [
            _decision("HA-AMS-0008811065126", now=7, want=0),
            _decision("HA-AMS-0008811096427", now=4, want=11),
        ]
        batch = create_batch(session, run.id, decisions)
        summary = send_batch(
            session, _client(handler), batch,
            decisions_by_sku={d.seller_sku: d for d in decisions},
        )

        assert summary.accepted == 1
        assert summary.rejected == 1
        assert summary.codes == {"8541": 1}
        assert batch.status is BatchStatus.PARTIALLY_FAILED

        by_sku = {
            i.seller_sku: i
            for i in session.execute(
                select(PushItem).where(PushItem.batch_id == batch.id)
            ).scalars()
        }
        assert by_sku["HA-AMS-0008811065126"].result is ItemResult.ACCEPTED
        assert by_sku["HA-AMS-0008811096427"].result is ItemResult.REJECTED
        assert by_sku["HA-AMS-0008811096427"].amazon_code == "8541"

    def test_a_missing_role_stops_the_batch_instead_of_repeating_5000_times(
        self, session, run, listings
    ):
        """
        The most likely first-deployment failure. Amazon answers 403 to every
        call, so continuing would produce one identical error per SKU and an
        unreadable audit trail. Unsent items stay PENDING so a retry after the
        role is granted resumes exactly where this stopped.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={"errors": [{"code": "Unauthorized", "message": "Access to requested resource is denied."}]},
            )

        decisions = [
            _decision("HA-AMS-0008811065126", now=7, want=0),
            _decision("HA-AMS-0008811096427", now=4, want=11),
        ]
        batch = create_batch(session, run.id, decisions)
        summary = send_batch(
            session, _client(handler), batch,
            decisions_by_sku={d.seller_sku: d for d in decisions},
        )

        assert batch.status is BatchStatus.FAILED
        assert summary.accepted == 0
        assert summary.errors, "the operator must be told why nothing was sent"
        still_pending = session.execute(
            select(PushItem).where(
                PushItem.batch_id == batch.id,
                PushItem.result == ItemResult.PENDING,
            )
        ).scalars().all()
        assert len(still_pending) == 2, "unsent items must stay retryable"

    def test_practice_mode_builds_the_real_payload_and_sends_nothing(
        self, session, run, listings
    ):
        """
        Practice mode has to be a genuine rehearsal, or weeks spent in it prove
        nothing. Same code path, same payload, no socket.
        """
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _accept_everything(request)

        decisions = [_decision("HA-AMS-0008811065126", now=7, want=0)]
        batch = create_batch(session, run.id, decisions)
        client = _client(handler, dry_run=True)
        summary = send_batch(
            session, client, batch,
            decisions_by_sku={d.seller_sku: d for d in decisions},
        )

        assert calls == [], "practice mode reached the network"
        assert summary.accepted == 1, "practice mode must still report what it would do"
        assert client.dry_run_payloads, "the payload should be recorded for review"

        payload = json.dumps(client.dry_run_payloads[0])
        assert "quantity" in payload
        assert "price" not in payload.lower()

    def test_a_price_in_the_payload_is_refused_at_the_transport(self, session, run):
        """
        The one thing the client forbade. Asserted against the real client, not
        against the guard in isolation, because what matters is that no code
        path can reach the socket with a price on it.
        """
        client = _client(_accept_everything)
        with pytest.raises(PriceFieldRefused):
            client.patch(
                "/listings/2021-08-01/items/A1TESTSELLER/HA-AMS-0008811065126",
                operation="listings.patch",
                json_body={"patches": [{"op": "replace", "value": [{"our_price": 9.99}]}]},
            )


# ===========================================================================
# Verification -- the silent failure
# ===========================================================================


class TestVerification:
    def test_accepted_but_not_applied_is_caught(self, session, run, listings):
        """
        THE SILENT FAILURE.

        Amazon answers ACCEPTED and then does nothing -- most often because the
        SKU does not exist in the exact form it was sent, which is what the
        zero-padding rule is about. Trusting the acceptance is how this account
        came to have 38,341 quantities out of step while its tooling reported
        success every day.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                # Amazon still shows the old number: the change did not stick.
                return httpx.Response(200, json=_listing_body(7))
            return _accept_everything(request)

        decisions = [_decision("HA-AMS-0008811065126", now=7, want=0)]
        batch = create_batch(session, run.id, decisions)
        client = _client(handler)
        send_batch(session, client, batch, decisions_by_sku={d.seller_sku: d for d in decisions})

        summary = verify_batch(session, client, batch, settle_seconds=0)

        assert summary.verified == 0
        assert summary.not_applied == 1
        item = session.execute(
            select(PushItem).where(PushItem.batch_id == batch.id)
        ).scalar_one()
        assert item.result is ItemResult.NOT_APPLIED
        assert item.verified_quantity == 7
        assert "still shows 7" in (item.amazon_message or "")
        assert batch.status is not BatchStatus.VERIFIED

    def test_a_sku_that_does_not_exist_is_reported_as_not_applied(
        self, session, run, listings
    ):
        """A 404 on read-back means we patched a SKU that is not on the account."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(404, json={"errors": [{"code": "NOT_FOUND"}]})
            return _accept_everything(request)

        decisions = [_decision("HA-AMS-0008811065126", now=7, want=0)]
        batch = create_batch(session, run.id, decisions)
        client = _client(handler)
        send_batch(session, client, batch, decisions_by_sku={d.seller_sku: d for d in decisions})

        summary = verify_batch(session, client, batch, settle_seconds=0)

        assert summary.not_applied == 1
        item = session.execute(
            select(PushItem).where(PushItem.batch_id == batch.id)
        ).scalar_one()
        assert item.result is ItemResult.NOT_APPLIED
        assert "not on the account" in (item.amazon_message or "")

    def test_a_confirmed_change_updates_our_picture_of_amazon(
        self, session, run, listings
    ):
        """
        Decisions are made against Amazon's reported quantity, so that picture
        has to be kept in step or the next run re-proposes the same change.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=_listing_body(0))
            return _accept_everything(request)

        decisions = [_decision("HA-AMS-0008811065126", now=7, want=0)]
        batch = create_batch(session, run.id, decisions)
        client = _client(handler)
        send_batch(session, client, batch, decisions_by_sku={d.seller_sku: d for d in decisions})

        summary = verify_batch(session, client, batch, settle_seconds=0)

        assert summary.verified == 1
        assert batch.status is BatchStatus.VERIFIED
        assert session.get(AmazonListing, "HA-AMS-0008811065126").quantity == 0


# ===========================================================================
# Undo
# ===========================================================================


class TestUndo:
    def _sent_batch(self, session, run, handler) -> PushBatch:
        decisions = [
            _decision("HA-AMS-0008811065126", now=7, want=0),
            _decision("HA-AMS-0008811096427", now=4, want=11),
        ]
        batch = create_batch(session, run.id, decisions)
        send_batch(
            session, _client(handler), batch,
            decisions_by_sku={d.seller_sku: d for d in decisions},
        )
        return batch

    def test_undo_sends_back_the_exact_previous_quantities(
        self, session, run, listings
    ):
        """The whole point. 0 goes back to 7, 11 goes back to 4."""
        sent: list[tuple[str, int]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PATCH":
                body = json.loads(request.content)
                qty = body["patches"][0]["value"][0]["quantity"]
                sent.append((str(request.url).rsplit("/", 1)[-1].split("?")[0], qty))
            return _accept_everything(request)

        batch = self._sent_batch(session, run, handler)
        plan = plan_rollback(session, [batch.id])

        assert plan.count == 2, f"expected both items to be reversible: {plan.skipped}"
        assert {(c.seller_sku, c.desired_quantity) for c in plan.changes} == {
            ("HA-AMS-0008811065126", 7),
            ("HA-AMS-0008811096427", 4),
        }

        sent.clear()
        execute_rollback(
            session, _client(handler), plan,
            actor="test@example.com", reason="testing undo on purpose", verify=False,
        )

        assert dict(sent) == {
            "HA-AMS-0008811065126": 7,
            "HA-AMS-0008811096427": 4,
        }
        assert batch.status is BatchStatus.ROLLED_BACK

    def test_undo_is_not_skipped_just_because_the_catalogue_cache_is_stale(
        self, session, run, listings
    ):
        """
        THE REGRESSION THIS FILE EXISTS FOR.

        ``amazon_listings.quantity`` is a cache refreshed by the daily All
        Listings Report, plus whatever verification happens to sample -- 100
        items out of a batch of 5,000. For the other 4,900 it still holds the
        pre-push value, which is exactly ``previous_quantity``.

        The old skip condition compared those two and concluded "Amazon already
        shows what we would restore, nothing to do". So Undo, pressed shortly
        after a large automatic run, skipped nearly every item and reported
        "already showing 7" for products Amazon was showing as 0.

        This test reproduces that state precisely: a sent-and-accepted batch,
        no verification, and a catalogue cache still showing the old quantity.
        """
        batch = self._sent_batch(session, run, _accept_everything)

        # Exactly the production state: cache untouched by the push.
        for sku, stale in (("HA-AMS-0008811065126", 7), ("HA-AMS-0008811096427", 4)):
            assert session.get(AmazonListing, sku).quantity == stale
        items = session.execute(
            select(PushItem).where(PushItem.batch_id == batch.id)
        ).scalars().all()
        assert all(i.result is ItemResult.ACCEPTED for i in items)
        assert all(i.verified_quantity is None for i in items), "no verification ran"

        plan = plan_rollback(session, [batch.id])

        assert plan.count == 2, (
            "Undo skipped items because the catalogue cache was stale. Amazon "
            f"holds the pushed values, not the cached ones. Skipped: {plan.skipped}"
        )
        assert not plan.skipped

    def test_undo_still_skips_an_item_verified_to_be_back_at_its_old_value(
        self, session, run, listings
    ):
        """
        The skip itself is correct when the evidence is direct. If verification
        read the SKU back and Amazon is already showing the previous quantity --
        someone fixed it by hand, say -- there is genuinely nothing to restore.
        """
        batch = self._sent_batch(session, run, _accept_everything)
        item = session.execute(
            select(PushItem).where(
                PushItem.batch_id == batch.id,
                PushItem.seller_sku == "HA-AMS-0008811065126",
            )
        ).scalar_one()
        item.verified_quantity = item.previous_quantity  # observed, not assumed
        session.flush()

        plan = plan_rollback(session, [batch.id])

        assert "HA-AMS-0008811065126" in {sku for sku, _ in plan.skipped}
        assert plan.count == 1, "the other item is still reversible"

    def test_a_batch_that_sent_nothing_has_nothing_to_undo(self, session, run, listings):
        """
        Planning a rollback of an unsent batch yields an empty plan, and trying
        to execute it is refused. Offering an undo that would silently do
        nothing is worse than refusing one.
        """
        batch = create_batch(session, run.id, [_decision("HA-AMS-0008811065126", now=7, want=0)])
        plan = plan_rollback(session, [batch.id])

        assert plan.count == 0
        assert "Nothing to undo" in plan.summary()
        with pytest.raises(RollbackError):
            execute_rollback(
                session, _client(_accept_everything), plan,
                actor="test@example.com", verify=False,
            )

    def test_undoing_twice_is_refused(self, session, run, listings):
        """
        Undoing an already-undone batch would push the quantities that the undo
        replaced -- reapplying the change the operator just reversed.
        """
        batch = self._sent_batch(session, run, _accept_everything)
        execute_rollback(
            session, _client(_accept_everything), plan_rollback(session, [batch.id]),
            actor="test@example.com", verify=False,
        )
        assert batch.status is BatchStatus.ROLLED_BACK

        second = plan_rollback(session, [batch.id])

        assert second.count == 0, "an already-undone batch must offer nothing to undo"
        assert ("batch " + str(batch.id), "already rolled back") in second.skipped
        with pytest.raises(RollbackError):
            execute_rollback(
                session, _client(_accept_everything), second,
                actor="test@example.com", verify=False,
            )


# ===========================================================================
# Recovery from an interrupted run
# ===========================================================================


class TestRecovery:
    def test_a_batch_stranded_mid_send_is_settled_by_the_next_run(
        self, session, run, listings
    ):
        """
        Committing SENDING before the first call makes an interrupted send
        visible. Something then has to act on it, or the batch sits in SENDING
        forever and the operator has no idea whether Amazon was changed.

        Simulates the wreckage: one item sent and accepted, one never reached.
        """
        decisions = [
            _decision("HA-AMS-0008811065126", now=7, want=0),
            _decision("HA-AMS-0008811096427", now=4, want=11),
        ]
        batch = create_batch(session, run.id, decisions)
        batch.status = BatchStatus.SENDING
        items = {
            i.seller_sku: i
            for i in session.execute(
                select(PushItem).where(PushItem.batch_id == batch.id)
            ).scalars()
        }
        items["HA-AMS-0008811065126"].result = ItemResult.ACCEPTED
        # The other stays PENDING: the process died before it was sent.
        session.flush()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=_listing_body(0))
            return _accept_everything(request)

        later = Run(
            trigger=RunTrigger.SCHEDULE, triggered_by="scheduler",
            mode=SyncMode.AUTOMATIC, status=RunStatus.RUNNING,
        )
        session.add(later)
        session.flush()

        _recover_interrupted_batches(
            session, later, _client(handler),
            settings_store.get_all(session), RunOutcome(run_id=later.id, status=RunStatus.RUNNING),
        )

        assert batch.status is not BatchStatus.SENDING, "the batch was left stranded"
        # The item that was sent has been checked against Amazon.
        assert items["HA-AMS-0008811065126"].result is ItemResult.VERIFIED
        # The item that never went is marked as such, not silently dropped and
        # not blindly resent from inside a recovery routine.
        assert items["HA-AMS-0008811096427"].result is ItemResult.SKIPPED
        assert "before this item was sent" in (
            items["HA-AMS-0008811096427"].amazon_message or ""
        )
        # And both are still undoable, because previous_quantity was committed
        # before the send began.
        assert items["HA-AMS-0008811065126"].previous_quantity == 7
        assert items["HA-AMS-0008811096427"].previous_quantity == 4

    def test_nothing_happens_when_there_is_nothing_to_recover(self, session, run):
        """The common case must be free -- this runs before every single sync."""
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("recovery must not call Amazon when nothing is stranded")

        _recover_interrupted_batches(
            session, run, _client(handler),
            settings_store.get_all(session), RunOutcome(run_id=run.id, status=RunStatus.RUNNING),
        )


# ===========================================================================
# Transport behaviour
# ===========================================================================


class TestTransport:
    def test_a_401_refreshes_the_token_and_retries_once(self, session, run, listings):
        """
        Access tokens last an hour and a long run can cross the boundary. If a
        401 were treated as a failure, every run straddling the hour would lose
        part of its batch.
        """
        attempts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request.method)
            if len(attempts) == 1:
                return httpx.Response(401, json={"errors": [{"code": "Unauthorized"}]})
            return _accept_everything(request)

        client = _client(handler)
        decisions = [_decision("HA-AMS-0008811065126", now=7, want=0)]
        batch = create_batch(session, run.id, decisions)
        summary = send_batch(
            session, client, batch, decisions_by_sku={d.seller_sku: d for d in decisions}
        )

        assert len(attempts) == 2, "the 401 should have been retried exactly once"
        assert summary.accepted == 1
        assert client.tokens.invalidations == 1

    def test_a_403_is_a_permission_error_and_not_a_generic_failure(self):
        """
        It has its own exception class because it has its own remedy -- tick a
        role in Developer Central -- and the operator needs to be told that
        rather than "request failed".
        """
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"errors": [{"code": "Unauthorized"}]})

        client = _client(handler)
        with pytest.raises(SpApiPermissionError):
            client.get("/listings/2021-08-01/items/A1TESTSELLER/x", operation="listings.get")
