"""
Database engine, sessions, and the run lock.

THE RUN LOCK
============
Exactly one pipeline run may execute at a time. Two overlapping runs would each
read Amazon's quantity, each compute a change from the same starting point, and
each push -- producing double writes and a corrupt rollback trail.

We use a PostgreSQL *advisory* lock rather than a row lock or a file lock:

  * it is held by the database session, so a crashed worker releases it
    automatically -- no stale lock file to clear by hand at 3am;
  * it costs nothing when uncontended;
  * it works across processes and containers, which a threading.Lock does not.

``try_advisory_lock`` returns immediately rather than waiting. A run that
cannot get the lock logs "another run is in progress" and exits, which is the
correct behaviour for a scheduler firing every 15 minutes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

log = logging.getLogger(__name__)

#: Arbitrary but fixed 64-bit key identifying "the pipeline run lock".
#: Must not collide with any other advisory lock in this database.
RUN_LOCK_ID = 0x494E5641  # "INVA" in hex, for INVentory Autopilot


def _build_engine() -> Engine:
    """Create the engine with settings tuned for this workload."""
    is_sqlite = settings.database_url.startswith("sqlite")

    kwargs: dict = {
        "echo": settings.debug and settings.environment == "development",
        # Recycle before most cloud providers' idle timeout so the first query
        # after a quiet night does not fail with a stale connection.
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
    if not is_sqlite:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
    else:
        # SQLite is supported for the test suite only, never for production:
        # a full feed upserts 1.15 million rows while the dashboard is
        # serving reads, and SQLite's single-writer lock makes that unusable.
        kwargs["connect_args"] = {"check_same_thread": False}

    eng = create_engine(settings.database_url, **kwargs)

    if is_sqlite:
        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return eng


engine: Engine = _build_engine()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,  # keeps objects usable after commit, avoids reloads
)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session that always closes."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """
    Transactional scope for background work.

    Commits on success, rolls back on any exception, always closes. Use this in
    the scheduler and in scripts; use :func:`get_session` in request handlers.
    """
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@contextmanager
def run_lock(*, lock_id: int = RUN_LOCK_ID) -> Iterator[bool]:
    """
    Try to take the exclusive run lock. Yields True if acquired, False if not.

    Always released, including on an exception, because the lock is bound to
    this session and the session is closed in the ``finally``.

    On SQLite (tests only) this is a no-op that always succeeds -- there is no
    concurrency to protect against in a single-process test run.

    Usage::

        with run_lock() as got_it:
            if not got_it:
                log.info("another run is already in progress; skipping")
                return
            ...do the run...
    """
    if settings.database_url.startswith("sqlite"):
        yield True
        return

    conn = engine.connect()
    acquired = False
    try:
        acquired = bool(
            conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_id}).scalar()
        )
        yield acquired
    finally:
        if acquired:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_id})
            except Exception:  # pragma: no cover - best effort on teardown
                log.warning("could not release advisory lock %s", lock_id, exc_info=True)
        conn.close()


def healthcheck() -> tuple[bool, str]:
    """Cheap liveness probe for the /health endpoint and the uptime monitor."""
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:  # pragma: no cover
        return False, str(exc)
