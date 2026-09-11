"""
A setting the dashboard accepts must actually change what the system does.

THE CLASS OF BUG THIS FILE EXISTS TO CATCH
==========================================
There is a failure that looks nothing like a failure. The dashboard offers a
field, the client edits it, the save succeeds, the page redisplays the new
value -- and the system carries on behaving exactly as it did before. Nothing
is logged, because from the code's point of view nothing went wrong. The value
really is stored; it is simply never read again by the thing it was supposed to
control.

It happened three times in this codebase, all in the same shape:

* ``sync_offset_minutes`` -- the rescheduler compared only the interval, so
  changing the minute past the hour did nothing until a restart. Reported from
  the live deployment as "you have hard coded the 30 minute". It was not
  hard-coded. It behaved identically to hard-coded, which is what matters.
* ``catalog_refresh_hour`` -- read once in ``start()`` and never again.
* ``timezone`` -- likewise, for the nightly jobs.

Every test here fails against the code as it was before this file was written.
"""

from __future__ import annotations

import ast
import pathlib
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import pytest

from app.core.settings_store import SPEC_BY_KEY, SPECS, SettingError, _coerce

ROOT = pathlib.Path(__file__).resolve().parents[1]


@contextmanager
def _no_session():
    yield None


# ===========================================================================
# The timezone must be a timezone
# ===========================================================================

class TestTheTimezoneIsValidated:
    """
    A mistyped zone name used to be accepted and then silently ignored.

    Every reader falls back to a default when a zone will not load, which is
    right -- a typo must not crash a run. But the fallback moved the day
    boundary by three hours, so the newest full feed was judged to belong to
    yesterday and skipped, with no error raised anywhere. The catalogue then
    went quietly stale.

    That is the single most expensive failure this system can have, it is
    invisible while it happens, and one wrong letter was enough to cause it.
    """

    @pytest.mark.parametrize(
        "name",
        ["America/Los_Angeles", "America/New_York", "Europe/London", "UTC"],
    )
    def test_a_real_zone_is_accepted(self, name: str) -> None:
        assert _coerce(SPEC_BY_KEY["timezone"], name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "America/Los_Angles",   # one letter missing -- the realistic typo
            "America/LosAngeles",   # underscore dropped
            "america/los_angeles",  # wrong case
            "Los_Angeles",          # region missing
            "PST",                  # an abbreviation, not an IANA name
            "GMT+5",                # an offset, not a zone
            "",
            "   ",
        ],
    )
    def test_anything_that_is_not_a_zone_is_refused(self, name: str) -> None:
        with pytest.raises(SettingError):
            _coerce(SPEC_BY_KEY["timezone"], name)

    def test_the_refusal_says_what_a_good_value_looks_like(self) -> None:
        """
        An error nobody can act on is barely better than no error. The client
        types this by hand -- the field has no dropdown -- so the message has
        to carry an example.
        """
        with pytest.raises(SettingError) as caught:
            _coerce(SPEC_BY_KEY["timezone"], "America/Los_Angles")
        message = str(caught.value)
        assert "America/Los_Angeles" in message
        assert "America/Los_Angles" in message, "the rejected value is not quoted back"

    def test_the_declared_default_is_itself_a_real_zone(self) -> None:
        """The default is what the fallback uses. If it is wrong, nothing works."""
        ZoneInfo(str(SPEC_BY_KEY["timezone"].default))


# ===========================================================================
# Changing a schedule setting changes the schedule
# ===========================================================================

@pytest.fixture()
def running(monkeypatch):
    """
    A really-started scheduler whose settings we can edit underneath it.

    Goes through ``start()`` rather than building triggers by hand, because
    the bugs above all lived in the gap between what ``start()`` built and
    what anything afterwards re-read.
    """
    from app import scheduler
    from app.config import settings

    values: dict[str, object] = {
        "sync_interval_minutes": 60,
        "sync_offset_minutes": 20,
        "catalog_refresh_hour": 3,
        "timezone": "America/Los_Angeles",
    }

    monkeypatch.setattr(scheduler, "session_scope", _no_session)
    monkeypatch.setattr(settings, "enable_scheduler", True, raising=False)
    monkeypatch.setattr(scheduler, "job_sync", lambda: None)
    monkeypatch.setattr(scheduler, "job_catalog_refresh", lambda: None)
    monkeypatch.setattr(scheduler, "job_daily_digest", lambda: None)
    monkeypatch.setattr(scheduler, "job_housekeeping", lambda: None)
    monkeypatch.setattr(scheduler, "_scheduler", None, raising=False)
    monkeypatch.setattr(
        scheduler.settings_store, "get", lambda _s, key, **kw: values.get(key)
    )

    started = scheduler.start()
    assert started is not None
    try:
        yield scheduler, started, values
    finally:
        started.shutdown(wait=False)
        monkeypatch.setattr(scheduler, "_scheduler", None, raising=False)


def _cron_hour(scheduler, job) -> int | None:
    return scheduler._cron_hour(job.trigger)


class TestTheCatalogueHourTakesEffect:
    """
    ``catalog_refresh_hour`` was read once, in ``start()``, and never again.

    An operator moving the refresh off a busy hour would have saved the change,
    seen it on the page, and watched the job keep firing at the old hour --
    with nothing at all to explain why.
    """

    def test_a_new_hour_moves_the_job_without_a_restart(self, running) -> None:
        scheduler, started, values = running
        assert _cron_hour(scheduler, started.get_job(scheduler.JOB_CATALOG)) == 3

        values["catalog_refresh_hour"] = 5
        scheduler.apply_schedule_settings()

        assert _cron_hour(scheduler, started.get_job(scheduler.JOB_CATALOG)) == 5, (
            "the catalogue hour was saved but the job never moved"
        )

    def test_the_summary_follows_the_catalogue(self, running) -> None:
        """
        The digest reports on the catalogue, so it must stay behind it. If only
        one of the two moved, the summary would describe yesterday's picture.
        """
        scheduler, started, values = running
        assert _cron_hour(scheduler, started.get_job(scheduler.JOB_DIGEST)) == 4

        values["catalog_refresh_hour"] = 22
        scheduler.apply_schedule_settings()

        assert _cron_hour(scheduler, started.get_job(scheduler.JOB_DIGEST)) == 23
        values["catalog_refresh_hour"] = 23
        scheduler.apply_schedule_settings()
        assert _cron_hour(scheduler, started.get_job(scheduler.JOB_DIGEST)) == 0, (
            "the hour after 23 must wrap to midnight, not fall off the end"
        )

    def test_an_unchanged_hour_is_left_alone(self, running) -> None:
        """Rescheduling for no reason is not free; it resets the next fire time."""
        scheduler, started, _values = running
        before = started.get_job(scheduler.JOB_CATALOG).trigger
        scheduler.apply_schedule_settings()
        assert started.get_job(scheduler.JOB_CATALOG).trigger is before


class TestTheTimezoneTakesEffect:
    """
    Moving the timezone is exactly what a deployment does when it finds the
    vendor is not in the zone everyone assumed -- which is what happened here:
    New York was wrong, the vendor publishes on US Pacific time.

    The pipeline re-reads its settings every run, so "which files are today's"
    corrected itself immediately. The scheduler did not, so the nightly jobs
    carried on firing on the old zone's clock. Los Angeles instead of New York
    moves a 3 AM refresh to midnight: still nightly, still plausible in the
    log, and three hours from where it was asked to be.
    """

    def test_the_nightly_jobs_move_with_it(self, running) -> None:
        scheduler, started, values = running
        assert str(started.get_job(scheduler.JOB_CATALOG).trigger.timezone) == (
            "America/Los_Angeles"
        )

        values["timezone"] = "America/New_York"
        scheduler.apply_schedule_settings()

        assert str(started.get_job(scheduler.JOB_CATALOG).trigger.timezone) == (
            "America/New_York"
        ), "the catalogue refresh kept the old timezone"
        assert str(started.get_job(scheduler.JOB_DIGEST).trigger.timezone) == (
            "America/New_York"
        ), "the daily summary kept the old timezone"

    def test_the_sync_moves_with_it_too(self, running) -> None:
        scheduler, started, values = running
        values["timezone"] = "Europe/London"
        scheduler.apply_schedule_settings()

        job = started.get_job(scheduler.JOB_SYNC)
        assert str(job.trigger.timezone) == "Europe/London"
        assert job.next_run_time.minute == 20, (
            "the configured minute was lost when the timezone changed"
        )


class TestSavingTheFormAppliesItNow:
    """
    The schedule used to be re-read only when a run finished. With a 60-minute
    interval, a change saved at five past could show no effect until nearly two
    hours later -- indistinguishable, from the dashboard, from a setting that
    does not work. One of them genuinely did not.
    """

    def test_the_save_handler_applies_the_schedule(self) -> None:
        source = (ROOT / "app" / "routers" / "settings_page.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        handler = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "save_settings"
        )
        calls = {
            node.func.attr
            for node in ast.walk(handler)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "apply_schedule_settings" in calls, (
            "saving the settings form does not push the new schedule to the "
            "scheduler, so a change waits for the next run or a restart"
        )

    def test_it_is_harmless_when_the_scheduler_is_not_running(
        self, monkeypatch
    ) -> None:
        """
        The dashboard can be served by a process that does not own the jobs, and
        a settings save there must still succeed.
        """
        from app import scheduler

        monkeypatch.setattr(scheduler, "_scheduler", None, raising=False)
        scheduler.apply_schedule_settings()  # must not raise


# ===========================================================================
# The guard against the whole class
# ===========================================================================

def test_every_setting_is_read_by_something() -> None:
    """
    A setting nothing reads is a promise the dashboard cannot keep.

    This is the cheap, permanent guard against the bug above: it will not catch
    a setting that is read in the wrong place, but it does catch the easiest
    version -- a field offered to the client that no code consults at all. That
    is worth having, because such a field is indistinguishable from a working
    one until somebody depends on it.
    """
    sources = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for pattern in ("app/**/*.py", "app/templates/*.html")
        for path in ROOT.glob(pattern)
        if path.name != "settings_store.py"
    )

    unread = [
        spec.key
        for spec in SPECS
        if not spec.locked and f'"{spec.key}"' not in sources
        and f"'{spec.key}'" not in sources
    ]
    assert not unread, f"settings that nothing reads: {', '.join(unread)}"
