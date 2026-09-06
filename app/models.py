"""
Database schema.

READING GUIDE
=============
The tables fall into five groups. If you are new to this code, read them in
this order:

  1. ``Setting`` / ``AuditEvent`` / ``User``
     Configuration the client owns, and the record of who changed what.

  2. ``FeedFile`` / ``VendorProduct`` / ``VendorProductHistory``
     What the vendor said. One row per file ever seen (so nothing is processed
     twice) and one row per barcode with its full change history.

  3. ``AmazonListing``
     What Amazon says. Refreshed from the All Listings Report. This is the
     table that makes the system self-healing: we compare the *desired*
     quantity against Amazon's own reported quantity, not against yesterday's
     feed, so a push that fails today is retried tomorrow instead of being
     forgotten. See docs/ARCHITECTURE.md, "Why we diff against Amazon".

  4. ``Run`` / ``PushBatch`` / ``PushItem``
     Every cycle the system performs, every batch it sends, and the before/after
     quantity of every single SKU. ``PushItem.previous_quantity`` is what makes
     one-click rollback possible.

  5. ``UnmappedBarcode`` / ``RejectedRow`` / ``Notification``
     The things a human needs to look at.

INVARIANTS ENFORCED HERE
------------------------
* ``FeedFile.content_sha256`` is unique. The same bytes can never be processed
  twice, even if the vendor renames the file.
* ``VendorProduct.barcode`` stores the CANONICAL 13-digit form
  (see :mod:`app.core.barcode`). Never the raw vendor value -- that lives in
  ``raw_barcode`` for audit only.
* ``PushItem`` rows are immutable once written. A rollback creates a NEW batch
  rather than editing history.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: JSON column type.
#:
#: JSONB on PostgreSQL, which is what production uses: it is stored parsed
#: rather than as text, so it is indexable and does not need re-parsing on every
#: read. Falls back to plain JSON on SQLite, which the test suite uses -- JSONB
#: is a PostgreSQL extension and will not compile anywhere else.
#:
#: Used for settings values, audit before/after pairs, the per-run settings
#: snapshot and Amazon's feed result summaries.
JSONType = JSON().with_variant(JSONB(), "postgresql")

#: Auto-incrementing 64-bit primary key.
#:
#: BIGINT on PostgreSQL, because ``push_items`` gains a row for every SKU in
#: every batch -- at 5,000 changes per run and a 15-minute cycle that is a few
#: hundred million rows over the life of the system, comfortably past the 2.1
#: billion ceiling of a 32-bit key being a real consideration.
#:
#: INTEGER on SQLite, which the test suite uses, because SQLite only
#: auto-increments a column declared exactly ``INTEGER PRIMARY KEY``. A
#: ``BIGINT PRIMARY KEY`` there is an ordinary column with a NOT NULL
#: constraint and no default, so every insert fails. This variant is the
#: documented way round it.
BigIntPk = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    Everything is stored in UTC. The client's "which day is it" question is a
    *presentation* concern answered with the configured timezone
    (America/New_York) at the edge -- never by storing local times, which
    breaks twice a year at the daylight-saving boundary.
    """
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base with a shared JSON type mapping."""


# ===========================================================================
# Enumerations
# ===========================================================================

class SyncMode(str, enum.Enum):
    """
    How far a run is allowed to go. The single most important control in the
    system, and the reason it is safe to point at a live revenue account.
    """

    DRY_RUN = "dry_run"          # compute everything, send nothing
    NEEDS_APPROVAL = "needs_approval"  # prepare a batch, wait for a human
    AUTOMATIC = "automatic"      # send on its own, guardrails supervising


class FeedKind(str, enum.Enum):
    """
    Full feeds and delta feeds must be treated by DIFFERENT rules.

    A delta lists only what changed, so a barcode's absence means "unchanged".
    A full feed lists everything the vendor carries, so a barcode's absence
    means "the vendor has dropped this product" and it should come off sale.
    Confusing the two would either leave dead stock on sale forever, or wipe
    out the catalogue. Hence an explicit type on every file.
    """

    FULL = "full"
    DELTA = "delta"
    UNKNOWN = "unknown"


class FileStatus(str, enum.Enum):
    DISCOVERED = "discovered"      # seen on the server, not yet downloaded
    DOWNLOADED = "downloaded"      # bytes on disk, not yet verified
    VERIFIED = "verified"          # zip opens, checksum matches
    PARSED = "parsed"              # rows loaded into vendor_products
    SKIPPED_OLD = "skipped_old"    # not from the current day
    SKIPPED_DUPLICATE = "skipped_duplicate"  # identical content already seen
    QUARANTINED = "quarantined"    # failed a sanity gate; needs a human
    FAILED = "failed"


class RunTrigger(str, enum.Enum):
    SCHEDULE = "schedule"
    MANUAL = "manual"
    ROLLBACK = "rollback"
    CATALOG_REFRESH = "catalog_refresh"


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    NO_CHANGES = "no_changes"           # a good outcome, not a failure
    HALTED_BY_GUARDRAIL = "halted_by_guardrail"
    AWAITING_APPROVAL = "awaiting_approval"
    DRY_RUN_COMPLETE = "dry_run_complete"
    FAILED = "failed"
    PAUSED = "paused"                   # the kill switch was on


class BatchStatus(str, enum.Enum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SENDING = "sending"
    SENT = "sent"
    VERIFIED = "verified"
    PARTIALLY_FAILED = "partially_failed"
    FAILED = "failed"
    REJECTED = "rejected"        # a human declined it
    ROLLED_BACK = "rolled_back"


class PushMethod(str, enum.Enum):
    """
    Two ways to write a quantity, used for different batch sizes.

    LISTINGS_API patches one SKU at a time. Feedback is immediate and
    per-SKU, which is what makes verification cheap, but it is one HTTP
    request each. Right for the frequent delta runs.

    FEEDS_API uploads the whole batch as a single JSON_LISTINGS_FEED document.
    One request for thousands of SKUs, but asynchronous: submit, poll, then
    download and parse a processing report. Right for the daily full-feed
    reconcile.
    """

    LISTINGS_API = "listings_api"
    FEEDS_API = "feeds_api"


class ItemResult(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"        # Amazon took it
    VERIFIED = "verified"        # and we read it back and confirmed
    REJECTED = "rejected"        # Amazon refused it, with a reason
    NOT_APPLIED = "not_applied"  # accepted but the read-back disagreed
    SKIPPED = "skipped"
    ERROR = "error"


class MapSource(str, enum.Enum):
    """
    How a barcode was matched to a SKU, in descending order of trust.

    Anything below MANUAL_OVERRIDE is never pushed: an unverified guess on a
    live account is how you update the wrong product.
    """

    BARCODE_EXACT = "barcode_exact"        # canonical 13-digit hit a real SKU
    BARCODE_VARIANT = "barcode_variant"    # a 12/14-digit form hit a real SKU
    MANUAL_OVERRIDE = "manual_override"    # a human said so
    UNMAPPED = "unmapped"                  # held for review, never sent


# ===========================================================================
# 1. Configuration, people, audit
# ===========================================================================

class Setting(Base):
    """
    One client-editable behaviour knob.

    Stored as key/JSON rather than as columns so that adding a knob does not
    require a migration -- which matters because the whole point is that the
    client can change behaviour without a developer.

    Every write goes through :mod:`app.core.settings_store`, which validates
    the value against ``value_type`` and writes an :class:`AuditEvent`. Nothing
    writes this table directly.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONType, nullable=False)

    #: "int" | "float" | "bool" | "str" | "list[str]" | "time" | "enum"
    value_type: Mapped[str] = mapped_column(String(24), nullable=False)

    #: Shown next to the field in the dashboard. Written for the client, not
    #: for a developer -- these strings are user interface.
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    help_text: Mapped[str] = mapped_column(Text, default="")

    #: Groups fields into dashboard sections: "safety", "schedule", "scope",
    #: "parsing", "alerts", "guardrails".
    category: Mapped[str] = mapped_column(String(40), default="general", index=True)

    #: Numeric bounds, enforced on write. Stops a typo like a cap of 15000.
    min_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    choices: Mapped[list | None] = mapped_column(JSONType, nullable=True)

    #: True for values that must never be editable from the web interface even
    #: by an administrator. There is exactly one: the "never send a price"
    #: invariant is enforced in code, and this flag stops anybody adding a
    #: settings row that pretends to turn it off.
    locked: Mapped[bool] = mapped_column(Boolean, default=False)

    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str] = mapped_column(String(120), default="system")


class User(Base):
    """
    A dashboard login.

    The client asked for a single shared profile rather than a role hierarchy,
    so the default deployment seeds exactly one administrator. The ``role``
    column exists anyway because it costs nothing now and adding it later would
    mean a migration plus an audit gap; ``viewer`` and ``operator`` are wired
    through the dependency in :mod:`app.security.auth` and simply unused until
    somebody creates such a user.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)

    #: Argon2id hash. Never a plaintext or reversible value.
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)

    #: "admin" | "operator" | "viewer"
    role: Mapped[str] = mapped_column(String(20), default="admin")

    #: Base32 TOTP secret, encrypted at rest. Null until the user enrols.
    totp_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    #: Simple brute-force brake. Cleared on a successful login.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Credential(Base):
    """
    An encrypted secret: the vendor FTP password, the Amazon client secret, the
    refresh token.

    Deliberately a separate table from ``settings`` so that a bug in the
    settings UI can never dump a secret into a JSON response, and so that a
    database dump for debugging can exclude one table and be safe to share.

    ``value_enc`` holds AES-256-GCM ciphertext keyed from ``MASTER_KEY``.
    ``hint`` holds the last four characters in clear so the dashboard can show
    "ends in 2c7" without ever returning the secret. Reads of the plaintext
    happen only inside :mod:`app.security.credentials`.
    """

    __tablename__ = "credentials"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value_enc: Mapped[str] = mapped_column(Text, nullable=False)
    hint: Mapped[str] = mapped_column(String(12), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str] = mapped_column(String(120), default="system")


class AuditEvent(Base):
    """
    Append-only record of every consequential action.

    Answers questions like "who set the cap to zero on Tuesday" and "who
    approved the batch that zeroed 400 SKUs". Nothing in the application
    updates or deletes rows here.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    #: "setting.changed", "credential.updated", "batch.approved",
    #: "batch.rejected", "batch.rolled_back", "run.started", "auth.login",
    #: "auth.failed", "killswitch.on", "scope.changed"
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)

    actor: Mapped[str] = mapped_column(String(120), default="system", index=True)
    actor_ip: Mapped[str | None] = mapped_column(String(60), nullable=True)

    #: What was acted on, e.g. a settings key or "batch:412".
    target: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)

    old_value: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_audit_at_action", "at", "action"),)


# ===========================================================================
# 2. What the vendor said
# ===========================================================================

class FeedFile(Base):
    """
    One vendor file, ever.

    The uniqueness of ``content_sha256`` is the whole anti-double-processing
    mechanism: if the vendor re-uploads yesterday's data under a new name, the
    hash matches and the file is skipped. Name, size and server timestamp are
    also recorded because they are how the "only today's files" rule is
    decided -- see :mod:`app.vendor.filename`.

    Real filenames from this vendor:
        FULL_FEED_110708_20260901.zip          (~27 MB zipped, 75 MB of text,
                                                1,150,544 rows)
        DELTA_FEED_110708_20260904_80.zip      (~4 KB, 105 rows, one every
                                                5 minutes, sequence resets daily)
    """

    __tablename__ = "feed_files"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)

    filename: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    kind: Mapped[FeedKind] = mapped_column(SAEnum(FeedKind, name="feed_kind"), default=FeedKind.UNKNOWN, index=True)

    #: Calendar date parsed out of the filename, in the vendor's own numbering.
    #: This -- not the file's timestamp -- is what the "today only" rule uses,
    #: because it is unambiguous and immune to timezone and clock drift.
    feed_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    #: The trailing counter on a delta filename. Gives a total order within a
    #: day, so files are always applied oldest-first even if discovered out of
    #: order.
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)

    size_bytes: Mapped[int | None] = mapped_column(BigIntPk, nullable=True)
    server_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: SHA-256 of the downloaded archive. Unique, and the reason nothing is
    #: ever processed twice.
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)

    status: Mapped[FileStatus] = mapped_column(SAEnum(FileStatus, name="file_status"), default=FileStatus.DISCOVERED, index=True)

    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rejected_row_count: Mapped[int] = mapped_column(Integer, default=0)

    #: Populated when a sanity gate refuses the file, so the dashboard can say
    #: exactly why rather than just "failed".
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Header row exactly as it arrived, so a vendor format change is visible
    #: in the audit trail and not merely inferred from a failure.
    observed_header: Mapped[str | None] = mapped_column(Text, nullable=True)

    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_feedfile_kind_date", "kind", "feed_date"),
        Index("ix_feedfile_status_date", "status", "feed_date"),
    )


class VendorProduct(Base):
    """
    The vendor's current position on one product.

    ``barcode`` is the CANONICAL 13-digit form produced by
    :func:`app.core.barcode.normalise`. ``raw_barcode`` keeps what the vendor
    actually sent (usually zero-stripped) purely so an operator can see the
    original when investigating.

    Feed columns, confirmed from the live file header
    ``barcode|artist|title|price|stock|format``:
      * ``artist`` and ``title`` are descriptive only
      * ``price`` is recorded but NEVER sent to Amazon -- the client sets their
        own prices with their own margin. See :mod:`app.amazon.guard`.
      * ``format`` is the product type: LP, CD, BD, DVD, SIN, TIN, MC, ACC ...
        Useful for per-type rules in the decision engine.
    """

    __tablename__ = "vendor_products"

    barcode: Mapped[str] = mapped_column(String(20), primary_key=True)
    raw_barcode: Mapped[str] = mapped_column(String(40), default="")

    artist: Mapped[str] = mapped_column(String(400), default="")
    title: Mapped[str] = mapped_column(String(600), default="")

    #: Vendor cost. Stored for the "price changed" report the team likes.
    #: Never transmitted.
    price: Mapped[float | None] = mapped_column(Float, nullable=True)

    stock: Mapped[int] = mapped_column(Integer, default=0, index=True)
    product_format: Mapped[str] = mapped_column(String(40), default="", index=True)

    #: False when the barcode failed the GTIN check digit. Not a blocker, but
    #: surfaced so a systematic vendor problem becomes visible.
    checksum_ok: Mapped[bool] = mapped_column(Boolean, default=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    #: Bumped every time ANY feed mentions this barcode, even with no change.
    #: The "dropped by the vendor" rule compares this against the newest full
    #: feed: if a full feed did not mention the product, the vendor no longer
    #: carries it.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    #: Only bumped when a value actually changed.
    last_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    #: The full feed that last confirmed this product exists. Null means it has
    #: only ever appeared in deltas.
    last_full_feed_id: Mapped[int | None] = mapped_column(BigIntPk, nullable=True)

    #: How many consecutive full feeds have omitted this product. The
    #: "missing from full feed" setting compares against this, so the client can
    #: choose "zero it immediately" (1) or "wait for two feeds" (2).
    missing_from_full_feeds: Mapped[int] = mapped_column(Integer, default=0, index=True)

    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("feed_files.id"), nullable=True)

    __table_args__ = (
        Index("ix_vendor_stock_seen", "stock", "last_seen_at"),
        CheckConstraint("stock >= 0", name="ck_vendor_stock_nonneg"),
    )


class VendorProductHistory(Base):
    """
    Append-only log of vendor changes.

    Answers "when did this go out of stock at the vendor" months later, and is
    the raw material for the five reports the client's team relies on.
    """

    __tablename__ = "vendor_product_history"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    barcode: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    feed_file_id: Mapped[int | None] = mapped_column(ForeignKey("feed_files.id"), nullable=True, index=True)

    old_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    old_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: "new" | "stock" | "price" | "both" | "restocked" | "sold_out" | "dropped"
    change_type: Mapped[str] = mapped_column(String(20), index=True)

    __table_args__ = (Index("ix_vph_barcode_at", "barcode", "at"),)


# ===========================================================================
# 3. What Amazon says
# ===========================================================================

class AmazonListing(Base):
    """
    One live listing on the seller account, from the All Listings Report.

    THIS TABLE IS WHY THE SYSTEM SELF-HEALS. The decision engine compares the
    quantity we *want* against ``quantity`` here -- Amazon's own reported
    number -- rather than against the previous feed. If a push silently fails,
    the difference is still present on the next run and gets retried. Diffing
    feed-against-feed would record "no change" forever and the error would
    become permanent and invisible.

    Note on this account's report: it contains only
    ``seller-sku, asin1, price, quantity, status`` -- there is NO product-id
    and NO fulfillment-channel column. So the barcode is recovered from inside
    the SKU (see :func:`app.core.barcode.split_sku`) and the fulfilment channel
    is taken from the listing itself when we read it back through the Listings
    API. Both are recorded here once known.

    Scope: ``sku_prefix`` is how the system knows a listing belongs to All
    Media Supply. Measured on 2026-09-04 across 96,010 listings:
        HA-AMS-    45,511 listings, 99.5% present in the AMS feed  <- in scope
        HA-INGR-   22,523 listings,  0.0% present  (Ingram)
        *-HM-     ~15,000 listings,  0-2% present
        *-OLD-      5,963 listings, 68-85% present (ambiguous, out of scope)
    """

    __tablename__ = "amazon_listings"

    seller_sku: Mapped[str] = mapped_column(String(120), primary_key=True)

    #: Denormalised from the SKU on write so scope filtering is an indexed
    #: equality test instead of a LIKE scan over 96,000 rows.
    sku_prefix: Mapped[str] = mapped_column(String(40), default="", index=True)

    #: Canonical 13-digit barcode recovered from the SKU. Indexed because it is
    #: the join key against ``vendor_products``.
    barcode: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)

    asin: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)

    #: Amazon's current quantity, as of ``synced_at``.
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    #: Recorded for the reports only. The system never writes a price.
    price: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: "Active" | "Inactive" | "Incomplete". Only Active listings are pushed;
    #: writing to an Inactive listing does nothing useful and muddies the audit.
    status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)

    #: "DEFAULT" means the seller ships it (merchant fulfilled), which is the
    #: case for this whole account -- confirmed by the client's filled template
    #: using "Fulfillment by Merchant (Default)". Anything starting with
    #: "AMAZON" is FBA: Amazon owns that number and we must never write it.
    fulfillment_channel: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    #: Preserved so a quantity patch does not wipe the seller's handling time.
    #: The Listings API replaces the whole ``fulfillment_availability`` object,
    #: so we read this, keep it, and send it back unchanged.
    lead_time_to_ship_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Amazon product type. ``PRODUCT`` for this account, confirmed from the
    #: template's ``ptds=UFJPRFVDVA==`` (base64 of "PRODUCT"). Required on every
    #: Listings API patch.
    product_type: Mapped[str] = mapped_column(String(60), default="PRODUCT")

    #: What we last successfully told Amazon, and when. Lets the dashboard
    #: distinguish "Amazon disagrees because our push failed" from "Amazon
    #: disagrees because a human changed it in Seller Central".
    last_pushed_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: True when this SKU is excluded by the never-touch list.
    blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    #: False once a catalogue refresh no longer lists this SKU (the listing was
    #: deleted in Seller Central). Kept rather than removed so history and
    #: rollback records stay meaningful.
    present_in_last_sync: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    __table_args__ = (
        Index("ix_listing_prefix_status", "sku_prefix", "status"),
        Index("ix_listing_barcode_prefix", "barcode", "sku_prefix"),
    )


class CatalogSync(Base):
    """One All Listings Report download, for the audit trail and for rollback.

    Keeping these lets an operator restore the account to the quantities of any
    past report -- the emergency option described in docs/OPERATIONS.md.
    """

    __tablename__ = "catalog_syncs"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Amazon's report id, so a support ticket can reference it.
    report_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    report_document_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    listing_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    in_scope_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    disappeared_count: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(30), default="running")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Path to the raw report file, kept for the restore-to-snapshot option.
    snapshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)


# ===========================================================================
# 4. Runs, batches, and the undo trail
# ===========================================================================

class Run(Base):
    """
    One cycle of the pipeline, whatever the outcome.

    A run exists even when nothing changed, because "we checked at 14:05 and
    everything already matched" is information the client wants on the
    dashboard.
    """

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    trigger: Mapped[RunTrigger] = mapped_column(SAEnum(RunTrigger, name="run_trigger"), default=RunTrigger.SCHEDULE)
    triggered_by: Mapped[str] = mapped_column(String(120), default="scheduler")

    #: The mode in force when the run started. Recorded rather than looked up
    #: later, so changing the mode mid-flight cannot rewrite history.
    mode: Mapped[SyncMode] = mapped_column(SAEnum(SyncMode, name="sync_mode"), default=SyncMode.DRY_RUN)

    status: Mapped[RunStatus] = mapped_column(SAEnum(RunStatus, name="run_status"), default=RunStatus.RUNNING, index=True)

    files_discovered: Mapped[int] = mapped_column(Integer, default=0)
    files_processed: Mapped[int] = mapped_column(Integer, default=0)
    files_skipped: Mapped[int] = mapped_column(Integer, default=0)
    rows_read: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)

    vendor_changes: Mapped[int] = mapped_column(Integer, default=0)
    proposed_changes: Mapped[int] = mapped_column(Integer, default=0)
    pushed_changes: Mapped[int] = mapped_column(Integer, default=0)
    unmapped_count: Mapped[int] = mapped_column(Integer, default=0)

    #: Full text of the guardrail that stopped the run, in the client's
    #: language, e.g. "Would set 2,431 products to zero; the limit is 500."
    guardrail_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Every guardrail evaluated, with its numbers, so a halt can be understood
    #: after the fact without re-running anything.
    guardrail_detail: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    #: Snapshot of the settings that governed this run. Essential for
    #: understanding a historical decision after somebody changes the rules.
    settings_snapshot: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    batches: Mapped[list[PushBatch]] = relationship(back_populates="run")

    __table_args__ = (Index("ix_run_status_started", "status", "started_at"),)


class PushBatch(Base):
    """
    A set of quantity changes sent to Amazon as one unit.

    A batch is the unit of approval AND the unit of rollback. "Undo batch 412"
    reads its :class:`PushItem` rows and sends every ``previous_quantity`` back.

    A rollback never edits this batch; it creates a new one with
    ``rollback_of_batch_id`` pointing here, so the history stays truthful.
    """

    __tablename__ = "push_batches"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    run: Mapped[Run] = relationship(back_populates="batches")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    status: Mapped[BatchStatus] = mapped_column(SAEnum(BatchStatus, name="batch_status"), default=BatchStatus.PENDING, index=True)
    method: Mapped[PushMethod] = mapped_column(SAEnum(PushMethod, name="push_method"), default=PushMethod.LISTINGS_API)

    item_count: Mapped[int] = mapped_column(Integer, default=0)
    zeroing_count: Mapped[int] = mapped_column(Integer, default=0)
    raising_count: Mapped[int] = mapped_column(Integer, default=0)
    lowering_count: Mapped[int] = mapped_column(Integer, default=0)

    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    verified_count: Mapped[int] = mapped_column(Integer, default=0)

    approved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Amazon's feed id when ``method`` is FEEDS_API. Null for direct patches.
    feed_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    feed_document_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    feed_result_summary: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Set when this batch IS a rollback of another one.
    rollback_of_batch_id: Mapped[int | None] = mapped_column(ForeignKey("push_batches.id"), nullable=True, index=True)
    #: Set on the original when it HAS been rolled back, so the UI can grey out
    #: the undo button rather than allowing a double rollback.
    rolled_back_by_batch_id: Mapped[int | None] = mapped_column(BigIntPk, nullable=True)

    #: True for the deliberate one-off catch-up run, which is allowed to exceed
    #: the per-run change limit. Recorded so it is obvious in the audit trail
    #: that a human chose to lift the brake.
    is_initial_sync: Mapped[bool] = mapped_column(Boolean, default=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list[PushItem]] = relationship(back_populates="batch")


class PushItem(Base):
    """
    One SKU's quantity change. **Immutable once written.**

    ``previous_quantity`` is the single most important column in the database
    for operational safety: it is what a rollback restores. It is captured from
    :class:`AmazonListing` immediately before the send, so it reflects what
    Amazon actually had rather than what we assumed.
    """

    __tablename__ = "push_items"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("push_batches.id"), index=True)
    batch: Mapped[PushBatch] = relationship(back_populates="items")

    seller_sku: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    barcode: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)

    #: Amazon's quantity before this change. What rollback puts back.
    previous_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: What we asked Amazon to set.
    new_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The vendor's raw number, before the cap and thresholds were applied.
    vendor_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Human-readable explanation of why this number, e.g.
    #: "vendor 27 -> capped at 15" or "vendor 0 -> out of stock".
    reason: Mapped[str] = mapped_column(String(200), default="")

    result: Mapped[ItemResult] = mapped_column(SAEnum(ItemResult, name="item_result"), default=ItemResult.PENDING, index=True)

    #: Amazon's error code and message when it refuses, e.g. 8684
    #: "SKU is associated to more than 1 GCID". Stored verbatim so recurring
    #: catalogue problems become visible instead of being retried forever.
    amazon_code: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    amazon_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Amazon's submission id for a Listings API patch, for support tickets.
    submission_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: What a read-back found. Null until verification runs.
    verified_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_pushitem_batch_result", "batch_id", "result"),
        Index("ix_pushitem_sku_batch", "seller_sku", "batch_id"),
    )


# ===========================================================================
# 5. Things a human needs to look at
# ===========================================================================

class UnmappedBarcode(Base):
    """
    A vendor product with stock that we could not tie to a real Amazon SKU.

    Never pushed. Surfaced in the dashboard so somebody can decide whether it
    is a genuinely new product to list, a listing under an unexpected SKU, or
    a product that belongs to a different supplier.
    """

    __tablename__ = "unmapped_barcodes"

    barcode: Mapped[str] = mapped_column(String(20), primary_key=True)
    raw_barcode: Mapped[str] = mapped_column(String(40), default="")

    title: Mapped[str] = mapped_column(String(600), default="")
    artist: Mapped[str] = mapped_column(String(400), default="")
    product_format: Mapped[str] = mapped_column(String(40), default="")
    vendor_stock: Mapped[int] = mapped_column(Integer, default=0)
    vendor_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: The SKU the prefix rule produced, so an operator can search Seller
    #: Central for it and see for themselves that it does not exist.
    attempted_sku: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: "no_listing" | "bad_barcode" | "out_of_scope_prefix" | "inactive_listing"
    reason: Mapped[str] = mapped_column(String(40), default="no_listing", index=True)

    times_seen: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    #: Set once somebody has dealt with it, so the queue does not grow forever.
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolved_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class SkuOverride(Base):
    """
    A manual barcode -> SKU mapping, for the cases the rules cannot reach.

    Client-editable and bulk-uploadable. Consulted by the mapping engine after
    the automatic tiers and before giving up. An override is trusted because a
    human asserted it, but the target SKU is still checked for existence before
    anything is sent.
    """

    __tablename__ = "sku_overrides"

    barcode: Mapped[str] = mapped_column(String(20), primary_key=True)
    seller_sku: Mapped[str] = mapped_column(String(120), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(120), default="system")


class RejectedRow(Base):
    """
    A feed row that could not be parsed. Kept rather than dropped.

    The live full feed contains roughly 150 of these a day (barcodes of 1-7
    characters). Recording them means a systematic vendor problem shows up as a
    trend on the dashboard instead of vanishing.
    """

    __tablename__ = "rejected_rows"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    feed_file_id: Mapped[int] = mapped_column(ForeignKey("feed_files.id"), index=True)
    line_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_line: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(String(120), default="", index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Notification(Base):
    """An alert, with delivery state, so a failed email is visible.

    Alerts also land in the dashboard. Email is a convenience, not the record
    of truth -- if SMTP is down the operator must still be able to find out
    what happened.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    #: "run_summary" | "guardrail" | "push_failure" | "approval_needed"
    #: | "vendor_unreachable" | "auth_failure" | "unmapped_spike"
    kind: Mapped[str] = mapped_column(String(40), index=True)
    #: "info" | "warning" | "critical"
    severity: Mapped[str] = mapped_column(String(12), default="info", index=True)

    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")

    run_id: Mapped[int | None] = mapped_column(BigIntPk, nullable=True, index=True)

    recipients: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    send_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class ReportFile(Base):
    """
    One generated report, per run.

    The client's team relies on two of these (In Stock and New Products) and
    wants all five produced for every full-feed run AND every delta run,
    separately. Keeping a row per file means the dashboard can offer a history
    rather than only the newest copy.
    """

    __tablename__ = "report_files"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    feed_file_id: Mapped[int | None] = mapped_column(ForeignKey("feed_files.id"), nullable=True, index=True)

    #: "in_stock" | "current_in_stock_database" | "new_products"
    #: | "out_of_stock" | "price_changed"
    kind: Mapped[str] = mapped_column(String(40), index=True)

    #: Which feed produced it, so a delta report is never confused with the
    #: daily full-feed report.
    feed_kind: Mapped[FeedKind] = mapped_column(SAEnum(FeedKind, name="report_feed_kind"), default=FeedKind.UNKNOWN)

    filename: Mapped[str] = mapped_column(String(300))
    path: Mapped[str] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(BigIntPk, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    __table_args__ = (Index("ix_report_run_kind", "run_id", "kind"),)
