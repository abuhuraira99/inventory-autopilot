"""
Deleting old files, and noticing before the disk fills.

WHY THIS MATTERS MORE THAN IT SOUNDS
====================================
Three things in this system used to grow without any limit at all, and the
failure they cause is the quiet kind. When the disk is full the vendor's file
cannot be downloaded, nothing can be written -- including the record of the
problem, and the alert about it -- and Amazon simply carries on showing whatever
it last showed. Nobody is told, because telling somebody requires a write.

The worst of them: `report_retention_days` existed as a setting, appeared on the
Settings page, and its own help text said "older report files are deleted to
stop the disk filling up". No code read it. A setting that promises something
and does nothing is worse than no setting, because it stops anyone looking.

WHY THESE TESTS ARE CAREFUL ABOUT WHAT IS *NOT* DELETED
=======================================================
This is code whose whole job is deleting the client's files. The cases that
matter most here are the ones where it must keep its hands off: a quarantined
archive, a recent file, a database row.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.core import settings_store
from app.engine.pipeline import RunOutcome, _housekeeping
from app.models import (
    FeedFile,
    FeedKind,
    FileStatus,
    Notification,
    ReportFile,
    Run,
    RunStatus,
    RunTrigger,
    SyncMode,
    utcnow,
)


@pytest.fixture
def run(session) -> Run:
    r = Run(
        trigger=RunTrigger.SCHEDULE, triggered_by="test",
        mode=SyncMode.DRY_RUN, status=RunStatus.COMPLETED,
    )
    session.add(r)
    session.flush()
    return r


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """Point the app's data directory at a temporary one."""
    import app.engine.pipeline as pipeline

    monkeypatch.setattr(pipeline.app_settings, "data_dir", tmp_path)
    (tmp_path / "quarantine").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reports").mkdir(parents=True, exist_ok=True)
    (tmp_path / "snapshots").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _archive(session, data_dir, name, *, days_old, status=FileStatus.PARSED, size=1024):
    """A downloaded feed archive on disk, with a matching database row."""
    path = data_dir / "quarantine" / name
    path.write_bytes(b"x" * size)
    record = FeedFile(
        filename=name,
        kind=FeedKind.FULL,
        status=status,
        local_path=str(path),
        processed_at=(utcnow() - timedelta(days=days_old)),
    )
    session.add(record)
    session.flush()
    return record, path


def _report(session, run, data_dir, name, *, days_old, size=2048):
    path = data_dir / "reports" / name
    path.write_bytes(b"y" * size)
    record = ReportFile(
        run_id=run.id,
        kind="full_price_changed",
        filename=name,
        path=str(path),
        created_at=(utcnow() - timedelta(days=days_old)),
    )
    session.add(record)
    session.flush()
    return record, path


def _cfg(session, **overrides):
    for key, value in overrides.items():
        settings_store.set_value(session, key, value, actor="test")
    session.flush()
    return settings_store.get_all(session)


# ===========================================================================
# Vendor archives
# ===========================================================================


class TestVendorArchives:
    def test_an_old_processed_archive_is_deleted(self, session, run, data_dir):
        """
        The daily full feed is 75 MB. Kept forever, that is about 2.2 GB a
        month, which fills a small VPS disk in weeks.
        """
        record, path = _archive(session, data_dir, "FULL_FEED_1_20260101.zip", days_old=10)
        cfg = _cfg(session, keep_feed_files_days=3)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert not path.exists(), "the old archive was not deleted"
        assert record.local_path is None, (
            "local_path must be cleared, or every later run retries the same delete"
        )

    def test_the_database_row_survives(self, session, run, data_dir):
        """
        Only the file goes. feed_files holds the content hash that stops a file
        being processed twice -- delete the row and the vendor's next re-upload
        of identical content would be ingested all over again.
        """
        record, _ = _archive(session, data_dir, "FULL_FEED_1_20260101.zip", days_old=10)
        record.content_sha256 = "abc123"
        session.flush()
        cfg = _cfg(session, keep_feed_files_days=3)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert session.get(FeedFile, record.id) is not None
        assert session.get(FeedFile, record.id).content_sha256 == "abc123"

    def test_a_recent_archive_is_kept(self, session, run, data_dir):
        record, path = _archive(session, data_dir, "FULL_FEED_1_20260907.zip", days_old=1)
        cfg = _cfg(session, keep_feed_files_days=3)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert path.exists(), "a file inside the retention window was deleted"

    def test_a_quarantined_archive_is_never_deleted(self, session, run, data_dir):
        """
        THE ONE THAT MUST NOT BE TIDIED AWAY.

        A rejected archive is exactly the file a human needs to open to find out
        what the vendor changed, and it is also the rarest. Deleting the
        evidence of a problem to reclaim 75 MB is a bad trade at any disk size.
        """
        record, path = _archive(
            session, data_dir, "FULL_FEED_1_20250101.zip",
            days_old=400, status=FileStatus.QUARANTINED,
        )
        cfg = _cfg(session, keep_feed_files_days=1)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert path.exists(), "a quarantined archive was deleted"
        assert record.local_path is not None

    def test_a_file_already_gone_is_not_an_error(self, session, run, data_dir):
        """
        A duplicate download deletes the file immediately but leaves local_path
        set. Housekeeping must cope rather than raising on a missing file.
        """
        record, path = _archive(session, data_dir, "DELTA_FEED_1_20260101_1.zip", days_old=10)
        path.unlink()
        cfg = _cfg(session, keep_feed_files_days=3)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert record.local_path is None

    def test_zero_days_deletes_as_soon_as_it_is_read(self, session, run, data_dir):
        """The setting the operator of a very small disk will want."""
        _, path = _archive(session, data_dir, "FULL_FEED_1_20260907.zip", days_old=0)
        cfg = _cfg(session, keep_feed_files_days=0)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert not path.exists()


# ===========================================================================
# Reports
# ===========================================================================


class TestReports:
    def test_old_report_files_are_deleted(self, session, run, data_dir):
        """
        The setting that promised this and did nothing. Five .xlsx files are
        written per run, and a run happens whenever a delta arrives.
        """
        _, old = _report(session, run, data_dir, "old.xlsx", days_old=120)
        _, new = _report(session, run, data_dir, "new.xlsx", days_old=2)
        cfg = _cfg(session, report_retention_days=90)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert not old.exists(), "report_retention_days is still not enforced"
        assert new.exists(), "a report inside the retention window was deleted"

    def test_the_report_rows_survive(self, session, run, data_dir):
        """
        The help text promises "the database records stay", so they must. The
        row is how the dashboard explains what a past run produced.
        """
        record, path = _report(session, run, data_dir, "old.xlsx", days_old=120)
        cfg = _cfg(session, report_retention_days=90)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert not path.exists()
        assert session.get(ReportFile, record.id) is not None


# ===========================================================================
# Catalogue snapshots
# ===========================================================================


class TestCatalogueSnapshots:
    def test_old_snapshots_are_deleted_and_recent_ones_kept(
        self, session, run, data_dir, monkeypatch
    ):
        """
        These are Amazon's own listing reports, and they are what "restore the
        account to how it looked on a past day" reads. Kept longer than
        anything else for that reason, but not forever at 5 MB a day.
        """
        import os
        import time

        old = data_dir / "snapshots" / "listings-2026-01-01.txt"
        new = data_dir / "snapshots" / "listings-2026-09-06.txt"
        for p in (old, new):
            p.write_bytes(b"z" * 512)

        long_ago = time.time() - (60 * 86400)
        os.utime(old, (long_ago, long_ago))

        cfg = _cfg(session, keep_catalog_snapshots_days=30)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert not old.exists()
        assert new.exists()


# ===========================================================================
# Vendor change history -- the biggest table
# ===========================================================================


class TestVendorHistory:
    def test_old_history_is_pruned_and_recent_history_kept(self, session, run, data_dir):
        """
        The largest table in the database and the last unbounded one. A row is
        written for every stock OR price change, and the first full feed alone
        writes 1,158,340 of them. On a small disk this is what fills it.
        """
        from app.models import VendorProductHistory

        old = VendorProductHistory(
            barcode="0015047810567", change_type="stock",
            old_stock=131, new_stock=0, at=utcnow() - timedelta(days=400),
        )
        recent = VendorProductHistory(
            barcode="0008811060626", change_type="price",
            old_price=9.84, new_price=10.10, at=utcnow() - timedelta(days=5),
        )
        session.add_all([old, recent])
        session.flush()
        cfg = _cfg(session, keep_vendor_history_days=180)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        remaining = {h.barcode for h in session.query(VendorProductHistory).all()}
        assert remaining == {"0008811060626"}, (
            "expected only the recent row to survive"
        )

    def test_the_minimum_retention_is_enforced_by_the_setting(self, session):
        """
        Seven days is the floor. The table is what the client's own reports are
        built from, so someone trimming it to nothing would break those rather
        than just save space -- the setting refuses rather than allowing it.
        """
        from app.core.settings_store import SettingError

        with pytest.raises(SettingError):
            settings_store.set_value(session, "keep_vendor_history_days", 1, actor="test")


# ===========================================================================
# The disk warning
# ===========================================================================


class TestDiskWarning:
    def test_low_space_raises_a_critical_alert(self, session, run, data_dir, monkeypatch):
        """
        The whole point of the check. It has to fire while there is still room
        to write the alert that says so.
        """
        import app.engine.pipeline as pipeline

        class _Usage:
            total = 30 * 1024**3
            used = 29 * 1024**3
            free = int(0.4 * 1024**3)     # 0.4 GB

        monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _p: _Usage())
        cfg = _cfg(session, min_free_disk_gb=2.0)
        outcome = RunOutcome(run_id=run.id, status=run.status)

        _housekeeping(session, run, cfg, outcome)

        note = session.query(Notification).filter(Notification.kind == "low_disk").one()
        assert note.severity == "critical"
        assert "0.4 GB" in note.subject
        # The message must say what to actually do, not just that it is bad.
        assert "Keep report files" in note.body
        assert any("low disk space" in e for e in outcome.errors)

    def test_plenty_of_space_says_nothing(self, session, run, data_dir, monkeypatch):
        import app.engine.pipeline as pipeline

        class _Usage:
            total = 200 * 1024**3
            used = 10 * 1024**3
            free = 190 * 1024**3

        monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _p: _Usage())
        cfg = _cfg(session, min_free_disk_gb=2.0)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert session.query(Notification).filter(Notification.kind == "low_disk").count() == 0

    def test_the_check_can_be_switched_off(self, session, run, data_dir, monkeypatch):
        import app.engine.pipeline as pipeline

        class _Usage:
            total = 30 * 1024**3
            used = 30 * 1024**3
            free = 0

        monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _p: _Usage())
        cfg = _cfg(session, min_free_disk_gb=0)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert session.query(Notification).filter(Notification.kind == "low_disk").count() == 0


# ===========================================================================
# It must never break a run
# ===========================================================================


class TestItNeverBreaksARun:
    """
    Housekeeping is called from a `finally`, inside its own try/except. Being
    unable to delete an old file is not worth turning a successful sync into a
    failed one -- and on Windows a file held open by a virus scanner or a backup
    agent is routine rather than exotic.
    """

    def test_a_file_that_cannot_be_deleted_is_logged_and_stepped_over(
        self, session, run, data_dir, monkeypatch
    ):
        """The Windows case: something else holds the file open."""
        record, path = _archive(session, data_dir, "FULL_FEED_1_20260101.zip", days_old=10)

        real_unlink = Path.unlink

        def locked(self, *a, **k):
            if self.name == "FULL_FEED_1_20260101.zip":
                raise OSError(32, "The process cannot access the file")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", locked)
        cfg = _cfg(session, keep_feed_files_days=3)

        # No exception, and the row is left pointing at the file so the next
        # run tries again once whatever held it open has let go.
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert path.exists()
        assert record.local_path is not None, "a file that survived must stay tracked"

    def test_a_missing_data_directory_is_not_an_error(self, session, run, tmp_path, monkeypatch):
        """A fresh install, before anything has been written."""
        import app.engine.pipeline as pipeline

        monkeypatch.setattr(pipeline.app_settings, "data_dir", tmp_path / "not-created-yet")
        cfg = settings_store.get_all(session)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))
