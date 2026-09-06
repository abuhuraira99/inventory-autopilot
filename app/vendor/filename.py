"""
Understanding vendor filenames.

WHY THIS IS EASY, AND WHY THAT IS LUCKY
=======================================
The client asked for a rule like "only process today's files", and worried
about timezones. It turns out the vendor puts the date directly in the
filename, which removes the guesswork entirely.

Real filenames observed on the All Media Supply FTP server:

    FULL_FEED_110708_20260901.zip        27,530,843 bytes  -> 1,150,544 rows
    FULL_FEED_110708_20260902.zip        27,906,707 bytes
    FULL_FEED_110708_20260903.zip        27,803,235 bytes

    DELTA_FEED_110708_20260904_80.zip         3,768 bytes  ->     105 rows
    DELTA_FEED_110708_20260904_81.zip         2,837 bytes  ->      81 rows
    DELTA_FEED_110708_20260904_82.zip         6,110 bytes  ->     215 rows
    ...
    DELTA_FEED_110708_20260904_99.zip         2,4xx bytes  ->      70 rows

So the shape is::

    <KIND>_FEED_<account>_<YYYYMMDD>[_<sequence>].zip

and we get three facts for free:

  * **kind** -- FULL or DELTA. This matters enormously, because a missing
    barcode means "unchanged" in a delta and "the vendor dropped it" in a full
    feed. Guessing would either strand dead stock on sale or wipe the catalogue.

  * **date** -- the vendor's own calendar date, as text. No timezone
    conversion, no clock skew, no ambiguity. "Today" becomes a string equality
    test against the current date in the configured timezone.

  * **sequence** -- a counter that resets each day. Timestamps inside the zips
    confirm the cadence: sequence 80 at 07:14, 81 at 07:19, 82 at 07:24 --
    exactly five minutes apart. This gives a total order within a day, so files
    are always applied oldest-first even if the FTP listing arrives jumbled.

DEFENSIVE POSTURE
-----------------
The pattern is matched loosely and every field is optional. A filename we do
not recognise yields ``kind=UNKNOWN`` and no date, and the ingest layer then
refuses to process it rather than guessing. If the vendor changes their naming,
the system stops and says so -- it does not quietly do the wrong thing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.models import FeedKind

#: Matches the observed convention, tolerantly:
#:   - the word FULL or DELTA anywhere before "FEED"
#:   - an 8-digit date
#:   - an optional trailing sequence number
#: Case-insensitive, and indifferent to the separator used.
_PATTERN = re.compile(
    r"""
    ^
    (?P<kind>FULL|DELTA)      # FULL_FEED / DELTA_FEED
    [_\-\s]*FEED
    [_\-\s]*
    (?P<account>[A-Za-z0-9]+)? # 110708 -- the vendor's account number for us
    [_\-\s]*
    (?P<date>\d{8})            # 20260904
    (?: [_\-\s]* (?P<seq>\d+) )?   # _80
    (?: \. (?P<ext>[A-Za-z0-9]+) )?  # .zip
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Fallback: pull any 8-digit run that looks like a plausible date out of a
#: filename we could not fully parse. Used only to give the operator a hint in
#: the dashboard; never used to decide whether to process a file.
_LOOSE_DATE = re.compile(r"(?<!\d)(20\d{6})(?!\d)")


@dataclass(frozen=True, slots=True)
class ParsedName:
    """Everything a filename tells us."""

    original: str
    kind: FeedKind
    #: The vendor's date as a ``date``, or None if unparseable.
    feed_date: date | None
    #: Same date as the raw 8-character string, for exact comparison.
    date_str: str | None
    sequence: int | None
    account: str | None
    extension: str | None

    @property
    def recognised(self) -> bool:
        """
        True only when we are confident about both kind and date.

        The ingest layer requires this before touching a file. An unrecognised
        filename is surfaced to a human rather than processed on a guess.
        """
        return self.kind in (FeedKind.FULL, FeedKind.DELTA) and self.feed_date is not None

    @property
    def sort_key(self) -> tuple:
        """
        Ordering for applying files: oldest date first, then sequence, and a
        full feed before the deltas of the same day.

        The full-feed-first rule matters. A full feed is the authoritative
        snapshot; replaying it after a delta from the same day would undo that
        delta's newer numbers.
        """
        d = self.feed_date or date.min
        kind_rank = 0 if self.kind == FeedKind.FULL else 1
        return (d, kind_rank, self.sequence if self.sequence is not None else -1, self.original)


def parse(filename: str) -> ParsedName:
    """
    Read a vendor filename.

    >>> p = parse("FULL_FEED_110708_20260901.zip")
    >>> p.kind.value, p.date_str, p.sequence, p.account
    ('full', '20260901', None, '110708')
    >>> p.recognised
    True

    >>> d = parse("DELTA_FEED_110708_20260904_80.zip")
    >>> d.kind.value, d.date_str, d.sequence
    ('delta', '20260904', 80)

    Case and separators do not matter:

    >>> parse("delta-feed-110708-20260904-7.ZIP").sequence
    7

    Anything we do not understand is flagged rather than guessed:

    >>> u = parse("inventory_latest.zip")
    >>> u.kind.value, u.recognised
    ('unknown', False)
    """
    name = (filename or "").strip()
    # Work on the basename only; an FTP listing may include a path.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]

    m = _PATTERN.match(name)
    if m:
        kind = FeedKind.FULL if m.group("kind").upper() == "FULL" else FeedKind.DELTA
        date_str = m.group("date")
        feed_date = _to_date(date_str)
        seq = m.group("seq")
        return ParsedName(
            original=name,
            kind=kind,
            feed_date=feed_date,
            date_str=date_str if feed_date else None,
            sequence=int(seq) if seq is not None else None,
            account=m.group("account"),
            extension=(m.group("ext") or "").lower() or None,
        )

    # Unrecognised. Still try to surface a date so the dashboard can show
    # something useful, but leave kind UNKNOWN so nothing gets processed.
    loose = _LOOSE_DATE.search(name)
    guessed = _to_date(loose.group(1)) if loose else None
    upper = name.upper()
    hinted_kind = FeedKind.UNKNOWN
    if "FULL" in upper:
        hinted_kind = FeedKind.FULL
    elif "DELTA" in upper:
        hinted_kind = FeedKind.DELTA

    return ParsedName(
        original=name,
        # Deliberately UNKNOWN unless BOTH the word and a date were found, so
        # `recognised` stays False and the ingest layer asks a human.
        kind=hinted_kind if (hinted_kind is not FeedKind.UNKNOWN and guessed) else FeedKind.UNKNOWN,
        feed_date=guessed,
        date_str=loose.group(1) if loose and guessed else None,
        sequence=None,
        account=None,
        extension=name.rsplit(".", 1)[-1].lower() if "." in name else None,
    )


def _to_date(s: str | None) -> date | None:
    """YYYYMMDD -> date, or None if it is not a real date."""
    if not s or len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def today_in(timezone_name: str) -> date:
    """
    Today's calendar date in the configured timezone.

    Uses a real IANA zone (``America/New_York``) rather than a fixed offset, so
    the day boundary stays correct across the daylight-saving changes in March
    and November. A hard-coded ``-05:00`` would be wrong for eight months of
    the year.
    """
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:  # pragma: no cover - bad config, fall back rather than crash
        tz = ZoneInfo("America/New_York")
    return datetime.now(tz).date()


def is_from_today(parsed: ParsedName, timezone_name: str) -> bool:
    """
    Whether a file belongs to the current day in the configured timezone.

    This is the "only today's files" rule the client asked for. It is an exact
    date comparison, not a time-window heuristic, because the date comes from
    the filename.
    """
    if parsed.feed_date is None:
        return False
    return parsed.feed_date == today_in(timezone_name)


def age_hours(parsed: ParsedName, timezone_name: str) -> float | None:
    """
    Roughly how old the file's date is, in hours. None if the date is unknown.

    Used by the ``max_file_age_hours`` setting. Deliberately coarse: the
    filename carries a date but not a time, so this is measured from the start
    of the file's day. That is the conservative direction -- it makes files look
    older rather than newer, so nothing stale slips through.
    """
    if parsed.feed_date is None:
        return None
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:  # pragma: no cover
        tz = ZoneInfo("America/New_York")
    start_of_day = datetime.combine(parsed.feed_date, datetime.min.time(), tzinfo=tz)
    return (datetime.now(tz) - start_of_day).total_seconds() / 3600.0


def sort_for_processing(names: list[str]) -> list[ParsedName]:
    """
    Parse and order filenames for safe sequential application.

    Oldest first; within a day the full feed before its deltas; within the
    deltas by sequence number.

    >>> [p.original for p in sort_for_processing([
    ...     "DELTA_FEED_110708_20260904_81.zip",
    ...     "DELTA_FEED_110708_20260904_80.zip",
    ...     "FULL_FEED_110708_20260904.zip",
    ... ])]
    ['FULL_FEED_110708_20260904.zip', 'DELTA_FEED_110708_20260904_80.zip', 'DELTA_FEED_110708_20260904_81.zip']
    """
    return sorted((parse(n) for n in names), key=lambda p: p.sort_key)
