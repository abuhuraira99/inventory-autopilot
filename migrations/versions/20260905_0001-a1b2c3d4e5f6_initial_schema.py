"""Initial schema.

Revision ID: a1b2c3d4e5f6
Revises: none
Created: 2026-09-05

WHY THIS ONE IS DIFFERENT
=========================
The first migration builds the whole schema from the SQLAlchemy models rather
than from hand-written DDL. That is a deliberate choice for an initial
revision: twelve tables with about thirty indexes, transcribed by hand, is a
large surface for a typo that would only show up as a subtly missing index
under production load.

From here on, migrations are proper diffs -- ``alembic revision --autogenerate``
compares the models against the live database and writes the delta, which is
reviewable and reversible in the normal way.

WHAT IT CREATES
===============
    settings                 client-editable behaviour
    credentials              the four encrypted secrets
    users                    the single shared dashboard login
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

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create every table, index and enum defined by the models."""
    from app.models import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    """
    Drop everything.

    Guarded by a comment rather than by code because Alembic downgrades are run
    deliberately: this destroys the rollback trail, which is the one thing in
    this database that cannot be reconstructed from the vendor's files or from
    Amazon. Take a backup first.
    """
    from app.models import Base

    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, checkfirst=True)
