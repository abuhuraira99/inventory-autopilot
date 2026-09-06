"""
Shared test fixtures.

The whole suite runs against an in-memory SQLite database and never touches the
network. That is a deliberate constraint: a test that can reach Amazon is a
test that can change a live listing, and no amount of care makes that safe to
have in a CI pipeline.

Anything that genuinely needs a live service is marked ``@pytest.mark.integration``
and excluded by default.
"""

from __future__ import annotations

import base64
import os
import secrets

# Environment must be set BEFORE app.config is imported anywhere, because the
# settings object is built at import time and cached.
os.environ.setdefault("MASTER_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(32))
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "false")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.models import Base  # noqa: E402


@pytest.fixture
def engine():
    """
    A fresh in-memory database per test.

    ``StaticPool`` keeps one connection alive for the whole fixture, which is
    required for ``:memory:`` -- otherwise each checkout gets its own empty
    database and nothing persists between statements.
    """
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session(engine) -> Session:
    """A committed-per-test session bound to the in-memory database."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


# ---------------------------------------------------------------------------
# Real production data, distilled into fixtures
# ---------------------------------------------------------------------------
# These values are taken from the client's actual files, verified 2026-09-04.
# Using real shapes rather than invented ones is the point: the bugs this system
# must not have are bugs about *this* vendor's quirks.

#: The exact header line of every observed feed file, full and delta alike.
REAL_FEED_HEADER = "barcode|artist|title|price|stock|format"

#: Genuine rows from FULL_FEED_110708_20260901. Note the stripped leading
#: zeros on the shorter barcodes -- that is the vendor's own output, not a
#: transcription error.
REAL_FEED_ROWS = [
    "5413356068320|GARNIER,LAURENT|RETROSPECTIVE|12.12|0|CD",
    "5413356682021|GARNIER,LAURENT|CLOUD MAKING MACHINE|9.00|0|CD",
    "15047810567|FOSTER,RUTHIE|MILEAGE (BABY BLUE VINYL)|17.73|131|LP",
    "8811060626|CARLISLE,BELINDA|HER GREATEST HITS|9.84|2|CD",
    "8138010236|SMART TRAVELS EUROPE|SALZBURG & THE LAKES|3.99|2|DVD",
]

#: Genuine SKUs from All+Listings+Report_09-04-2026.txt. The barcode part is
#: always padded to 13 digits -- the fact the whole project hinges on.
REAL_AMS_SKUS = [
    "HA-AMS-0008811065126",
    "HA-AMS-0008811096427",
    "HA-AMS-0010058212225",
    "HA-AMS-3341348053448",   # already 13 digits, no padding needed
    "HA-AMS-0634457195035",
]

#: SKUs from other suppliers on the same account. The system must never touch
#: these. Counts as of 2026-09-04: HA-INGR- 22,523; RA-HM- 4,158; RA-OLD- 2,170.
REAL_OUT_OF_SCOPE_SKUS = [
    "HA-INGR-9798385266500",
    "RA-HM-1234567",
    "RA-OLD-013431496021",
    "AS-OLD-015095733528",
    "HB-WOB-1234567890123-V.G",
]

#: Genuinely damaged SKUs found live on the account. Real data-entry accidents,
#: kept as fixtures so the parser is never allowed to choke on them.
REAL_MALFORMED_SKUS = [
    ": HA-INGR-9798385266500",   # leading colon and space from a paste
    "A-AMS-088438889432",        # missing the leading H
    "18-HM-7756615",
]


@pytest.fixture
def feed_header() -> str:
    return REAL_FEED_HEADER


@pytest.fixture
def feed_rows() -> list[str]:
    return list(REAL_FEED_ROWS)


@pytest.fixture
def feed_text(feed_header, feed_rows) -> str:
    """A complete miniature feed file, exactly as the vendor formats it."""
    return "\n".join([feed_header, *feed_rows]) + "\n"


@pytest.fixture
def ams_skus() -> list[str]:
    return list(REAL_AMS_SKUS)


@pytest.fixture
def out_of_scope_skus() -> list[str]:
    return list(REAL_OUT_OF_SCOPE_SKUS)
