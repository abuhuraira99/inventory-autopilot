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
