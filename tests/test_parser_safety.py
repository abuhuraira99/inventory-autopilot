"""
The vendor archive is the least trustworthy input this system has.

Not because the vendor is hostile -- it is their own FTP account -- but because
this file is produced by someone else's system, arrives every five minutes with
nobody watching, and a wrong reading of it becomes a change to a live Amazon
catalogue within seconds.

The specific failures guarded here are the ones that would be *plausible*:

  * a file grabbed while the vendor was still uploading it. Half a feed reads as
    "the vendor has sold out of everything", and acting on that would zero the
    catalogue.
  * something that is not a feed at all -- a nested archive, a database backup
    -- dropped in the folder by mistake, expanding until the VPS disk is full
    and the sync stops.
  * a header that has quietly changed, so stock is read out of the price column.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.vendor.parser import (
    MAX_COMPRESSION_RATIO,
    MAX_UNCOMPRESSED_BYTES,
    FeedFormatError,
    iter_rows,
    verify_archive,
)

FEED = "barcode|artist|title|price|stock|format\n5413356068320|GARNIER,LAURENT|RETROSPECTIVE|12.12|3|CD\n"


def _zip(path: Path, name: str, data: bytes | str) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return path


# ---------------------------------------------------------------------------
# Damaged and truncated archives
# ---------------------------------------------------------------------------


class TestDamagedArchives:
    def test_a_truncated_download_is_refused(self, tmp_path):
        """
        The most dangerous failure in the pipeline. A half-downloaded feed is
        syntactically fine and semantically catastrophic.
        """
        good = _zip(tmp_path / "FULL_FEED_110708_20260903.zip", "feed.txt", FEED * 200)
        raw = good.read_bytes()
        cut = tmp_path / "cut.zip"
        cut.write_bytes(raw[: len(raw) // 2])

        with pytest.raises(FeedFormatError) as exc:
            verify_archive(cut)
        assert "retried" in str(exc.value), "the operator should be told it self-heals"

    def test_a_corrupt_member_fails_the_crc_check(self, tmp_path):
        """
        ``testzip()`` decompresses every member and checks its CRC, so bit rot
        or a partial write is caught before any row is believed.
        """
        path = _zip(tmp_path / "feed.zip", "feed.txt", FEED * 200)
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 0xFF  # flip a byte inside the compressed stream
        path.write_bytes(bytes(raw))

        with pytest.raises(FeedFormatError):
            verify_archive(path)

    def test_no_corruption_anywhere_escapes_as_a_raw_exception(self, tmp_path):
        """
        Damage must ALWAYS arrive as FeedFormatError, wherever the damage lands.

        The test above flips a single byte in the middle. WHICH stdlib failure
        that produces depends on where it lands, so a one-position test passes
        or fails on luck -- it passed on Windows/CPython 3.14 and failed on
        Linux/CPython 3.12 with a raw ``zlib.error``, which was the real bug it
        was supposed to be catching all along.

        The failure this prevents is not a cosmetic one. ``verify_archive`` is
        called inside the pipeline's per-file handler, which catches
        FeedFormatError, quarantines that one file and carries on with the
        others. Anything else escapes that handler and aborts the WHOLE run as
        "failed unexpectedly" -- so one damaged delta stops a cycle that should
        merely have skipped it, and the operator is handed a zlib traceback
        instead of "the download was cut short, it will be retried".

        Walking a flip across every byte of a real archive produced three
        distinct escapes from the stdlib before the fix -- ``zlib.error``,
        ``NotImplementedError`` ("zip file version 23.5") and ``OSError``
        ([Errno 22]). Enumerating them is the losing move, and a different
        Python or zlib build can invent more. The property is that none of them
        ever reach the caller.
        """
        source = _zip(tmp_path / "feed.zip", "feed.txt", FEED * 200)
        base = source.read_bytes()
        target = tmp_path / "corrupt.zip"

        escaped: list[tuple[int, str, str]] = []
        for position in range(len(base)):
            raw = bytearray(base)
            raw[position] ^= 0xFF
            target.write_bytes(bytes(raw))
            try:
                verify_archive(target)
            except FeedFormatError:
                pass  # the operator gets a readable message, the run continues
            except Exception as exc:
                escaped.append((position, type(exc).__name__, str(exc)))

        assert not escaped, (
            f"{len(escaped)} of {len(base)} corrupted archives escaped "
            "verify_archive as something other than FeedFormatError, e.g. byte "
            f"{escaped[0][0]} -> {escaped[0][1]}: {escaped[0][2]}"
        )

    def test_an_empty_file_is_refused(self, tmp_path):
        path = tmp_path / "empty.zip"
        path.write_bytes(b"")
        with pytest.raises(FeedFormatError, match="empty"):
            verify_archive(path)

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(FeedFormatError, match="does not exist"):
            verify_archive(tmp_path / "nope.zip")

    def test_an_archive_with_several_members_is_refused(self, tmp_path):
        """
        Every archive ever observed holds exactly one file. More than one means
        the vendor changed something, and guessing which member is the feed is
        precisely the guess that must not be made automatically.
        """
        path = tmp_path / "two.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("feed.txt", FEED)
            z.writestr("also.txt", FEED)

        with pytest.raises(FeedFormatError, match="expected exactly 1"):
            verify_archive(path)


# ---------------------------------------------------------------------------
# Expansion bounds
# ---------------------------------------------------------------------------


class TestExpansionBounds:
    def test_an_archive_that_expands_absurdly_is_refused(self, tmp_path):
        """
        A megabyte of zeroes compresses about 1000:1, well past the limit. The
        real feed compresses about 2.5:1.

        The point is not that the vendor would do this. It is that reading a
        file that expands without bound will fill the disk, and a full disk
        stops the sync -- which is a silent outage.
        """
        path = _zip(tmp_path / "bomb.zip", "feed.txt", b"\0" * (8 * 1024 * 1024))

        with pytest.raises(FeedFormatError, match="expands"):
            verify_archive(path)

    def test_the_check_happens_before_anything_is_decompressed(self, tmp_path):
        """
        Order matters. ``testzip()`` expands every byte to check the CRC, so if
        it ran first the size limit would be enforced only after doing the exact
        work it exists to prevent. The declared sizes come from the archive's
        central directory and cost nothing to read.
        """
        path = _zip(tmp_path / "bomb.zip", "feed.txt", b"\0" * (8 * 1024 * 1024))
        with pytest.raises(FeedFormatError) as exc:
            verify_archive(path)

        message = str(exc.value)
        assert "Nothing has been read" in message
        assert "CRC" not in message, "the CRC check ran before the bound was applied"

    def test_a_real_sized_feed_is_comfortably_inside_the_limits(self, tmp_path):
        """
        A limit that trips on real data is worse than none, because it stops the
        sync. The genuine full feed is ~75 MB compressed, ~190 MB expanded.
        """
        path = _zip(tmp_path / "feed.zip", "feed.txt", FEED * 5000)
        member, size = verify_archive(path)

        assert member == "feed.txt"
        assert size < MAX_UNCOMPRESSED_BYTES
        assert size / path.stat().st_size < MAX_COMPRESSION_RATIO


# ---------------------------------------------------------------------------
# Reading it once
# ---------------------------------------------------------------------------


class TestSinglePass:
    def test_a_verified_archive_is_not_verified_again(self, tmp_path, monkeypatch):
        """
        The pipeline verifies each archive when it downloads it. Without passing
        the member name on, ``iter_rows`` would call ``verify_archive`` again --
        and ``testzip()`` expands all 75 MB a second time, every run.
        """
        path = _zip(tmp_path / "feed.zip", "feed.txt", FEED)
        member, _ = verify_archive(path)

        calls: list[Path] = []
        import app.vendor.parser as parser

        real = parser.verify_archive
        monkeypatch.setattr(
            parser, "verify_archive", lambda p: (calls.append(p), real(p))[1]
        )

        rows = [r for r, _rej, _s in iter_rows(path, member_name=member) if r]

        assert calls == [], "the archive was expanded a second time"
        assert len(rows) == 1

    def test_omitting_the_member_name_still_verifies(self, tmp_path):
        """
        The safe default. A caller that has not verified anything -- a script,
        say -- must not be able to skip the check by accident, which is why the
        parameter is a member name rather than a boolean flag.
        """
        path = _zip(tmp_path / "feed.zip", "feed.txt", FEED)
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        path.write_bytes(bytes(raw))

        with pytest.raises(FeedFormatError):
            list(iter_rows(path))


# ---------------------------------------------------------------------------
# The header
# ---------------------------------------------------------------------------


class TestHeaderGate:
    def test_a_changed_header_stops_the_parse(self, tmp_path):
        """
        Stock and price are adjacent columns. If the vendor swaps them and we
        parse on position, every quantity becomes a price -- and prices are the
        one thing this system is forbidden to touch.
        """
        swapped = "barcode|artist|title|stock|price|format\n5413356068320|A|B|3|12.12|CD\n"
        path = _zip(tmp_path / "feed.zip", "feed.txt", swapped)

        with pytest.raises(FeedFormatError) as exc:
            list(iter_rows(path))

        message = str(exc.value)
        assert "has NOT been processed" in message
        assert "do not" in message.lower(), "the message should say what not to assume"


# ===========================================================================
# Bytes the database will not accept
# ===========================================================================
# The vendor's 1.15-million-row full feed contained a NUL byte. PostgreSQL text
# columns cannot hold one -- not escaped, not truncated; the whole INSERT is
# refused with "PostgreSQL text fields cannot contain NUL (0x00) bytes". So one
# byte, somewhere in a million rows, destroyed the entire daily catalogue load.
#
# It survived weeks of real traffic because the five-minute delta files are a
# few hundred rows each and never happened to contain one. It appeared the very
# first time a full feed was successfully downloaded, which is exactly the file
# the whole system depends on.


class TestControlCharactersNeverReachTheDatabase:
    def test_a_nul_byte_is_removed_from_a_title(self) -> None:
        """The exact byte, in the exact field, that stopped the live server."""
        from app.vendor.parser import _clean

        cleaned = _clean("THIEVES\x00 & LIARS")

        assert "\x00" not in cleaned
        assert cleaned == "THIEVES & LIARS"

    def test_the_other_control_characters_go_too(self) -> None:
        """
        Only NUL is rejected by PostgreSQL; the rest are still junk.

        They are meaningless inside an artist or a title and they corrupt any
        CSV report built from them later. Keeping a byte the vendor plainly did
        not intend to send buys nothing and costs another failure mode.
        """
        from app.vendor.parser import _clean

        assert _clean("OK\x01\x1f\x7fTITLE") == "OKTITLE"

    def test_ordinary_whitespace_is_still_collapsed_not_deleted(self) -> None:
        """
        Tabs and newlines must stay word separators.

        Deleting them instead would silently glue words together -- "ROCK N
        ROLL" becoming "ROCKNROLL" would change how a title reads and how it
        matches, which is a worse bug than the one being fixed.
        """
        from app.vendor.parser import _clean

        assert _clean("AMERICAN\tROCK\nN  ROLL") == "AMERICAN ROCK N ROLL"

    def test_a_row_carrying_a_nul_byte_still_parses(self, tmp_path) -> None:
        """
        End to end through the real parser, not just the helper.

        A test of _clean alone would keep passing if a future change stopped
        routing a field through it.
        """
        import zipfile

        from app.vendor.parser import parse_all

        archive = tmp_path / "FULL_FEED_110721_20260909.zip"
        body = (
            "barcode|artist|title|price|stock|format\n"
            "5413356068320|GARNIER,\x00LAURENT|RETRO\x00SPECTIVE|12.12|3|CD\n"
        )
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("feed.csv", body)

        rows, _rejects, _stats = parse_all(archive)

        assert len(rows) == 1
        assert "\x00" not in rows[0].artist
        assert "\x00" not in rows[0].title
        assert rows[0].artist == "GARNIER,LAURENT"
