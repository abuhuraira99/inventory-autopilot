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
from sqlalchemy import text


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


class TestARunNeverStaysRunningForever:
    """
    A run interrupted by a restart must not sit on the dashboard as "Running".

    execute_run marks a run FAILED on every exception it can see, but it cannot
    write anything when the process itself stops mid-run: a restart, a Ctrl+C,
    a Scheduled Task being stopped. The row keeps the status it was created
    with and stays RUNNING for ever.

    On the first deployment three of these accumulated during an afternoon of
    updates. The dashboard header reported "Running" indefinitely, and when a
    genuine fault appeared later it was impossible to tell the live run from
    the corpses -- which is the exact opposite of what a status page is for.
    """

    def _run(self, session, status):
        from app.models import Run, RunTrigger

        run = Run(status=status, trigger=RunTrigger.SCHEDULE, triggered_by="test")
        session.add(run)
        session.commit()
        return run

    def test_a_run_left_running_is_closed_at_startup(self, session) -> None:
        from app.engine.pipeline import close_interrupted_runs
        from app.models import RunStatus

        run = self._run(session, RunStatus.RUNNING)

        assert close_interrupted_runs(session) == 1

        session.refresh(run)
        assert run.status is RunStatus.FAILED
        assert run.finished_at is not None
        assert "Interrupted" in (run.error or "")
        # The operator's real question is "did it send half a batch?"
        assert "undone" in (run.error or "")

    def test_finished_runs_are_left_exactly_as_they_are(self, session) -> None:
        """
        Only RUNNING rows are touched.

        A sweep that rewrote history would destroy the record this system is
        built to keep -- and NO_CHANGES, the ordinary steady-state outcome,
        must never be relabelled as a failure.
        """
        from app.engine.pipeline import close_interrupted_runs
        from app.models import RunStatus

        done = self._run(session, RunStatus.NO_CHANGES)
        completed = self._run(session, RunStatus.COMPLETED)

        assert close_interrupted_runs(session) == 0

        session.refresh(done)
        session.refresh(completed)
        assert done.status is RunStatus.NO_CHANGES
        assert completed.status is RunStatus.COMPLETED

    def test_every_interrupted_run_is_closed_not_just_the_newest(self, session) -> None:
        """
        Three had piled up on the real server, not one.

        Closing only the most recent would leave the older ones on the Runs
        page still claiming to be in progress.
        """
        from app.engine.pipeline import close_interrupted_runs
        from app.models import RunStatus

        runs = [self._run(session, RunStatus.RUNNING) for _ in range(3)]

        assert close_interrupted_runs(session) == 3

        for run in runs:
            session.refresh(run)
            assert run.status is RunStatus.FAILED


class TestAFailedRunCanRecordThatItFailed:
    """
    A database error leaves the session unusable, including for the handler
    whose entire job is to write down what went wrong.

    On the live server a single NUL byte in a vendor feed aborted the
    transaction. Every statement afterwards raised PendingRollbackError -- so
    the except block could not set the status, could not queue the alert, and
    could not even read ``run.id`` to format its own log line. It raised a
    second exception on top of the first, the run row kept the status it was
    created with, and the dashboard showed "Running" indefinitely while the
    real cause was buried under a rollback error that says nothing about it.
    """

    def test_the_session_is_recovered_and_the_run_comes_back_alive(
        self, session
    ) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        from app.engine.pipeline import _recover_session
        from app.models import Run, RunStatus, RunTrigger

        run = Run(status=RunStatus.RUNNING, trigger=RunTrigger.SCHEDULE, triggered_by="t")
        session.add(run)
        session.commit()
        run_id = run.id

        # Poison the transaction the way a bad row does.
        with pytest.raises(SQLAlchemyError):
            session.execute(text("SELECT * FROM a_table_that_does_not_exist"))

        recovered = _recover_session(session, run, run_id)

        assert recovered.id == run_id
        # The proof: the session can be written to again, which is what the
        # failure handler needs and could not do before.
        recovered.status = RunStatus.FAILED
        session.commit()

        session.expire_all()
        assert session.get(Run, run_id).status is RunStatus.FAILED

    def test_a_healthy_session_is_left_working(self, session) -> None:
        """
        The recovery runs on every failure path, including the ordinary ones.

        Nothing is lost by rolling back here: everything up to this point was
        already committed by ``checkpoint`` when the run opened.
        """
        from app.engine.pipeline import _recover_session
        from app.models import Run, RunStatus, RunTrigger

        run = Run(status=RunStatus.RUNNING, trigger=RunTrigger.SCHEDULE, triggered_by="t")
        session.add(run)
        session.commit()

        recovered = _recover_session(session, run, run.id)

        assert recovered.id == run.id
        assert recovered.status is RunStatus.RUNNING
