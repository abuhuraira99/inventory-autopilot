"""
When something goes wrong, it has to be possible to find out what.

WHY THIS FILE EXISTS
====================
A catalogue refresh failed four times in half an hour on the live server and
produced nothing at all: no dashboard alert, no error, no log. Two separate
defects combined to make that possible.

  1. The application logged only to stdout. Under Docker that is the log, and
     the platform captures it. On Windows the supported deployment is a
     Scheduled Task running uvicorn, and a Scheduled Task's stdout is discarded
     by the operating system -- so on the platform the client actually runs,
     every diagnostic was thrown away.

  2. The refresh job caught one exception type and alerted on it. Everything
     else -- an expired token, a missing role, an unwritable snapshot directory
     -- escaped into the scheduler, which logged it to that same discarded
     stream and carried on.

Being unable to answer "what did it do?" is worse than most bugs. It turns
every question into a guess, and this system exists precisely so that nobody
has to guess about a live seller account.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest


class TestTheLogReachesADisk:
    def test_configure_writes_to_a_file_under_the_data_directory(
        self, tmp_path, monkeypatch
    ) -> None:
        """
        A log file must exist and contain what was logged.

        Asserted by reading the file back rather than by checking the handler
        list, because a handler pointing at an unwritable path satisfies the
        second and not the first.
        """
        from app import logging_setup
        from app.config import settings

        monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
        logging_setup.configure()

        logging.getLogger("test.visibility").warning("a line that must be findable")

        for handler in logging.getLogger().handlers:
            handler.flush()

        log_file = tmp_path / "logs" / "app.log"
        assert log_file.exists(), "no log file was created"
        assert "a line that must be findable" in log_file.read_text(encoding="utf-8")

    def test_an_unwritable_log_location_does_not_stop_the_application(
        self, tmp_path, monkeypatch
    ) -> None:
        """
        Failing to open the log file must not prevent booting.

        Refusing to start because a log file is unwritable would trade a
        degraded system for no system, and stdout still works. The path is made
        impossible by pointing the data directory at a file.
        """
        from app import logging_setup
        from app.config import settings

        blocker = tmp_path / "not-a-directory"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setattr(settings, "data_dir", blocker, raising=False)

        logging_setup.configure()   # must not raise

        assert logging.getLogger().handlers, "logging was left with no handlers at all"


class TestTheCatalogueRefreshAlwaysReportsBack:
    """
    Pressing the button and receiving nothing is not an acceptable outcome.

    The job is driven directly with the lock and the session substituted, so
    these exercise the real error handling rather than a copy of it. The
    PostgreSQL advisory lock cannot be taken against the SQLite test database,
    which is exactly why it is replaced here rather than worked around.
    """

    @pytest.fixture()
    def driven(self, session, monkeypatch):
        from app import scheduler

        @contextmanager
        def granted_lock():
            yield True

        @contextmanager
        def fixed_session():
            yield session

        monkeypatch.setattr(scheduler, "run_lock", granted_lock)
        monkeypatch.setattr(scheduler, "session_scope", fixed_session)
        return scheduler

    def _alerts(self, session) -> list:
        from app.models import Notification

        return session.query(Notification).all()

    def test_missing_amazon_credentials_raise_an_alert(
        self, session, driven, monkeypatch
    ) -> None:
        """
        The silent dead end. It used to log a warning and return.

        A warning is invisible on a Scheduled Task, so the operator pressed the
        button and the dashboard said nothing whatsoever.
        """
        monkeypatch.setattr(driven.services, "amazon_client", lambda *a, **k: None)

        driven.job_catalog_refresh()

        alerts = self._alerts(session)
        assert len(alerts) == 1
        assert "not fully configured" in alerts[0].body
        assert "Test Amazon" in alerts[0].body, "say how to find out which part is missing"

    def test_an_unexpected_failure_raises_an_alert_naming_its_type(
        self, session, driven, monkeypatch
    ) -> None:
        """
        The one that actually escaped: anything that is not a ReportError.

        An expired refresh token surfaces as an SpApiError, not a ReportError,
        so it went straight past the handler and into the scheduler. The class
        name has to be in the message: "connection refused" reads identically
        whether it came from Amazon or from the local disk.
        """
        class _Client:
            closed = False

            def close(self) -> None:
                type(self).closed = True

        monkeypatch.setattr(driven.services, "amazon_client", lambda *a, **k: _Client())

        def explode(*_args, **_kwargs):
            raise RuntimeError("the refresh token has expired")

        monkeypatch.setattr(driven, "fetch_all_listings", explode)

        driven.job_catalog_refresh()   # must not raise

        alerts = self._alerts(session)
        assert len(alerts) == 1
        assert "RuntimeError" in alerts[0].body
        assert "the refresh token has expired" in alerts[0].body
        assert _Client.closed, "the client must still be closed on the failure path"
