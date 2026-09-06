"""
The orchestrator: one run, start to finish.

THE EIGHT STAGES
================
    1  FETCH      list the vendor folder, download files never seen before
    2  READ       parse them, apply the sanity gates
    3  STORE      update what the vendor has, keep the history
    4  ASK        make sure we know what Amazon currently shows
    5  MATCH      barcode -> confirmed Amazon SKU
    6  DECIDE     apply the client's rules, compare against Amazon
    7  GATE       guardrails, then the mode: practice / ask first / automatic
    8  SEND       write quantity only, then read Amazon back to confirm

Data moves in one direction. Every stage records what it did on the
:class:`app.models.Run` row, so a run can be understood afterwards from the
database alone -- no log spelunking.

THREE RULES THIS FILE ENFORCES
==============================
1. **The kill switch is checked first, every time.** A paused system stays
   paused even if a cycle was already queued.

2. **Settings are read ONCE, at the start, and snapshotted onto the run.**
   Re-reading mid-run would let a settings change alter behaviour halfway
   through, making the run impossible to explain later.

3. **Full feeds and delta feeds are treated differently.** A barcode missing
   from a delta means "unchanged". A barcode missing from a full feed means
   "the vendor has dropped it". Confusing the two would either strand dead
   stock on sale or wipe the catalogue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.amazon.client import SpApiClient
from app.config import settings as app_settings
from app.core import settings_store
from app.engine import guardrails as rails
from app.engine.decision import (
    Decision,
    DecisionEngine,
    apply_change_limit,
    rules_from_settings,
)
from app.engine.mapping import CatalogIndex, ListingEntry, Mapper
from app.engine.pusher import create_batch, send_batch, verify_batch
from app.engine.report_builder import ReportRow, write_all_reports
from app.models import (
    AmazonListing,
    BatchStatus,
    FeedFile,
    FeedKind,
    FileStatus,
    Notification,
    ReportFile,
    Run,
    RunStatus,
    RunTrigger,
    SkuOverride,
    SyncMode,
    UnmappedBarcode,
    VendorProduct,
    VendorProductHistory,
    utcnow,
)
from app.vendor import filename as fname
from app.vendor.ftp_client import VendorConnectionError, VendorCredentials, connect
from app.vendor.parser import (
    EXPECTED_HEADER,
    FeedFormatError,
    ParseStats,
    iter_rows,
    sha256_of_file,
    verify_archive,
)

log = logging.getLogger(__name__)

#: How many vendor rows to accumulate before flushing to the database. Tuned
#: for the 1.15-million-row full feed: large enough that the round trips do not
#: dominate, small enough that memory stays flat.
UPSERT_BATCH = 5000


@dataclass
class RunOutcome:
    """Everything a caller needs to know about a finished run."""

    run_id: int
    status: RunStatus
    message: str = ""
    files_processed: int = 0
    rows_read: int = 0
    proposed: int = 0
    pushed: int = 0
    deferred: int = 0
    unmapped: int = 0
    batch_id: int | None = None
    guardrail_halt: str | None = None
    reports: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (
            RunStatus.COMPLETED,
            RunStatus.NO_CHANGES,
            RunStatus.DRY_RUN_COMPLETE,
            RunStatus.AWAITING_APPROVAL,
        )


# ===========================================================================
# The run
# ===========================================================================

def execute_run(
    session: Session,
    *,
    client: SpApiClient | None,
    vendor_credentials: VendorCredentials | None,
    trigger: RunTrigger = RunTrigger.SCHEDULE,
    triggered_by: str = "scheduler",
    force_full_reconcile: bool = False,
) -> RunOutcome:
    """
    Run the whole pipeline once.

    ``client`` may be None when Amazon is not configured yet -- stages 1 to 3
    still run, so the vendor data and the five reports keep flowing. That is
    Phase 1 of the rollout: real value with Amazon untouched.

    ``force_full_reconcile`` re-decides the entire in-scope catalogue rather
    than only what the newest feed mentioned. The daily full feed sets it
    automatically; the dashboard exposes it as "Reconcile everything now".
    """
    # ---- settings, once -------------------------------------------------
    cfg = settings_store.get_all(session)

    run = Run(
        trigger=trigger,
        triggered_by=triggered_by,
        mode=SyncMode(cfg["sync_mode"]),
        status=RunStatus.RUNNING,
        settings_snapshot=cfg,
    )
    session.add(run)
    session.flush()
    outcome = RunOutcome(run_id=run.id, status=RunStatus.RUNNING)

    log.info(
        "run %d started (trigger=%s, mode=%s, by=%s)",
        run.id, trigger.value, run.mode.value, triggered_by,
    )

    # ---- rule 1: the kill switch, before anything else -------------------
    if cfg.get("paused"):
        run.status = RunStatus.PAUSED
        run.guardrail_message = (
            "The system is paused. Nothing was checked and nothing was sent. "
            "Switch 'Pause everything' off in Settings to resume."
        )
        _finish(run)
        outcome.status = RunStatus.PAUSED
        outcome.message = run.guardrail_message
        log.info("run %d skipped: paused", run.id)
        return outcome

    try:
        # ---- stages 1-3: the vendor side --------------------------------
        processed_files, parse_stats, touched = _ingest(
            session, run, cfg, vendor_credentials, outcome
        )

        if run.status is RunStatus.HALTED_BY_GUARDRAIL:
            _finish(run)
            return outcome

        # A full feed among the processed files means reconcile everything.
        saw_full_feed = any(f.kind is FeedKind.FULL for f in processed_files)
        reconcile = force_full_reconcile or saw_full_feed

        # ---- reports, regardless of whether Amazon is configured --------
        _write_reports(session, run, cfg, processed_files, touched, outcome)

        if client is None:
            run.status = RunStatus.COMPLETED
            run.guardrail_message = (
                "Vendor data collected and reports produced. Amazon is not configured "
                "yet, so nothing was sent. Add the Amazon credentials in Settings to "
                "enable sending."
            )
            _finish(run)
            outcome.status = RunStatus.COMPLETED
            outcome.message = run.guardrail_message
            return outcome

        # ---- stages 4-6: Amazon's side, matching, deciding ---------------
        decisions, index, mapper, engine = _decide(
            session, run, cfg, touched, reconcile, outcome
        )

        if not decisions:
            run.status = RunStatus.NO_CHANGES
            run.guardrail_message = (
                "Checked, and Amazon already matches the vendor. Nothing to change - "
                "which is the outcome we want most of the time."
            )
            _finish(run)
            outcome.status = RunStatus.NO_CHANGES
            outcome.message = run.guardrail_message
            return outcome

        # ---- the change limit --------------------------------------------
        limit = int(cfg["max_changes_per_run"])
        batch_decisions, deferred = apply_change_limit(decisions, limit)
        run.proposed_changes = len(batch_decisions)
        outcome.proposed = len(batch_decisions)
        outcome.deferred = len(deferred)

        if deferred:
            log.info(
                "run %d: %d changes, sending %d this run (limit), %d deferred",
                run.id, len(decisions), len(batch_decisions), len(deferred),
            )

        # ---- stage 7: the gate -------------------------------------------
        hours = _hours_since_catalog_sync(session)
        verdict = rails.evaluate_batch(
            batch_decisions,
            in_scope_total=index.in_scope_count,
            settings=cfg,
            unmapped=mapper.stats.unmapped,
            considered=mapper.stats.considered,
            hours_since_catalog_sync=hours,
        )
        run.guardrail_detail = verdict.as_dict()

        if not verdict.passed:
            run.status = RunStatus.HALTED_BY_GUARDRAIL
            run.guardrail_message = verdict.message
            outcome.status = RunStatus.HALTED_BY_GUARDRAIL
            outcome.guardrail_halt = verdict.message
            outcome.message = verdict.message
            _notify(
                session, run, "guardrail", "critical",
                "Inventory sync stopped by a safety rule", verdict.message, cfg,
            )
            _finish(run)
            return outcome

        for w in verdict.warnings:
            _notify(session, run, "guardrail", "warning", "Inventory sync warning", w.message, cfg)

        # ---- stage 8: send, or hold -------------------------------------
        batch = create_batch(session, run.id, batch_decisions)
        outcome.batch_id = batch.id
        by_sku = {d.seller_sku: d for d in batch_decisions}

        if run.mode is SyncMode.DRY_RUN:
            batch.status = BatchStatus.PENDING
            batch.notes = "Practice mode: nothing was sent."
            run.status = RunStatus.DRY_RUN_COMPLETE
            run.guardrail_message = (
                f"Practice mode. {len(batch_decisions):,} products would have been "
                "changed. Nothing was sent to Amazon. The full list is on this run's "
                "page."
            )
            outcome.status = RunStatus.DRY_RUN_COMPLETE
            outcome.message = run.guardrail_message

        elif run.mode is SyncMode.NEEDS_APPROVAL:
            batch.status = BatchStatus.AWAITING_APPROVAL
            run.status = RunStatus.AWAITING_APPROVAL
            run.guardrail_message = (
                f"{len(batch_decisions):,} changes are ready and waiting for approval. "
                "Nothing has been sent yet."
            )
            outcome.status = RunStatus.AWAITING_APPROVAL
            outcome.message = run.guardrail_message
            _notify(
                session, run, "approval_needed", "info",
                f"{len(batch_decisions):,} inventory changes need your approval",
                run.guardrail_message
                + f"\n\nReview them at {app_settings.base_url}/runs/{run.id}",
                cfg,
            )

        else:  # AUTOMATIC
            summary = send_batch(session, client, batch, decisions_by_sku=by_sku)
            run.pushed_changes = summary.accepted
            outcome.pushed = summary.accepted
            outcome.errors.extend(summary.errors)

            if cfg.get("verify_after_push") and summary.accepted:
                verify_batch(session, client, batch)

            if summary.errors:
                run.status = RunStatus.FAILED
                run.error = "\n".join(summary.errors[:10])
                outcome.status = RunStatus.FAILED
                _notify(
                    session, run, "push_failure", "critical",
                    "Inventory sync could not send changes to Amazon",
                    run.error, cfg,
                )
            else:
                run.status = RunStatus.COMPLETED
                outcome.status = RunStatus.COMPLETED
                run.guardrail_message = (
                    f"{summary.accepted:,} changes sent"
                    + (f", {summary.rejected:,} rejected by Amazon" if summary.rejected else "")
                    + (f", {len(deferred):,} deferred to the next run" if deferred else "")
                    + "."
                )
                outcome.message = run.guardrail_message
                if summary.rejected:
                    _notify(
                        session, run, "push_failure", "warning",
                        f"Amazon rejected {summary.rejected:,} inventory changes",
                        f"Codes: {summary.codes}. See run {run.id} for the detail.",
                        cfg,
                    )

    except (VendorConnectionError, FeedFormatError) as exc:
        run.status = RunStatus.FAILED
        run.error = str(exc)
        outcome.status = RunStatus.FAILED
        outcome.message = str(exc)
        outcome.errors.append(str(exc))
        log.error("run %d failed: %s", run.id, exc)
        _notify(session, run, "vendor_unreachable", "critical",
                "Inventory sync could not read the vendor's files", str(exc), cfg)

    except Exception as exc:  # pragma: no cover - last resort
        run.status = RunStatus.FAILED
        run.error = f"{type(exc).__name__}: {exc}"
        outcome.status = RunStatus.FAILED
        outcome.message = run.error
        outcome.errors.append(run.error)
        log.exception("run %d failed unexpectedly", run.id)
        _notify(session, run, "push_failure", "critical",
                "Inventory sync failed unexpectedly", run.error, cfg)

    _finish(run)
    return outcome


# ===========================================================================
# Stages 1-3: the vendor side
# ===========================================================================

def _ingest(
    session: Session,
    run: Run,
    cfg: dict,
    creds: VendorCredentials | None,
    outcome: RunOutcome,
) -> tuple[list[FeedFile], dict[int, ParseStats], dict[str, tuple[int, int | None]]]:
    """
    Stages 1-3. Returns ``(processed files, stats per file, touched barcodes)``.

    ``touched`` maps barcode -> (new stock, previous stock) for everything the
    feeds mentioned, so the later stages need only consider what actually
    changed rather than the whole 1.15-million-row catalogue.
    """
    processed: list[FeedFile] = []
    stats_by_file: dict[int, ParseStats] = {}
    touched: dict[str, tuple[int, int | None]] = {}

    if creds is None:
        log.info("run %d: no vendor credentials configured; skipping the download", run.id)
        return processed, stats_by_file, touched

    quarantine = app_settings.quarantine_dir
    quarantine.mkdir(parents=True, exist_ok=True)

    tz = str(cfg["timezone"])
    only_today = bool(cfg["process_only_today"])
    max_age = float(cfg["max_file_age_hours"])

    # ---- stage 1: list and download -------------------------------------
    with connect(creds) as vendor:
        remote = vendor.list_files(suffix=".zip")
        run.files_discovered = len(remote)
        outcome.files_processed = 0

        known_hashes = {
            h for (h,) in session.execute(
                select(FeedFile.content_sha256).where(FeedFile.content_sha256.isnot(None))
            ).all()
        }
        known_names = {
            n for (n,) in session.execute(
                select(FeedFile.filename).where(
                    FeedFile.status.in_([FileStatus.PARSED, FileStatus.SKIPPED_DUPLICATE])
                )
            ).all()
        }

        candidates = []
        for entry in remote:
            parsed = fname.parse(entry.name)

            if not parsed.recognised:
                log.warning(
                    "run %d: filename %r is not recognised; skipping rather than guessing",
                    run.id, entry.name,
                )
                run.files_skipped += 1
                continue

            if entry.name in known_names:
                continue  # already handled

            if only_today and not fname.is_from_today(parsed, tz):
                # This is the client's "only today's files" rule. Exact, because
                # the date is in the filename.
                run.files_skipped += 1
                continue

            age = fname.age_hours(parsed, tz)
            if age is not None and age > max_age:
                run.files_skipped += 1
                continue

            candidates.append((parsed, entry))

        # Oldest first; a full feed before the deltas of the same day.
        candidates.sort(key=lambda pe: pe[0].sort_key)

        log.info(
            "run %d: %d files on the server, %d to process, %d skipped",
            run.id, len(remote), len(candidates), run.files_skipped,
        )

        for parsed, entry in candidates:
            local = quarantine / entry.name
            record = FeedFile(
                filename=entry.name,
                kind=parsed.kind,
                feed_date=(
                    datetime.combine(parsed.feed_date, datetime.min.time(), tzinfo=UTC)
                    if parsed.feed_date
                    else None
                ),
                sequence=parsed.sequence,
                size_bytes=entry.size,
                server_modified_at=entry.modified_at,
                status=FileStatus.DISCOVERED,
                local_path=str(local),
            )
            session.add(record)
            session.flush()

            try:
                written = vendor.download(entry.name, local)
                record.size_bytes = written
                record.status = FileStatus.DOWNLOADED

                digest = sha256_of_file(local)
                if digest in known_hashes:
                    # Same bytes under a different name. Never process twice.
                    record.status = FileStatus.SKIPPED_DUPLICATE
                    record.content_sha256 = None  # the column is unique
                    record.quarantine_reason = "identical content has already been processed"
                    run.files_skipped += 1
                    local.unlink(missing_ok=True)
                    session.flush()
                    continue

                record.content_sha256 = digest
                known_hashes.add(digest)

                verify_archive(local)  # CRC check: catches a truncated download
                record.status = FileStatus.VERIFIED
                session.flush()

            except (VendorConnectionError, FeedFormatError) as exc:
                record.status = FileStatus.QUARANTINED
                record.quarantine_reason = str(exc)
                local.unlink(missing_ok=True)
                session.flush()
                log.error("run %d: %s", run.id, exc)
                outcome.errors.append(str(exc))
                continue

            # ---- stages 2-3: parse and store ---------------------------
            try:
                stats = _parse_and_store(session, run, cfg, record, local, touched)
            except FeedFormatError as exc:
                record.status = FileStatus.QUARANTINED
                record.quarantine_reason = str(exc)
                session.flush()
                outcome.errors.append(str(exc))
                log.error("run %d: %s", run.id, exc)
                continue

            stats_by_file[record.id] = stats
            processed.append(record)
            run.files_processed += 1
            outcome.files_processed += 1
            run.rows_read += stats.usable_rows
            run.rows_rejected += stats.rejected_rows
            outcome.rows_read += stats.usable_rows
            session.flush()

    return processed, stats_by_file, touched


def _parse_and_store(
    session: Session,
    run: Run,
    cfg: dict,
    record: FeedFile,
    path: Path,
    touched: dict[str, tuple[int, int | None]],
) -> ParseStats:
    """
    Stages 2 and 3 for one file: parse, gate, and upsert.

    Streams the file, so a 1.15-million-row full feed never exists in memory
    all at once.
    """
    delimiter = str(cfg["feed_delimiter"])
    column_map = dict(cfg["column_map"])
    strict = bool(cfg["guardrail_require_known_header"])

    is_full = record.kind is FeedKind.FULL

    # Gate on row count BEFORE trusting the file, using the median of previous
    # full feeds as the baseline.
    median = _median_full_feed_rows(session) if is_full else None

    pending: list[tuple] = []
    stats = ParseStats()

    for row, reject, stats in iter_rows(
        path, delimiter=delimiter, column_map=column_map, strict_header=strict
    ):
        if reject is not None:
            # Rejects are recorded, not dropped -- a rising trend means the
            # vendor changed something. Capped so a catastrophically broken
            # file cannot fill the database.
            if stats.rejected_rows <= 500:
                from app.models import RejectedRow

                session.add(
                    RejectedRow(
                        feed_file_id=record.id,
                        line_number=reject.line_number,
                        raw_line=reject.raw_line[:2000],
                        reason=reject.reason,
                    )
                )
            continue

        if row is None:
            continue

        pending.append(
            (row.barcode.canonical, row.barcode.raw, row.artist, row.title,
             row.price, row.stock, row.product_format, row.barcode.checksum_ok)
        )

        if len(pending) >= UPSERT_BATCH:
            _upsert(session, record, pending, touched)
            pending.clear()

    if pending:
        _upsert(session, record, pending, touched)

    record.row_count = stats.usable_rows
    record.rejected_row_count = stats.rejected_rows
    record.observed_header = stats.observed_header

    # ---- the feed-level guardrails -------------------------------------
    verdict = rails.evaluate_feed(
        filename=record.filename,
        row_count=stats.usable_rows,
        rejected=stats.rejected_rows,
        historical_median=median,
        is_full_feed=is_full,
        observed_header=stats.observed_header,
        expected_header=delimiter.join(EXPECTED_HEADER),
        header_matched=stats.header_matches_expected,
        settings=cfg,
    )
    if not verdict.passed:
        # The rows are already in the database, which is fine -- vendor state
        # is only a record of what the vendor said. What must NOT happen is
        # deciding and pushing from a file we do not trust.
        record.status = FileStatus.QUARANTINED
        record.quarantine_reason = verdict.message
        run.status = RunStatus.HALTED_BY_GUARDRAIL
        run.guardrail_message = verdict.message
        run.guardrail_detail = verdict.as_dict()
        raise FeedFormatError(verdict.message)

    record.status = FileStatus.PARSED
    record.processed_at = utcnow()

    # ---- the dropped-product bookkeeping, full feeds only ---------------
    if is_full:
        _mark_missing_from_full_feed(session, record)

    log.info(
        "run %d: %s parsed (%s rows, %d rejected)",
        run.id, record.filename, f"{stats.usable_rows:,}", stats.rejected_rows,
    )
    return stats


def _upsert(
    session: Session,
    record: FeedFile,
    rows: list[tuple],
    touched: dict[str, tuple[int, int | None]],
) -> None:
    """
    Insert or update a block of vendor products, recording history.

    One SELECT for the whole block, then per-row work in memory. Doing this
    with one query per row would be 1.15 million round trips.
    """
    barcodes = [r[0] for r in rows]
    existing = {
        p.barcode: p
        for p in session.execute(
            select(VendorProduct).where(VendorProduct.barcode.in_(barcodes))
        ).scalars()
    }
    now = utcnow()

    for barcode, raw, artist, title, price, stock, fmt, checksum_ok in rows:
        current = existing.get(barcode)

        if current is None:
            session.add(
                VendorProduct(
                    barcode=barcode, raw_barcode=raw[:40], artist=artist, title=title,
                    price=price, stock=stock, product_format=fmt, checksum_ok=checksum_ok,
                    first_seen_at=now, last_seen_at=now, last_changed_at=now,
                    source_file_id=record.id,
                    last_full_feed_id=record.id if record.kind is FeedKind.FULL else None,
                    missing_from_full_feeds=0,
                )
            )
            session.add(
                VendorProductHistory(
                    barcode=barcode, feed_file_id=record.id,
                    old_stock=None, new_stock=stock,
                    old_price=None, new_price=price,
                    change_type="new",
                )
            )
            touched[barcode] = (stock, None)
            continue

        old_stock, old_price = current.stock, current.price
        stock_changed = old_stock != stock
        price_changed = (old_price or 0) != (price or 0)

        current.last_seen_at = now
        current.source_file_id = record.id
        if record.kind is FeedKind.FULL:
            current.last_full_feed_id = record.id
            current.missing_from_full_feeds = 0

        if stock_changed or price_changed:
            current.artist = artist or current.artist
            current.title = title or current.title
            current.product_format = fmt or current.product_format
            current.price = price
            current.stock = stock
            current.checksum_ok = checksum_ok
            current.last_changed_at = now

            if stock_changed and price_changed:
                change_type = "both"
            elif stock_changed:
                change_type = "sold_out" if stock == 0 else ("restocked" if old_stock == 0 else "stock")
            else:
                change_type = "price"

            session.add(
                VendorProductHistory(
                    barcode=barcode, feed_file_id=record.id,
                    old_stock=old_stock, new_stock=stock,
                    old_price=old_price, new_price=price,
                    change_type=change_type,
                )
            )
            touched[barcode] = (stock, old_stock)
        else:
            # Mentioned but unchanged. Still recorded as touched so a
            # reconcile can correct Amazon if it drifted.
            touched.setdefault(barcode, (stock, old_stock))

    session.flush()


def _mark_missing_from_full_feed(session: Session, record: FeedFile) -> None:
    """
    Count consecutive full feeds that did not mention each product.

    THE ONLY PLACE the dropped-product counter moves, and only ever driven by a
    FULL feed -- absence from a delta means "unchanged", never "gone".

    Done in SQL because it touches every one of the ~1.15 million vendor rows,
    and pulling those into Python to increment an integer would be absurd.
    """
    session.flush()
    session.query(VendorProduct).filter(
        VendorProduct.last_full_feed_id != record.id
    ).update(
        {VendorProduct.missing_from_full_feeds: VendorProduct.missing_from_full_feeds + 1},
        synchronize_session=False,
    )
    session.flush()


def _median_full_feed_rows(session: Session) -> int | None:
    """
    Typical row count of previous full feeds.

    The median rather than the mean, so one bad file already in the history
    cannot drag the baseline down and let the next bad file through.
    """
    counts = [
        c for (c,) in session.execute(
            select(FeedFile.row_count)
            .where(
                FeedFile.kind == FeedKind.FULL,
                FeedFile.status == FileStatus.PARSED,
                FeedFile.row_count.isnot(None),
            )
            .order_by(FeedFile.id.desc())
            .limit(10)
        ).all()
        if c
    ]
    if not counts:
        return None
    counts.sort()
    mid = len(counts) // 2
    return counts[mid] if len(counts) % 2 else (counts[mid - 1] + counts[mid]) // 2


# ===========================================================================
# Stages 4-6: Amazon, matching, deciding
# ===========================================================================

def _decide(
    session: Session,
    run: Run,
    cfg: dict,
    touched: dict[str, tuple[int, int | None]],
    reconcile: bool,
    outcome: RunOutcome,
) -> tuple[list[Decision], CatalogIndex, Mapper, DecisionEngine]:
    """Stages 4, 5 and 6."""
    prefixes = list(cfg["sku_prefixes_in_scope"])
    blacklist = list(cfg["blacklisted_skus"])

    listings = [
        ListingEntry(
            seller_sku=row.seller_sku,
            sku_prefix=row.sku_prefix,
            barcode=row.barcode or "",
            quantity=row.quantity,
            status=row.status,
            fulfillment_channel=row.fulfillment_channel or "DEFAULT",
            lead_time_to_ship_days=row.lead_time_to_ship_days,
            product_type=row.product_type,
            blacklisted=row.blacklisted,
        )
        for row in session.execute(
            select(AmazonListing).where(AmazonListing.present_in_last_sync.is_(True))
        ).scalars()
    ]
    index = CatalogIndex(listings, prefixes_in_scope=prefixes)

    overrides = {
        o.barcode: o.seller_sku
        for o in session.execute(
            select(SkuOverride).where(SkuOverride.active.is_(True))
        ).scalars()
    }

    mapper = Mapper(
        index,
        overrides=overrides,
        sku_prefix=str(cfg["sku_prefix_for_new"]),
        skip_inactive=bool(cfg["skip_inactive_listings"]),
        skip_fba=bool(cfg["skip_fba_listings"]),
        blacklist=blacklist,
    )
    engine = DecisionEngine(rules_from_settings(cfg))

    # Which barcodes to consider. A delta run looks only at what changed; a
    # full-feed reconcile walks the whole in-scope catalogue, which is how the
    # accumulated backlog gets found at all -- products that have not changed
    # at the vendor for weeks never appear in a delta.
    if reconcile:
        candidates = {lst.barcode for lst in index.all_in_scope if lst.barcode}
        log.info("run %d: full reconcile over %d in-scope listings", run.id, len(candidates))
    else:
        candidates = set(touched)
        log.info("run %d: delta run over %d changed barcodes", run.id, len(candidates))

    stock_by_barcode = _stock_for(session, candidates)
    decisions: list[Decision] = []
    unmapped_in_stock = 0

    for barcode in candidates:
        stock, fmt = stock_by_barcode.get(barcode, (None, ""))
        if stock is None:
            continue  # in Amazon's catalogue but the vendor has never listed it

        match = mapper.match(barcode)
        if not match.matched:
            if stock > 0:
                unmapped_in_stock += 1
                _record_unmapped(session, barcode, match, stock)
            continue

        decisions.append(engine.decide(match, stock, product_format=fmt))

    # ---- products the vendor has dropped -------------------------------
    if reconcile:
        threshold = int(cfg["missing_full_feeds_before_zero"])
        missing = {
            p.barcode: p.missing_from_full_feeds
            for p in session.execute(
                select(VendorProduct).where(
                    VendorProduct.missing_from_full_feeds >= threshold
                )
            ).scalars()
        }
        for lst in index.all_in_scope:
            if not lst.barcode:
                continue
            count = missing.get(lst.barcode)
            if count is None and lst.barcode not in stock_by_barcode:
                # Never seen in any feed at all. Treat as dropped once a full
                # feed has been processed, using the same threshold.
                count = threshold
            if count is None:
                continue
            d = engine.decide_dropped(
                lst, missing_from_full_feeds=count, threshold=threshold
            )
            if d is not None:
                decisions.append(d)

    run.unmapped_count = unmapped_in_stock
    run.vendor_changes = len(touched)
    outcome.unmapped = unmapped_in_stock
    mapper.stats.unmapped = unmapped_in_stock  # report the actionable figure

    log.info(
        "run %d: %d changes proposed, %d in-stock products unmatched",
        run.id, sum(1 for d in decisions if d.should_push), unmapped_in_stock,
    )
    return [d for d in decisions if d.should_push], index, mapper, engine


def _stock_for(session: Session, barcodes: set[str]) -> dict[str, tuple[int, str]]:
    """Current vendor stock and format for a set of barcodes, in blocks."""
    out: dict[str, tuple[int, str]] = {}
    items = list(barcodes)
    CHUNK = 10000
    for i in range(0, len(items), CHUNK):
        block = items[i : i + CHUNK]
        for p in session.execute(
            select(VendorProduct).where(VendorProduct.barcode.in_(block))
        ).scalars():
            out[p.barcode] = (p.stock, p.product_format)
    return out


def _record_unmapped(session: Session, barcode: str, match, stock: int) -> None:
    """
    Queue an in-stock product we could not match, for a human to look at.

    Only in-stock products are recorded. The vendor has 1.15 million rows and
    the client lists about 45,500 of them, so recording every unlisted product
    would bury the actionable ones -- and the actionable ones are exactly those
    the vendor HAS in stock, which is also what the New Products report shows.
    """
    row = session.get(UnmappedBarcode, barcode)
    product = session.get(VendorProduct, barcode)

    if row is None:
        session.add(
            UnmappedBarcode(
                barcode=barcode,
                raw_barcode=(product.raw_barcode if product else "")[:40],
                title=(product.title if product else "")[:600],
                artist=(product.artist if product else "")[:400],
                product_format=(product.product_format if product else "")[:40],
                vendor_stock=stock,
                vendor_price=product.price if product else None,
                attempted_sku=match.attempted_sku,
                reason=match.reason or "no_listing",
            )
        )
    else:
        row.times_seen += 1
        row.last_seen_at = utcnow()
        row.vendor_stock = stock
        row.reason = match.reason or row.reason


def _hours_since_catalog_sync(session: Session) -> float | None:
    """How stale Amazon's snapshot is, in hours."""
    newest = session.execute(select(func.max(AmazonListing.synced_at))).scalar()
    if newest is None:
        return None
    if newest.tzinfo is None:  # SQLite loses the timezone
        newest = newest.replace(tzinfo=UTC)
    return (datetime.now(UTC) - newest).total_seconds() / 3600.0


# ===========================================================================
# Reports
# ===========================================================================

def _write_reports(
    session: Session,
    run: Run,
    cfg: dict,
    files: list[FeedFile],
    touched: dict[str, tuple[int, int | None]],
    outcome: RunOutcome,
) -> None:
    """
    Produce the five report files.

    Generated for every full-feed run and, when the setting allows, for every
    delta run too -- separately, as the client asked, so a delta's report is
    never confused with the daily one.
    """
    if not files:
        return

    newest = files[-1]
    is_delta = newest.kind is FeedKind.DELTA
    if is_delta and not cfg.get("generate_reports_for_delta", True):
        return

    prefix = str(cfg["sku_prefix_for_new"])
    directory = app_settings.reports_dir / f"run-{run.id}"

    in_stock: list[ReportRow] = []
    out_of_stock: list[ReportRow] = []
    price_changed: list[ReportRow] = []
    new_products: list[ReportRow] = []

    changed = [b for b, (new, old) in touched.items() if old is None or new != old]
    products = _products_for(session, changed)
    listings = _listings_for(session, [f"{prefix}{b}" for b in changed])

    for barcode in changed:
        product = products.get(barcode)
        if product is None:
            continue
        new_stock, old_stock = touched[barcode]
        listing = listings.get(f"{prefix}{barcode}")

        row = ReportRow(
            barcode=barcode,
            artist=product.artist,
            title=product.title,
            product_format=product.product_format,
            vendor_stock=new_stock,
            previous_stock=old_stock,
            vendor_price=product.price,
            amazon_sku=f"{prefix}{barcode}",
            amazon_quantity=listing.quantity if listing else None,
            published_quantity=listing.last_pushed_quantity if listing else None,
        )

        if old_stock is None:
            row.note = "not seen in a feed before"
            new_products.append(row)
            if new_stock > 0:
                in_stock.append(row)
        elif new_stock > 0 and old_stock == 0:
            row.note = "back in stock"
            in_stock.append(row)
        elif new_stock == 0 and old_stock > 0:
            row.note = "sold out at the vendor"
            out_of_stock.append(row)
        elif new_stock != old_stock:
            row.note = f"stock moved from {old_stock} to {new_stock}"
            in_stock.append(row)

    # Price movements are informational only: this system never sends a price.
    for barcode in changed:
        product = products.get(barcode)
        if product is None:
            continue
        recent = session.execute(
            select(VendorProductHistory)
            .where(
                VendorProductHistory.barcode == barcode,
                VendorProductHistory.feed_file_id == newest.id,
                VendorProductHistory.change_type.in_(["price", "both"]),
            )
            .limit(1)
        ).scalar_one_or_none()
        if recent is not None:
            price_changed.append(
                ReportRow(
                    barcode=barcode,
                    artist=product.artist,
                    title=product.title,
                    product_format=product.product_format,
                    vendor_stock=product.stock,
                    vendor_price=recent.new_price,
                    previous_price=recent.old_price,
                    amazon_sku=f"{prefix}{barcode}",
                    note="vendor cost changed - Amazon prices are not touched by this system",
                )
            )

    data = {
        "in_stock": in_stock,
        "new_products": new_products,
        "out_of_stock": out_of_stock,
        "price_changed": price_changed,
        # Streamed rather than materialised: about 116,000 rows.
        "current_in_stock_database": _iter_current_in_stock(session, prefix),
    }

    written = write_all_reports(
        data,
        directory=directory,
        run_id=run.id,
        feed_kind=newest.kind,
        feed_filename=newest.filename,
        sku_prefix=prefix,
    )

    for w in written:
        session.add(
            ReportFile(
                run_id=run.id,
                feed_file_id=newest.id,
                kind=w.kind,
                feed_kind=newest.kind,
                filename=w.path.name,
                path=str(w.path),
                row_count=w.row_count,
                size_bytes=w.size_bytes,
            )
        )
        outcome.reports.append(w.path.name)
    session.flush()


def _iter_current_in_stock(session: Session, prefix: str):
    """Stream every in-stock vendor product, for the big report."""
    for p in session.execute(
        select(VendorProduct).where(VendorProduct.stock > 0).order_by(VendorProduct.barcode)
    ).scalars():
        yield ReportRow(
            barcode=p.barcode,
            artist=p.artist,
            title=p.title,
            product_format=p.product_format,
            vendor_stock=p.stock,
            vendor_price=p.price,
            amazon_sku=f"{prefix}{p.barcode}",
        )


def _products_for(session: Session, barcodes: list[str]) -> dict[str, VendorProduct]:
    out: dict[str, VendorProduct] = {}
    for i in range(0, len(barcodes), 10000):
        for p in session.execute(
            select(VendorProduct).where(VendorProduct.barcode.in_(barcodes[i : i + 10000]))
        ).scalars():
            out[p.barcode] = p
    return out


def _listings_for(session: Session, skus: list[str]) -> dict[str, AmazonListing]:
    out: dict[str, AmazonListing] = {}
    for i in range(0, len(skus), 10000):
        for lst in session.execute(
            select(AmazonListing).where(AmazonListing.seller_sku.in_(skus[i : i + 10000]))
        ).scalars():
            out[lst.seller_sku] = lst
    return out


# ===========================================================================
# Bits and pieces
# ===========================================================================

def _finish(run: Run) -> None:
    run.finished_at = utcnow()
    started = run.started_at
    if started.tzinfo is None:  # SQLite
        started = started.replace(tzinfo=UTC)
    run.duration_seconds = (run.finished_at - started).total_seconds()


def _notify(
    session: Session,
    run: Run,
    kind: str,
    severity: str,
    subject: str,
    body: str,
    cfg: dict,
) -> None:
    """
    Queue an alert.

    Written to the database first and sent afterwards, so a notification
    survives an SMTP outage and still appears on the dashboard. Email is a
    convenience; the dashboard is the record.
    """
    recipients = {
        "critical": cfg.get("alert_emails_critical") or [],
        "warning": cfg.get("alert_emails_critical") or [],
        "info": cfg.get("alert_emails_approval") or [],
    }.get(severity, [])

    session.add(
        Notification(
            kind=kind,
            severity=severity,
            subject=subject[:300],
            body=body,
            run_id=run.id,
            recipients=list(recipients),
        )
    )
