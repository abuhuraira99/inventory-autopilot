"""
The sync timetable must not move when the server is restarted.

An interval trigger with no start date counts from the moment the process
starts, so every restart re-phased the whole schedule: checks landing at 17
past moved to 31 past, then to 09 past, then wherever the next restart fell.
Over a week of updates the timetable wandered around the clock.

That is worse than untidy. The vendor publishes on a fixed clock -- the full
feed at about 8 PM their time -- so when our checks happen decides how long a
new file waits before anybody sees it. A schedule that moves cannot be reasoned
about, and an operator restarting to pick up a change should not have to work
out whether the run that just appeared was caused by the restart.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest


@contextmanager
def _no_session():
    yield None


def _fires(trigger, count: int = 4) -> list[datetime]:
    """The next few fire times, walked forward from now."""
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    out: list[datetime] = []
    previous = None
    for _ in range(count):
        nxt = trigger.get_next_fire_time(previous, now if previous is None else previous)
        out.append(nxt)
        previous = nxt
    return out


@pytest.fixture()
def wired(monkeypatch):
    """The scheduler module with the timezone and the offset under our control."""
    from app import scheduler

    monkeypatch.setattr(scheduler, "session_scope", _no_session)
    monkeypatch.setattr(scheduler, "_timezone", lambda: ZoneInfo("America/Los_Angeles"))

    def set_offset(value: int) -> None:
        monkeypatch.setattr(
            scheduler.settings_store, "get", lambda _session, key: value
        )

    return scheduler, set_offset


def test_every_check_lands_on_the_configured_minute(wired) -> None:
    """
    The point of the whole change: a fixed, predictable timetable.

    Asserted on the fire times rather than on the start date, because the start
    date being right is only useful if the times it produces are.
    """
    scheduler, set_offset = wired
    set_offset(10)

    fires = _fires(scheduler._sync_trigger(60))

    assert [f.minute for f in fires] == [10, 10, 10, 10]


def test_a_shorter_interval_repeats_from_the_same_offset(wired) -> None:
    """15 minutes with an offset of 10 gives 10, 25, 40 and 55 past."""
    scheduler, set_offset = wired
    set_offset(10)

    fires = _fires(scheduler._sync_trigger(15))

    assert {f.minute for f in fires} <= {10, 25, 40, 55}


def test_the_schedule_is_anchored_to_midnight_not_to_start_up(wired) -> None:
    """
    The property that makes a restart harmless.

    If the anchor were "now", the grid would be rebuilt on every restart and
    the first fire would be one interval after start-up -- which is exactly how
    the timetable drifted, and how a restart came to look like it triggered a
    run of its own.
    """
    scheduler, set_offset = wired
    set_offset(25)

    trigger = scheduler._sync_trigger(60)

    assert trigger.start_date.hour == 0
    assert trigger.start_date.minute == 25
    assert trigger.start_date.second == 0


def test_an_offset_outside_the_hour_cannot_break_the_schedule(wired) -> None:
    """
    Clamped rather than trusted.

    The setting is bounded at 0-59 on the way in, but this reads a stored value
    that a future migration or a hand-edited row could put out of range, and a
    trigger built from a bad anchor fails at start-up -- the worst possible
    moment and the hardest place to see.
    """
    scheduler, set_offset = wired

    set_offset(999)
    assert scheduler._sync_trigger(60).start_date.minute == 59

    set_offset(-5)
    assert scheduler._sync_trigger(60).start_date.minute == 0


class TestStartingTheSchedulerDoesNotFireARun:
    """
    Goes through the real start(), because the trigger being right was not enough.

    The anchored trigger was correct and had a passing test, and the schedule
    still moved on every restart -- because add_job passed
    next_run_time=datetime.now() and overrode it. Worse, once APScheduler has a
    previous fire time it computes the next as previous + interval and stops
    consulting the anchor, so one forced first fire re-based the whole
    timetable and the setting did nothing at all.

    A test of the trigger alone could never have caught that. This one asserts
    the property the operator actually cares about: restarting does not run
    anything, and the next run is on the grid.
    """

    @pytest.fixture()
    def started(self, monkeypatch):
        from app import scheduler
        from app.config import settings

        monkeypatch.setattr(scheduler, "session_scope", _no_session)
        monkeypatch.setattr(scheduler, "_timezone", lambda: ZoneInfo("America/Los_Angeles"))
        monkeypatch.setattr(settings, "enable_scheduler", True, raising=False)

        values = {"sync_interval_minutes": 60, "sync_offset_minutes": 30, "catalog_refresh_hour": 3}
        monkeypatch.setattr(
            scheduler.settings_store, "get", lambda _s, key: values.get(key)
        )
        monkeypatch.setattr(scheduler, "_scheduler", None, raising=False)

        # Watch for the job being invoked. Patched before start(), because
        # add_job stores the function by reference.
        fired: list[float] = []
        monkeypatch.setattr(scheduler, "job_sync", lambda: fired.append(1.0))
        scheduler._fired = fired   # type: ignore[attr-defined]

        started = scheduler.start()
        assert started is not None, "the scheduler did not start"
        try:
            yield scheduler
        finally:
            started.shutdown(wait=False)
            monkeypatch.setattr(scheduler, "_scheduler", None, raising=False)

    def test_starting_up_does_not_actually_run_anything(self, started) -> None:
        """
        Asserted by watching for the job to be CALLED, not by reading a clock.

        The obvious version of this test -- "the next run time is more than a
        minute away" -- passes against the broken code, because by the time it
        looks the forced run has already fired and the next one is an hour out.
        The only reliable evidence is whether the work happened.
        """
        import time

        time.sleep(1.0)   # long enough for a forced immediate fire to land

        assert started._fired == [], (
            f"starting the scheduler ran the sync {len(started._fired)} time(s)"
        )

    def test_the_first_run_lands_on_the_configured_minute(self, started) -> None:
        """And it is on the grid, not merely delayed by some arbitrary amount."""
        job = started._scheduler.get_job(started.JOB_SYNC)

        assert job.next_run_time.minute == 30


class TestChangingTheIntervalKeepsTheAnchor:
    """
    The dashboard lets the client change the interval, and that path rebuilds
    the trigger. If it rebuilt an unanchored one, the timetable would start
    drifting again from the next settings change -- silently, and long after
    anyone was still watching for it.

    This path had no test at all, which is how the forced-first-fire bug lived
    next to a passing test of the trigger.
    """

    def test_a_new_interval_still_lands_on_the_configured_minute(
        self, monkeypatch
    ) -> None:
        from app import scheduler
        from app.config import settings

        monkeypatch.setattr(scheduler, "session_scope", _no_session)
        monkeypatch.setattr(scheduler, "_timezone", lambda: ZoneInfo("America/Los_Angeles"))
        monkeypatch.setattr(settings, "enable_scheduler", True, raising=False)
        monkeypatch.setattr(scheduler, "job_sync", lambda: None)
        monkeypatch.setattr(scheduler, "_scheduler", None, raising=False)

        values = {"sync_interval_minutes": 60, "sync_offset_minutes": 20, "catalog_refresh_hour": 3}
        monkeypatch.setattr(scheduler.settings_store, "get", lambda _s, key: values.get(key))

        started = scheduler.start()
        assert started is not None
        try:
            assert started.get_job(scheduler.JOB_SYNC).next_run_time.minute == 20

            # The client changes the interval on the dashboard.
            values["sync_interval_minutes"] = 30
            scheduler._reschedule_sync_if_needed()

            job = started.get_job(scheduler.JOB_SYNC)
            assert int(job.trigger.interval.total_seconds() // 60) == 30, (
                "the new interval was not applied"
            )
            assert job.next_run_time.minute in (20, 50), (
                f"the anchor was lost on reschedule: {job.next_run_time}"
            )
        finally:
            started.shutdown(wait=False)
            monkeypatch.setattr(scheduler, "_scheduler", None, raising=False)


class TestChangingOnlyTheMinuteTakesEffect:
    """
    Changing "minutes past the hour" alone must move the schedule.

    The rescheduler originally compared only the interval, so changing just
    the offset did nothing until the next restart -- the running trigger kept
    the grid it was built with. From the operator's side that is
    indistinguishable from the value being hard-coded, and that is exactly
    what it was reported as.
    """

    def test_a_new_offset_moves_the_schedule_without_a_restart(
        self, monkeypatch
    ) -> None:
        from app import scheduler
        from app.config import settings

        monkeypatch.setattr(scheduler, "session_scope", _no_session)
        monkeypatch.setattr(scheduler, "_timezone", lambda: ZoneInfo("America/Los_Angeles"))
        monkeypatch.setattr(settings, "enable_scheduler", True, raising=False)
        monkeypatch.setattr(scheduler, "job_sync", lambda: None)
        monkeypatch.setattr(scheduler, "_scheduler", None, raising=False)

        values = {"sync_interval_minutes": 60, "sync_offset_minutes": 30, "catalog_refresh_hour": 3}
        monkeypatch.setattr(scheduler.settings_store, "get", lambda _s, key: values.get(key))

        started = scheduler.start()
        assert started is not None
        try:
            assert started.get_job(scheduler.JOB_SYNC).next_run_time.minute == 30

            # The operator changes only the minute, leaving the interval alone.
            values["sync_offset_minutes"] = 25
            scheduler._reschedule_sync_if_needed()

            assert started.get_job(scheduler.JOB_SYNC).next_run_time.minute == 25, (
                "the offset change was ignored, as it was when the rescheduler "
                "compared only the interval"
            )
        finally:
            started.shutdown(wait=False)
            monkeypatch.setattr(scheduler, "_scheduler", None, raising=False)

    def test_an_unchanged_schedule_is_not_rescheduled(self, monkeypatch) -> None:
        """
        Nothing changed means nothing moves.

        Rescheduling unnecessarily would recompute the next fire time after
        every single run, which on a 60-minute interval could quietly push the
        next check up to an hour later each time.
        """
        from app import scheduler
        from app.config import settings

        monkeypatch.setattr(scheduler, "session_scope", _no_session)
        monkeypatch.setattr(scheduler, "_timezone", lambda: ZoneInfo("America/Los_Angeles"))
        monkeypatch.setattr(settings, "enable_scheduler", True, raising=False)
        monkeypatch.setattr(scheduler, "job_sync", lambda: None)
        monkeypatch.setattr(scheduler, "_scheduler", None, raising=False)

        values = {"sync_interval_minutes": 60, "sync_offset_minutes": 15, "catalog_refresh_hour": 3}
        monkeypatch.setattr(scheduler.settings_store, "get", lambda _s, key: values.get(key))

        started = scheduler.start()
        assert started is not None
        try:
            before = started.get_job(scheduler.JOB_SYNC).next_run_time

            scheduler._reschedule_sync_if_needed()

            assert started.get_job(scheduler.JOB_SYNC).next_run_time == before
        finally:
            started.shutdown(wait=False)
            monkeypatch.setattr(scheduler, "_scheduler", None, raising=False)
