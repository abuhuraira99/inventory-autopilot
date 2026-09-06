"""
Reading a vendor feed file.

THE FILE, AS IT REALLY IS
=========================
Verified against FULL_FEED_110708_20260901.zip on 2026-09-04:

  * a ZIP archive containing exactly one ``.txt`` file of the same name
  * 27.5 MB compressed, 74.7 MB uncompressed
  * **1,150,544 data rows** in a full feed; 23-320 rows in a delta
  * pipe-delimited, despite everyone calling them CSVs::

        barcode|artist|title|price|stock|format
        5413356068320|GARNIER,LAURENT|RETROSPECTIVE|12.12|0|CD

  * barcodes arrive with leading zeros stripped (see :mod:`app.core.barcode`)
  * 1,034,815 of the 1,150,544 rows are ``stock=0`` -- about 90% of the
    catalogue is out of stock at any moment
  * the largest stock value observed was 5,662

WHY THIS IS A STREAMING PARSER
==============================
Loading 1.15 million rows into a list of dicts costs well over a gigabyte of
memory, and the target machine is a modest VPS that also runs PostgreSQL. So
nothing here ever holds the whole file: the zip member is read as a stream, and
rows are yielded one at a time. The caller batches them into the database.

The previous generation of this tool used pandas and did the same job in under
ten seconds; a hand-written stdlib reader is comparable for this shape of data
and removes a 60 MB dependency from the production image.

WHY BAD ROWS ARE KEPT
=====================
Roughly 150 rows a day have a barcode of 1-7 characters -- unrecoverable junk.
Silently dropping them would hide a vendor-side problem forever. They are
returned as :class:`RejectedRow` records so the dashboard can show a trend, and
so somebody can eventually ask the vendor about them.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from app.core.barcode import Barcode, normalise

log = logging.getLogger(__name__)

#: The header we expect, in order. Used by the "reject an unknown header"
#: guardrail: if the vendor reorders or renames columns, stock could silently
#: be read out of the price column, which would be catastrophic.
EXPECTED_HEADER = ("barcode", "artist", "title", "price", "stock", "format")

#: csv.field_size_limit defaults to 128 KB. Some titles in this catalogue are
#: very long ("SMART TRAVELS EUROPE WITH RUDY MAXA: CLASSICAL EUROPE - ...")
#: but nowhere near that. Raised anyway so one pathological row cannot abort a
#: 1.15-million-row import.
csv.field_size_limit(4 * 1024 * 1024)


class FeedFormatError(RuntimeError):
    """
    The file cannot be trusted: bad zip, wrong header, no data.

    Raised rather than worked around. A feed we do not understand must stop the
    run and alert a human -- half-processing an unfamiliar file is how a
    catalogue gets zeroed.
    """


@dataclass(slots=True)
class FeedRow:
    """One usable product row."""

    barcode: Barcode
    artist: str
    title: str
    price: float | None
    stock: int
    product_format: str
    line_number: int


@dataclass(slots=True)
class RejectedRow:
    """One row we could not use, with the reason, kept for the report."""

    line_number: int
    raw_line: str
    reason: str


@dataclass(slots=True)
class ParseStats:
    """Counters for the run summary and the sanity gates."""

    total_lines: int = 0
    usable_rows: int = 0
    rejected_rows: int = 0
    zero_stock_rows: int = 0
    in_stock_rows: int = 0
    max_stock_seen: int = 0
    bad_checksum_rows: int = 0
    duplicate_barcodes: int = 0
    observed_header: str = ""
    header_matches_expected: bool = False
    rejections_by_reason: dict[str, int] = field(default_factory=dict)

    def note_rejection(self, reason: str) -> None:
        self.rejected_rows += 1
        self.rejections_by_reason[reason] = self.rejections_by_reason.get(reason, 0) + 1


# ---------------------------------------------------------------------------
# Archive handling
# ---------------------------------------------------------------------------

def sha256_of_file(path: Path, *, chunk: int = 1024 * 1024) -> str:
    """
    Hash a file without reading it all into memory.

    This hash is the anti-double-processing mechanism: it is stored unique on
    ``feed_files.content_sha256``, so identical content is never processed
    twice even if the vendor renames or re-uploads the file.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def verify_archive(path: Path) -> tuple[str, int]:
    """
    Check the archive opens cleanly and return ``(member_name, member_size)``.

    Catches the most dangerous failure mode in the whole pipeline: a file
    downloaded while the vendor was still uploading it. A truncated feed looks
    exactly like "the vendor has sold out of everything", and processing one
    would set the entire catalogue to zero on Amazon.

    ``testzip()`` verifies every member's CRC, so a corrupt or partial download
    is rejected here rather than producing plausible-looking garbage later.
    """
    if not path.exists():
        raise FeedFormatError(f"file does not exist: {path}")
    if path.stat().st_size == 0:
        raise FeedFormatError(f"file is empty: {path.name}")

    try:
        with zipfile.ZipFile(path) as z:
            broken = z.testzip()
            if broken is not None:
                raise FeedFormatError(
                    f"{path.name} is damaged: the CRC check failed on {broken}. "
                    "This usually means the download was cut short, or the vendor "
                    "was still uploading. It will be retried next cycle."
                )
            members = [i for i in z.infolist() if not i.is_dir()]
            if not members:
                raise FeedFormatError(f"{path.name} contains no files")
            if len(members) > 1:
                # Every observed archive holds exactly one member. More than
                # one means the vendor changed something and a human should look.
                names = ", ".join(m.filename for m in members[:5])
                raise FeedFormatError(
                    f"{path.name} contains {len(members)} files ({names}); expected exactly 1"
                )
            return members[0].filename, members[0].file_size
    except zipfile.BadZipFile as exc:
        raise FeedFormatError(
            f"{path.name} is not a valid zip archive ({exc}). Most likely an "
            "incomplete download; it will be retried."
        ) from exc


def peek_header(path: Path, *, delimiter: str = "|") -> str:
    """
    Read only the header line, without decompressing the rest.

    Used by the header guardrail before committing to a full parse of 75 MB.
    """
    with zipfile.ZipFile(path) as z:
        member = next(i for i in z.infolist() if not i.is_dir())
        with z.open(member) as raw:
            first = raw.readline(64 * 1024)
    return first.decode("utf-8", errors="replace").strip().lstrip("﻿")


def header_matches(observed: str, *, delimiter: str = "|") -> bool:
    """
    Whether the observed header is the one we know how to read.

    Compared case-insensitively and whitespace-insensitively, because a vendor
    changing ``stock`` to ``Stock`` is cosmetic, whereas reordering the columns
    is not.

    >>> header_matches("barcode|artist|title|price|stock|format")
    True
    >>> header_matches("BARCODE | ARTIST | TITLE | PRICE | STOCK | FORMAT")
    True
    >>> header_matches("barcode|artist|title|stock|price|format")   # reordered
    False
    >>> header_matches("barcode|title|price|qty")
    False
    """
    cols = tuple(c.strip().lower() for c in observed.strip().lstrip("﻿").split(delimiter))
    return cols == EXPECTED_HEADER


# ---------------------------------------------------------------------------
# Streaming parse
# ---------------------------------------------------------------------------

def iter_rows(
    path: Path,
    *,
    delimiter: str = "|",
    column_map: dict[str, str] | None = None,
    strict_header: bool = True,
    encoding: str = "utf-8",
) -> Iterator[tuple[FeedRow | None, RejectedRow | None, ParseStats]]:
    """
    Stream a feed file, yielding ``(row, rejection, stats)`` one line at a time.

    Exactly one of ``row`` and ``rejection`` is set on each yield. ``stats`` is
    the *same mutable object* every time -- read it after the loop for the
    totals, which avoids a second pass over 1.15 million rows.

    Parameters
    ----------
    delimiter:
        From the ``feed_delimiter`` setting. ``|`` for this vendor.
    column_map:
        From the ``column_map`` setting: ``{our_name: their_name}``. Lets the
        client cope with the vendor renaming ``stock`` to ``qty`` from the
        dashboard, with no code change.
    strict_header:
        From the ``guardrail_require_known_header`` setting. When True an
        unfamiliar header raises rather than being parsed on a guess.

    Notes
    -----
    Rows are read with :class:`csv.reader` rather than split on the delimiter,
    because titles in this catalogue contain commas and quotes
    ("LIFE OF LUCKY CUCUMBER", "MILEAGE (BABY BLUE VINYL)") and a naive split
    would mangle them.
    """
    column_map = column_map or {c: c for c in EXPECTED_HEADER}
    stats = ParseStats()

    member_name, _ = verify_archive(path)

    seen_barcodes: set[str] = set()

    with zipfile.ZipFile(path) as z, z.open(member_name) as raw:
        # newline="" is required by the csv module so it can handle embedded
        # newlines itself rather than being handed pre-split lines.
        text = io.TextIOWrapper(raw, encoding=encoding, errors="replace", newline="")
        reader = csv.reader(text, delimiter=delimiter)

        try:
            header = next(reader)
        except StopIteration:
            raise FeedFormatError(f"{path.name} has no header row") from None

        header = [h.strip().lstrip("﻿") for h in header]
        stats.observed_header = delimiter.join(header)
        stats.header_matches_expected = header_matches(stats.observed_header, delimiter=delimiter)

        if strict_header and not stats.header_matches_expected:
            raise FeedFormatError(
                f"{path.name} has an unexpected header.\n"
                f"  expected: {delimiter.join(EXPECTED_HEADER)}\n"
                f"  found:    {stats.observed_header}\n"
                "The file has NOT been processed. If the vendor has genuinely changed "
                "their format, update 'Which column is which' in Settings; do not "
                "assume the columns still mean what they used to."
            )

        # Resolve our field names to positions in this particular file, so the
        # hot loop below is index arithmetic rather than dict lookups.
        lower = [h.lower() for h in header]

        def index_of(our_name: str) -> int | None:
            their = column_map.get(our_name, our_name).strip().lower()
            return lower.index(their) if their in lower else None

        i_barcode = index_of("barcode")
        i_artist = index_of("artist")
        i_title = index_of("title")
        i_price = index_of("price")
        i_stock = index_of("stock")
        i_format = index_of("format")

        if i_barcode is None or i_stock is None:
            raise FeedFormatError(
                f"{path.name} is missing an essential column. Need a barcode column "
                f"and a stock column; the file has: {stats.observed_header}. "
                "Check 'Which column is which' in Settings."
            )

        width = len(header)

        for line_no, parts in enumerate(reader, start=2):  # header was line 1
            stats.total_lines += 1

            if not parts or (len(parts) == 1 and not parts[0].strip()):
                continue  # blank line, not worth recording

            if len(parts) < width:
                stats.note_rejection("too_few_columns")
                yield None, RejectedRow(line_no, delimiter.join(parts)[:1000], "too_few_columns"), stats
                continue

            bc = normalise(parts[i_barcode])
            if not bc.usable:
                # ~150 of these a day: barcodes of 1-7 characters.
                reason = "no_digits_in_barcode" if not bc.digits else "barcode_wrong_length"
                stats.note_rejection(reason)
                yield None, RejectedRow(line_no, delimiter.join(parts)[:1000], reason), stats
                continue

            stock = _to_int(parts[i_stock])
            if stock is None:
                stats.note_rejection("unreadable_stock")
                yield None, RejectedRow(line_no, delimiter.join(parts)[:1000], "unreadable_stock"), stats
                continue
            if stock < 0:
                # Never seen in practice, but a negative quantity must not be
                # allowed to reach Amazon. Clamp and carry on.
                log.warning("negative stock %d on line %d of %s; treating as 0", stock, line_no, path.name)
                stock = 0

            if bc.canonical in seen_barcodes:
                # Later rows win: within a single file the last mention is the
                # most recent. Counted so a systematic vendor duplication
                # problem is visible.
                stats.duplicate_barcodes += 1
            else:
                seen_barcodes.add(bc.canonical)

            if not bc.checksum_ok:
                stats.bad_checksum_rows += 1

            stats.usable_rows += 1
            if stock > 0:
                stats.in_stock_rows += 1
                stats.max_stock_seen = max(stats.max_stock_seen, stock)
            else:
                stats.zero_stock_rows += 1

            yield (
                FeedRow(
                    barcode=bc,
                    artist=_clean(parts[i_artist]) if i_artist is not None else "",
                    title=_clean(parts[i_title]) if i_title is not None else "",
                    price=_to_float(parts[i_price]) if i_price is not None else None,
                    stock=stock,
                    product_format=_clean(parts[i_format]).upper() if i_format is not None else "",
                    line_number=line_no,
                ),
                None,
                stats,
            )

    if stats.usable_rows == 0:
        raise FeedFormatError(
            f"{path.name} produced no usable rows out of {stats.total_lines} lines. "
            "Treating the file as broken rather than concluding the vendor has "
            "nothing in stock."
        )


def parse_all(
    path: Path,
    *,
    delimiter: str = "|",
    column_map: dict[str, str] | None = None,
    strict_header: bool = True,
) -> tuple[list[FeedRow], list[RejectedRow], ParseStats]:
    """
    Convenience wrapper that materialises everything.

    Safe for delta files (23-320 rows). **Do not use on a full feed** -- 1.15
    million :class:`FeedRow` objects is roughly a gigabyte of memory. Use
    :func:`iter_rows` there.
    """
    rows: list[FeedRow] = []
    rejects: list[RejectedRow] = []
    stats = ParseStats()
    for row, reject, stats in iter_rows(  # noqa: B007 - stats is read after the loop
        path, delimiter=delimiter, column_map=column_map, strict_header=strict_header
    ):
        if row is not None:
            rows.append(row)
        elif reject is not None:
            rejects.append(reject)
    return rows, rejects, stats


# ---------------------------------------------------------------------------
# Field coercion
# ---------------------------------------------------------------------------

def _clean(value: str) -> str:
    """
    Trim and flatten a text field.

    Blank fields become ``""`` rather than ``None``: the previous tool hit
    JSON serialisation failures from NaN values leaking into the browser, and
    an empty string is always safe.
    """
    if value is None:
        return ""
    return " ".join(str(value).split())[:600]


def _to_int(value: str) -> int | None:
    """
    Parse a stock value. ``None`` means unreadable, which rejects the row.

    Accepts ``"5"``, ``"5.0"``, ``" 5 "``, ``"1,234"``. Deliberately does NOT
    treat a blank as zero: a blank stock field is ambiguous, and guessing zero
    would take a product off sale on Amazon for no good reason.
    """
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        try:
            f = float(s)
        except ValueError:
            return None
        return int(f)


def _to_float(value: str) -> float | None:
    """
    Parse a price. Recorded for the reports; never sent to Amazon.

    A blank or unreadable price is ``None`` and does not reject the row --
    prices are outside this system's remit, so a bad one must not stop a stock
    update.
    """
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None
