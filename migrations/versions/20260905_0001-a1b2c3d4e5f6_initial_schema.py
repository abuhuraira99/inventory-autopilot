"""Initial schema.

Revision ID: a1b2c3d4e5f6
Revises: none
Created: 2026-09-05

WHY THIS FILE IS EXPLICIT DDL AND NOT ``Base.metadata.create_all``
==================================================================
An earlier version of this revision built the schema by importing the models
and calling ``create_all``. It was shorter, and it was wrong.

A migration must describe the schema **as it was at that revision**, forever. A
migration that reads the current models describes whatever the models happen to
say on the day it runs, which breaks the moment a second revision exists:

    fresh database, revision 0002 adds a column
      0001 runs  -> create_all builds TODAY'S models, column already present
      0002 runs  -> op.add_column -> ERROR: column already exists

The same deployment succeeds on an existing database and fails on a new one,
which is the worst shape a schema bug can take -- it appears only when someone
rebuilds from scratch, months later, usually during an incident.

So this file is frozen. It was generated once, by ``alembic revision
--autogenerate`` against an empty database on 2026-09-07, and must not be
regenerated. From here on, migrations are proper diffs.

``tests/test_migrations.py`` asserts that applying this file produces exactly
the schema the models describe -- every table, column, nullability, primary key
and index. If someone changes a model and forgets to add a migration, that test
fails and names the difference. That test, not this comment, is what keeps the
two in step.

The two dialect variants below are deliberate and are explained in
``app/models.py``:

    sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
        JSONB on PostgreSQL for indexing and containment operators; plain JSON
        on SQLite, which has no JSONB, so the test suite can run in memory.

    sa.BigInteger().with_variant(sa.Integer(), "sqlite")
        BIGINT keys on PostgreSQL because push_items grows by thousands of rows
        a day; INTEGER on SQLite, which only auto-increments INTEGER keys.

WHAT IT CREATES
===============
    settings                 client-editable behaviour
    credentials              the encrypted secrets
    users                    the dashboard login
    audit_events             append-only record of every consequential action

    feed_files               one row per vendor file, ever
    vendor_products          what the vendor currently has
    vendor_product_history   every change, for the reports and for questions
                             asked months later
    rejected_rows            feed rows that could not be read

    amazon_listings          what Amazon says -- the table that makes the
                             system self-healing
    catalog_syncs            each All Listings Report download

    runs                     every cycle
    push_batches             the unit of approval and of rollback
    push_items               per-SKU before/after -- this is the undo trail

    unmapped_barcodes        the human queue
    sku_overrides            manual mappings
    notifications            alerts, with delivery state
    report_files             the five files, per run
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Opt-in required by :func:`downgrade`. Deliberately awkward to type, and
#: deliberately not a flag anyone would set out of habit.
DESTRUCTIVE_OPT_IN = "I_UNDERSTAND_THIS_DESTROYS_THE_UNDO_TRAIL"


def upgrade() -> None:
    """Create every table and index this revision defines."""
    op.create_table('amazon_listings',
    sa.Column('seller_sku', sa.String(length=120), nullable=False),
    sa.Column('sku_prefix', sa.String(length=40), nullable=False),
    sa.Column('barcode', sa.String(length=20), nullable=True),
    sa.Column('asin', sa.String(length=20), nullable=True),
    sa.Column('quantity', sa.Integer(), nullable=True),
    sa.Column('price', sa.Float(), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=True),
    sa.Column('fulfillment_channel', sa.String(length=40), nullable=True),
    sa.Column('lead_time_to_ship_days', sa.Integer(), nullable=True),
    sa.Column('product_type', sa.String(length=60), nullable=False),
    sa.Column('last_pushed_quantity', sa.Integer(), nullable=True),
    sa.Column('last_pushed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('blacklisted', sa.Boolean(), nullable=False),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('present_in_last_sync', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('seller_sku')
    )
    op.create_index(op.f('ix_amazon_listings_asin'), 'amazon_listings', ['asin'], unique=False)
    op.create_index(op.f('ix_amazon_listings_barcode'), 'amazon_listings', ['barcode'], unique=False)
    op.create_index(op.f('ix_amazon_listings_blacklisted'), 'amazon_listings', ['blacklisted'], unique=False)
    op.create_index(op.f('ix_amazon_listings_fulfillment_channel'), 'amazon_listings', ['fulfillment_channel'], unique=False)
    op.create_index(op.f('ix_amazon_listings_present_in_last_sync'), 'amazon_listings', ['present_in_last_sync'], unique=False)
    op.create_index(op.f('ix_amazon_listings_quantity'), 'amazon_listings', ['quantity'], unique=False)
    op.create_index(op.f('ix_amazon_listings_sku_prefix'), 'amazon_listings', ['sku_prefix'], unique=False)
    op.create_index(op.f('ix_amazon_listings_status'), 'amazon_listings', ['status'], unique=False)
    op.create_index(op.f('ix_amazon_listings_synced_at'), 'amazon_listings', ['synced_at'], unique=False)
    op.create_index('ix_listing_barcode_prefix', 'amazon_listings', ['barcode', 'sku_prefix'], unique=False)
    op.create_index('ix_listing_prefix_status', 'amazon_listings', ['sku_prefix', 'status'], unique=False)
    op.create_table('audit_events',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('action', sa.String(length=60), nullable=False),
    sa.Column('actor', sa.String(length=120), nullable=False),
    sa.Column('actor_ip', sa.String(length=60), nullable=True),
    sa.Column('target', sa.String(length=200), nullable=True),
    sa.Column('old_value', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('new_value', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_at_action', 'audit_events', ['at', 'action'], unique=False)
    op.create_index(op.f('ix_audit_events_action'), 'audit_events', ['action'], unique=False)
    op.create_index(op.f('ix_audit_events_actor'), 'audit_events', ['actor'], unique=False)
    op.create_index(op.f('ix_audit_events_at'), 'audit_events', ['at'], unique=False)
    op.create_index(op.f('ix_audit_events_target'), 'audit_events', ['target'], unique=False)
    op.create_table('catalog_syncs',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('report_id', sa.String(length=80), nullable=True),
    sa.Column('report_document_id', sa.String(length=200), nullable=True),
    sa.Column('listing_count', sa.Integer(), nullable=True),
    sa.Column('in_scope_count', sa.Integer(), nullable=True),
    sa.Column('new_count', sa.Integer(), nullable=False),
    sa.Column('disappeared_count', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('snapshot_path', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('credentials',
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('value_enc', sa.Text(), nullable=False),
    sa.Column('hint', sa.String(length=12), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.String(length=120), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('feed_files',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('filename', sa.String(length=300), nullable=False),
    sa.Column('kind', sa.Enum('FULL', 'DELTA', 'UNKNOWN', name='feed_kind'), nullable=False),
    sa.Column('feed_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sequence', sa.Integer(), nullable=True),
    sa.Column('size_bytes', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('server_modified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('content_sha256', sa.String(length=64), nullable=True),
    sa.Column('status', sa.Enum('DISCOVERED', 'DOWNLOADED', 'VERIFIED', 'PARSED', 'SKIPPED_OLD', 'SKIPPED_DUPLICATE', 'QUARANTINED', 'FAILED', name='file_status'), nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=True),
    sa.Column('rejected_row_count', sa.Integer(), nullable=False),
    sa.Column('quarantine_reason', sa.Text(), nullable=True),
    sa.Column('observed_header', sa.Text(), nullable=True),
    sa.Column('local_path', sa.Text(), nullable=True),
    sa.Column('discovered_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('content_sha256')
    )
    op.create_index(op.f('ix_feed_files_feed_date'), 'feed_files', ['feed_date'], unique=False)
    op.create_index(op.f('ix_feed_files_filename'), 'feed_files', ['filename'], unique=False)
    op.create_index(op.f('ix_feed_files_kind'), 'feed_files', ['kind'], unique=False)
    op.create_index(op.f('ix_feed_files_status'), 'feed_files', ['status'], unique=False)
    op.create_index('ix_feedfile_kind_date', 'feed_files', ['kind', 'feed_date'], unique=False)
    op.create_index('ix_feedfile_status_date', 'feed_files', ['status', 'feed_date'], unique=False)
    op.create_table('notifications',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('severity', sa.String(length=12), nullable=False),
    sa.Column('subject', sa.String(length=300), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('run_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('recipients', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('sent', sa.Boolean(), nullable=False),
    sa.Column('send_error', sa.Text(), nullable=True),
    sa.Column('acknowledged', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_notifications_acknowledged'), 'notifications', ['acknowledged'], unique=False)
    op.create_index(op.f('ix_notifications_at'), 'notifications', ['at'], unique=False)
    op.create_index(op.f('ix_notifications_kind'), 'notifications', ['kind'], unique=False)
    op.create_index(op.f('ix_notifications_run_id'), 'notifications', ['run_id'], unique=False)
    op.create_index(op.f('ix_notifications_sent'), 'notifications', ['sent'], unique=False)
    op.create_index(op.f('ix_notifications_severity'), 'notifications', ['severity'], unique=False)
    op.create_table('runs',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('trigger', sa.Enum('SCHEDULE', 'MANUAL', 'ROLLBACK', 'CATALOG_REFRESH', name='run_trigger'), nullable=False),
    sa.Column('triggered_by', sa.String(length=120), nullable=False),
    sa.Column('mode', sa.Enum('DRY_RUN', 'NEEDS_APPROVAL', 'AUTOMATIC', name='sync_mode'), nullable=False),
    sa.Column('status', sa.Enum('RUNNING', 'COMPLETED', 'NO_CHANGES', 'HALTED_BY_GUARDRAIL', 'AWAITING_APPROVAL', 'DRY_RUN_COMPLETE', 'FAILED', 'PAUSED', name='run_status'), nullable=False),
    sa.Column('files_discovered', sa.Integer(), nullable=False),
    sa.Column('files_processed', sa.Integer(), nullable=False),
    sa.Column('files_skipped', sa.Integer(), nullable=False),
    sa.Column('rows_read', sa.Integer(), nullable=False),
    sa.Column('rows_rejected', sa.Integer(), nullable=False),
    sa.Column('vendor_changes', sa.Integer(), nullable=False),
    sa.Column('proposed_changes', sa.Integer(), nullable=False),
    sa.Column('pushed_changes', sa.Integer(), nullable=False),
    sa.Column('unmapped_count', sa.Integer(), nullable=False),
    sa.Column('guardrail_message', sa.Text(), nullable=True),
    sa.Column('guardrail_detail', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('settings_snapshot', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('duration_seconds', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_run_status_started', 'runs', ['status', 'started_at'], unique=False)
    op.create_index(op.f('ix_runs_started_at'), 'runs', ['started_at'], unique=False)
    op.create_index(op.f('ix_runs_status'), 'runs', ['status'], unique=False)
    op.create_table('settings',
    sa.Column('key', sa.String(length=120), nullable=False),
    sa.Column('value', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('value_type', sa.String(length=24), nullable=False),
    sa.Column('label', sa.String(length=200), nullable=False),
    sa.Column('help_text', sa.Text(), nullable=False),
    sa.Column('category', sa.String(length=40), nullable=False),
    sa.Column('min_value', sa.Float(), nullable=True),
    sa.Column('max_value', sa.Float(), nullable=True),
    sa.Column('choices', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('locked', sa.Boolean(), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.String(length=120), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_index(op.f('ix_settings_category'), 'settings', ['category'], unique=False)
    op.create_table('sku_overrides',
    sa.Column('barcode', sa.String(length=20), nullable=False),
    sa.Column('seller_sku', sa.String(length=120), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.String(length=120), nullable=False),
    sa.PrimaryKeyConstraint('barcode')
    )
    op.create_table('unmapped_barcodes',
    sa.Column('barcode', sa.String(length=20), nullable=False),
    sa.Column('raw_barcode', sa.String(length=40), nullable=False),
    sa.Column('title', sa.String(length=600), nullable=False),
    sa.Column('artist', sa.String(length=400), nullable=False),
    sa.Column('product_format', sa.String(length=40), nullable=False),
    sa.Column('vendor_stock', sa.Integer(), nullable=False),
    sa.Column('vendor_price', sa.Float(), nullable=True),
    sa.Column('attempted_sku', sa.String(length=120), nullable=True),
    sa.Column('reason', sa.String(length=40), nullable=False),
    sa.Column('times_seen', sa.Integer(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved', sa.Boolean(), nullable=False),
    sa.Column('resolved_note', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('barcode')
    )
    op.create_index(op.f('ix_unmapped_barcodes_last_seen_at'), 'unmapped_barcodes', ['last_seen_at'], unique=False)
    op.create_index(op.f('ix_unmapped_barcodes_reason'), 'unmapped_barcodes', ['reason'], unique=False)
    op.create_index(op.f('ix_unmapped_barcodes_resolved'), 'unmapped_barcodes', ['resolved'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=200), nullable=False),
    sa.Column('password_hash', sa.String(length=300), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('totp_secret_enc', sa.Text(), nullable=True),
    sa.Column('totp_enabled', sa.Boolean(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('failed_logins', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_table('push_batches',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('run_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'AWAITING_APPROVAL', 'APPROVED', 'SENDING', 'SENT', 'VERIFIED', 'PARTIALLY_FAILED', 'FAILED', 'REJECTED', 'ROLLED_BACK', name='batch_status'), nullable=False),
    sa.Column('method', sa.Enum('LISTINGS_API', 'FEEDS_API', name='push_method'), nullable=False),
    sa.Column('item_count', sa.Integer(), nullable=False),
    sa.Column('zeroing_count', sa.Integer(), nullable=False),
    sa.Column('raising_count', sa.Integer(), nullable=False),
    sa.Column('lowering_count', sa.Integer(), nullable=False),
    sa.Column('accepted_count', sa.Integer(), nullable=False),
    sa.Column('rejected_count', sa.Integer(), nullable=False),
    sa.Column('verified_count', sa.Integer(), nullable=False),
    sa.Column('approved_by', sa.String(length=120), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rejected_reason', sa.Text(), nullable=True),
    sa.Column('feed_id', sa.String(length=80), nullable=True),
    sa.Column('feed_document_id', sa.String(length=200), nullable=True),
    sa.Column('feed_result_summary', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rollback_of_batch_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('rolled_back_by_batch_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('is_initial_sync', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['rollback_of_batch_id'], ['push_batches.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_push_batches_created_at'), 'push_batches', ['created_at'], unique=False)
    op.create_index(op.f('ix_push_batches_rollback_of_batch_id'), 'push_batches', ['rollback_of_batch_id'], unique=False)
    op.create_index(op.f('ix_push_batches_run_id'), 'push_batches', ['run_id'], unique=False)
    op.create_index(op.f('ix_push_batches_status'), 'push_batches', ['status'], unique=False)
    op.create_table('rejected_rows',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('feed_file_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('line_number', sa.Integer(), nullable=True),
    sa.Column('raw_line', sa.Text(), nullable=False),
    sa.Column('reason', sa.String(length=120), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['feed_file_id'], ['feed_files.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_rejected_rows_feed_file_id'), 'rejected_rows', ['feed_file_id'], unique=False)
    op.create_index(op.f('ix_rejected_rows_reason'), 'rejected_rows', ['reason'], unique=False)
    op.create_table('report_files',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('run_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('feed_file_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('feed_kind', sa.Enum('FULL', 'DELTA', 'UNKNOWN', name='report_feed_kind'), nullable=False),
    sa.Column('filename', sa.String(length=300), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['feed_file_id'], ['feed_files.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_report_files_created_at'), 'report_files', ['created_at'], unique=False)
    op.create_index(op.f('ix_report_files_feed_file_id'), 'report_files', ['feed_file_id'], unique=False)
    op.create_index(op.f('ix_report_files_kind'), 'report_files', ['kind'], unique=False)
    op.create_index(op.f('ix_report_files_run_id'), 'report_files', ['run_id'], unique=False)
    op.create_index('ix_report_run_kind', 'report_files', ['run_id', 'kind'], unique=False)
    op.create_table('vendor_product_history',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('barcode', sa.String(length=20), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('feed_file_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('old_stock', sa.Integer(), nullable=True),
    sa.Column('new_stock', sa.Integer(), nullable=True),
    sa.Column('old_price', sa.Float(), nullable=True),
    sa.Column('new_price', sa.Float(), nullable=True),
    sa.Column('change_type', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['feed_file_id'], ['feed_files.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_vendor_product_history_at'), 'vendor_product_history', ['at'], unique=False)
    op.create_index(op.f('ix_vendor_product_history_barcode'), 'vendor_product_history', ['barcode'], unique=False)
    op.create_index(op.f('ix_vendor_product_history_change_type'), 'vendor_product_history', ['change_type'], unique=False)
    op.create_index(op.f('ix_vendor_product_history_feed_file_id'), 'vendor_product_history', ['feed_file_id'], unique=False)
    op.create_index('ix_vph_barcode_at', 'vendor_product_history', ['barcode', 'at'], unique=False)
    op.create_table('vendor_products',
    sa.Column('barcode', sa.String(length=20), nullable=False),
    sa.Column('raw_barcode', sa.String(length=40), nullable=False),
    sa.Column('artist', sa.String(length=400), nullable=False),
    sa.Column('title', sa.String(length=600), nullable=False),
    sa.Column('price', sa.Float(), nullable=True),
    sa.Column('stock', sa.Integer(), nullable=False),
    sa.Column('product_format', sa.String(length=40), nullable=False),
    sa.Column('checksum_ok', sa.Boolean(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_changed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_full_feed_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('missing_from_full_feeds', sa.Integer(), nullable=False),
    sa.Column('source_file_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.CheckConstraint('stock >= 0', name='ck_vendor_stock_nonneg'),
    sa.ForeignKeyConstraint(['source_file_id'], ['feed_files.id'], ),
    sa.PrimaryKeyConstraint('barcode')
    )
    op.create_index(op.f('ix_vendor_products_last_seen_at'), 'vendor_products', ['last_seen_at'], unique=False)
    op.create_index(op.f('ix_vendor_products_missing_from_full_feeds'), 'vendor_products', ['missing_from_full_feeds'], unique=False)
    op.create_index(op.f('ix_vendor_products_product_format'), 'vendor_products', ['product_format'], unique=False)
    op.create_index(op.f('ix_vendor_products_stock'), 'vendor_products', ['stock'], unique=False)
    op.create_index('ix_vendor_stock_seen', 'vendor_products', ['stock', 'last_seen_at'], unique=False)
    op.create_table('push_items',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('batch_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('seller_sku', sa.String(length=120), nullable=False),
    sa.Column('barcode', sa.String(length=20), nullable=True),
    sa.Column('previous_quantity', sa.Integer(), nullable=True),
    sa.Column('new_quantity', sa.Integer(), nullable=False),
    sa.Column('vendor_stock', sa.Integer(), nullable=True),
    sa.Column('reason', sa.String(length=200), nullable=False),
    sa.Column('result', sa.Enum('PENDING', 'ACCEPTED', 'VERIFIED', 'REJECTED', 'NOT_APPLIED', 'SKIPPED', 'ERROR', name='item_result'), nullable=False),
    sa.Column('amazon_code', sa.String(length=40), nullable=True),
    sa.Column('amazon_message', sa.Text(), nullable=True),
    sa.Column('submission_id', sa.String(length=120), nullable=True),
    sa.Column('verified_quantity', sa.Integer(), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['batch_id'], ['push_batches.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_push_items_amazon_code'), 'push_items', ['amazon_code'], unique=False)
    op.create_index(op.f('ix_push_items_barcode'), 'push_items', ['barcode'], unique=False)
    op.create_index(op.f('ix_push_items_batch_id'), 'push_items', ['batch_id'], unique=False)
    op.create_index(op.f('ix_push_items_result'), 'push_items', ['result'], unique=False)
    op.create_index(op.f('ix_push_items_seller_sku'), 'push_items', ['seller_sku'], unique=False)
    op.create_index('ix_pushitem_batch_result', 'push_items', ['batch_id', 'result'], unique=False)
    op.create_index('ix_pushitem_sku_batch', 'push_items', ['seller_sku', 'batch_id'], unique=False)


def downgrade() -> None:
    """
    Drop every table -- and the undo trail with them.

    THIS IS GUARDED BY CODE, ON PURPOSE, AND NOT BY A COMMENT
    =========================================================
    Downgrading the *initial* revision is not "step back one version". It is
    "delete the database". It destroys ``push_items``, which holds the previous
    quantity of every listing this system has ever changed -- the only thing an
    Undo can be reconstructed from, and the one table here that cannot be
    rebuilt from the vendor's files or from Amazon.

    An earlier version of this file was guarded only by a docstring, while
    ``deploy.sh`` told an operator to run ``alembic downgrade -1`` after a
    failed release. With a single revision in the tree, following that
    instruction would have dropped the entire database. A warning that has to be
    read at 3am by someone whose deploy has just failed is not a safeguard.

    To wipe a development database::

        ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE=I_UNDERSTAND_THIS_DESTROYS_THE_UNDO_TRAIL \
            alembic downgrade base

    In production, restore from a backup instead. ``deploy.sh`` takes one before
    every deployment for exactly this reason.
    """
    if os.environ.get("ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE") != DESTRUCTIVE_OPT_IN:
        raise RuntimeError(
            "Refusing to downgrade the initial revision.\n"
            "\n"
            "This would DROP EVERY TABLE, including push_items -- the record of\n"
            "the previous quantity of every listing this system has changed. That\n"
            "is the undo trail, and it cannot be rebuilt from the vendor's files\n"
            "or from Amazon.\n"
            "\n"
            "If a release went wrong, roll the CODE back and restore the database\n"
            "from the backup deploy.sh took before the deployment. Do not\n"
            "downgrade this revision.\n"
            "\n"
            "If you really do mean to wipe a development database, set:\n"
            f"  ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE={DESTRUCTIVE_OPT_IN}"
        )

    op.drop_index('ix_pushitem_sku_batch', table_name='push_items')
    op.drop_index('ix_pushitem_batch_result', table_name='push_items')
    op.drop_index(op.f('ix_push_items_seller_sku'), table_name='push_items')
    op.drop_index(op.f('ix_push_items_result'), table_name='push_items')
    op.drop_index(op.f('ix_push_items_batch_id'), table_name='push_items')
    op.drop_index(op.f('ix_push_items_barcode'), table_name='push_items')
    op.drop_index(op.f('ix_push_items_amazon_code'), table_name='push_items')
    op.drop_table('push_items')
    op.drop_index('ix_vendor_stock_seen', table_name='vendor_products')
    op.drop_index(op.f('ix_vendor_products_stock'), table_name='vendor_products')
    op.drop_index(op.f('ix_vendor_products_product_format'), table_name='vendor_products')
    op.drop_index(op.f('ix_vendor_products_missing_from_full_feeds'), table_name='vendor_products')
    op.drop_index(op.f('ix_vendor_products_last_seen_at'), table_name='vendor_products')
    op.drop_table('vendor_products')
    op.drop_index('ix_vph_barcode_at', table_name='vendor_product_history')
    op.drop_index(op.f('ix_vendor_product_history_feed_file_id'), table_name='vendor_product_history')
    op.drop_index(op.f('ix_vendor_product_history_change_type'), table_name='vendor_product_history')
    op.drop_index(op.f('ix_vendor_product_history_barcode'), table_name='vendor_product_history')
    op.drop_index(op.f('ix_vendor_product_history_at'), table_name='vendor_product_history')
    op.drop_table('vendor_product_history')
    op.drop_index('ix_report_run_kind', table_name='report_files')
    op.drop_index(op.f('ix_report_files_run_id'), table_name='report_files')
    op.drop_index(op.f('ix_report_files_kind'), table_name='report_files')
    op.drop_index(op.f('ix_report_files_feed_file_id'), table_name='report_files')
    op.drop_index(op.f('ix_report_files_created_at'), table_name='report_files')
    op.drop_table('report_files')
    op.drop_index(op.f('ix_rejected_rows_reason'), table_name='rejected_rows')
    op.drop_index(op.f('ix_rejected_rows_feed_file_id'), table_name='rejected_rows')
    op.drop_table('rejected_rows')
    op.drop_index(op.f('ix_push_batches_status'), table_name='push_batches')
    op.drop_index(op.f('ix_push_batches_run_id'), table_name='push_batches')
    op.drop_index(op.f('ix_push_batches_rollback_of_batch_id'), table_name='push_batches')
    op.drop_index(op.f('ix_push_batches_created_at'), table_name='push_batches')
    op.drop_table('push_batches')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
    op.drop_index(op.f('ix_unmapped_barcodes_resolved'), table_name='unmapped_barcodes')
    op.drop_index(op.f('ix_unmapped_barcodes_reason'), table_name='unmapped_barcodes')
    op.drop_index(op.f('ix_unmapped_barcodes_last_seen_at'), table_name='unmapped_barcodes')
    op.drop_table('unmapped_barcodes')
    op.drop_table('sku_overrides')
    op.drop_index(op.f('ix_settings_category'), table_name='settings')
    op.drop_table('settings')
    op.drop_index(op.f('ix_runs_status'), table_name='runs')
    op.drop_index(op.f('ix_runs_started_at'), table_name='runs')
    op.drop_index('ix_run_status_started', table_name='runs')
    op.drop_table('runs')
    op.drop_index(op.f('ix_notifications_severity'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_sent'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_run_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_kind'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_at'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_acknowledged'), table_name='notifications')
    op.drop_table('notifications')
    op.drop_index('ix_feedfile_status_date', table_name='feed_files')
    op.drop_index('ix_feedfile_kind_date', table_name='feed_files')
    op.drop_index(op.f('ix_feed_files_status'), table_name='feed_files')
    op.drop_index(op.f('ix_feed_files_kind'), table_name='feed_files')
    op.drop_index(op.f('ix_feed_files_filename'), table_name='feed_files')
    op.drop_index(op.f('ix_feed_files_feed_date'), table_name='feed_files')
    op.drop_table('feed_files')
    op.drop_table('credentials')
    op.drop_table('catalog_syncs')
    op.drop_index(op.f('ix_audit_events_target'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_at'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_actor'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_action'), table_name='audit_events')
    op.drop_index('ix_audit_at_action', table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index('ix_listing_prefix_status', table_name='amazon_listings')
    op.drop_index('ix_listing_barcode_prefix', table_name='amazon_listings')
    op.drop_index(op.f('ix_amazon_listings_synced_at'), table_name='amazon_listings')
    op.drop_index(op.f('ix_amazon_listings_status'), table_name='amazon_listings')
    op.drop_index(op.f('ix_amazon_listings_sku_prefix'), table_name='amazon_listings')
    op.drop_index(op.f('ix_amazon_listings_quantity'), table_name='amazon_listings')
    op.drop_index(op.f('ix_amazon_listings_present_in_last_sync'), table_name='amazon_listings')
    op.drop_index(op.f('ix_amazon_listings_fulfillment_channel'), table_name='amazon_listings')
    op.drop_index(op.f('ix_amazon_listings_blacklisted'), table_name='amazon_listings')
    op.drop_index(op.f('ix_amazon_listings_barcode'), table_name='amazon_listings')
    op.drop_index(op.f('ix_amazon_listings_asin'), table_name='amazon_listings')
    op.drop_table('amazon_listings')
