#!/usr/bin/env python
"""
Stage 0: measure the catalogue before enabling anything.

WHAT THIS ANSWERS
=================
One question, with a real number:

    Of the Amazon listings this system would manage, how many can actually be
    matched to a row in the vendor's feed -- and how far apart are Amazon and
    the vendor right now?

WHY IT MATTERS
==============
It runs with **no Amazon credentials and no network access**, against files the
client can email over in ten minutes. So the client learns something concrete
and useful about their own account before agreeing to let any software write to
it -- and the answer is worth having whether or not the automation ever ships.

It also exercises the real mapping and decision engines, so a good result here
is evidence the pipeline is correct, not just that the analysis is.

USAGE
=====
    python scripts/stage0_coverage.py \
        --listings  "All+Listings+Report_09-04-2026.txt" \
        --feed      "FULL_FEED_110708_20260903.zip" \
        --prefix    "HA-AMS-"

Add ``--json out.json`` to save a machine-readable copy for the dashboard.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Allow running from the repository root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.amazon.reports import parse_local_report  # noqa: E402
from app.core.barcode import build_sku  # noqa: E402
from app.engine.decision import (  # noqa: E402
    DecisionEngine,
    QuantityRules,
    apply_change_limit,
)
from app.engine.guardrails import evaluate_batch  # noqa: E402
from app.engine.mapping import CatalogIndex, ListingEntry, Mapper, measure_coverage  # noqa: E402
from app.vendor.parser import iter_rows  # noqa: E402

BAR = "=" * 78
SUB = "-" * 78


def heading(text: str) -> None:
    print()
    print(BAR)
    print(text)
    print(BAR)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listings", required=True, type=Path, help="All Listings Report (.txt, tab separated)")
    ap.add_argument("--feed", required=True, type=Path, help="A full feed archive (.zip)")
    # default=None, not a string: with action="append" a string default would be
    # kept as a string and then iterated character by character. That produced a
    # silent "0 listings in scope" the first time this script ran.
    ap.add_argument(
        "--prefix",
        action="append",
        default=None,
        help="SKU prefix in scope. Repeat for several. Defaults to HA-AMS-.",
    )
    ap.add_argument("--max-quantity", type=int, default=15)
    ap.add_argument("--safety-buffer", type=int, default=0)
    ap.add_argument("--out-of-stock-at", type=int, default=0)
    ap.add_argument("--change-limit", type=int, default=5000)
    ap.add_argument("--json", type=Path, default=None, help="Write the report as JSON")
    args = ap.parse_args()

    prefixes: list[str] = args.prefix or ["HA-AMS-"]

    # -------------------------------------------------------------- listings
    heading("1. AMAZON'S VIEW  (All Listings Report)")
    if not args.listings.exists():
        print(f"   ERROR: no such file: {args.listings}")
        return 2

    records, rstats = parse_local_report(args.listings)
    print(f"   file            : {args.listings.name}")
    print(f"   columns         : {rstats.observed_columns}")
    print(f"   listings        : {rstats.parsed_rows:,}")
    print(f"   without barcode : {rstats.no_barcode_rows:,}")
    print()
    print("   by status:")
    for status, n in sorted(rstats.by_status.items(), key=lambda kv: -kv[1]):
        print(f"      {status:<14} {n:>8,}")
    print()
    print("   top SKU prefixes (each one is a different supplier):")
    for prefix, n in sorted(rstats.by_prefix.items(), key=lambda kv: -kv[1])[:12]:
        mark = "  <-- IN SCOPE" if prefix in prefixes else ""
        print(f"      {prefix or '(none)':<14} {n:>8,}{mark}")

    # ----------------------------------------------------------------- index
    entries = [
        ListingEntry(
            seller_sku=r.seller_sku,
            sku_prefix=r.sku_prefix,
            barcode=r.barcode,
            quantity=r.quantity,
            status=r.status,
            # This account's report has no fulfillment-channel column. The
            # client confirmed they never use Amazon's shipping service, and
            # their filled template says "Fulfillment by Merchant (Default)",
            # so DEFAULT is correct here. The live pipeline additionally
            # verifies this per SKU through the Listings API before writing.
            fulfillment_channel="DEFAULT",
            lead_time_to_ship_days=None,
            product_type="PRODUCT",
        )
        for r in records
    ]
    index = CatalogIndex(entries, prefixes_in_scope=prefixes)
    print()
    print(f"   in scope        : {index.in_scope_count:,}")
    print(f"   out of scope    : {index.out_of_scope_count:,}   (left completely alone)")

    # ------------------------------------------------------------------ feed
    heading("2. THE VENDOR'S VIEW  (full feed)")
    if not args.feed.exists():
        print(f"   ERROR: no such file: {args.feed}")
        return 2

    vendor: dict[str, tuple[int, str, str]] = {}
    fstats = None
    # noqa below: fstats is deliberately read AFTER the loop - iter_rows yields the
    # same mutable stats object each time, which avoids a second pass over 1.15
    # million rows just to count them.
    for row, _reject, fstats in iter_rows(args.feed):  # noqa: B007
        if row is not None:
            vendor[row.barcode.canonical] = (row.stock, row.product_format, row.title)

    print(f"   file            : {args.feed.name}")
    print(f"   rows read       : {fstats.total_lines:,}")
    print(f"   usable          : {fstats.usable_rows:,}")
    print(f"   unreadable      : {fstats.rejected_rows:,}   {fstats.rejections_by_reason}")
    print(f"   in stock (>0)   : {fstats.in_stock_rows:,}")
    print(f"   out of stock    : {fstats.zero_stock_rows:,}  ({100*fstats.zero_stock_rows/fstats.usable_rows:.1f}%)")
    print(f"   largest stock   : {fstats.max_stock_seen:,}")
    print(f"   bad check digit : {fstats.bad_checksum_rows:,}")

    # -------------------------------------------------------------- coverage
    heading("3. COVERAGE  --  the headline number")
    coverage = measure_coverage(index, set(vendor))
    print(f"   in-scope Amazon listings          : {coverage.in_scope_listings:>8,}")
    print(f"   matched to a vendor row           : {coverage.matched_listings:>8,}")
    print(f"   NOT in the vendor feed            : {coverage.unmatched_listings:>8,}")
    print()
    print(f"   >>> COVERAGE: {coverage.listing_coverage_percent:.1f}% <<<")
    print()
    print("   per prefix:")
    for prefix, counts in sorted(coverage.by_prefix.items(), key=lambda kv: -sum(kv[1].values())):
        tot = counts["matched"] + counts["unmatched"]
        pct = 100.0 * counts["matched"] / tot if tot else 0.0
        print(f"      {prefix:<14} matched {counts['matched']:>7,} / {tot:>7,}  = {pct:5.1f}%")
    if coverage.sample_unmatched:
        print()
        print("   examples with no vendor row (discontinued, or listed elsewhere):")
        for sku in coverage.sample_unmatched[:8]:
            print(f"      {sku}")

    # ------------------------------------------------- the padding question
    heading("4. DOES THE ZERO-PADDING RULE MATTER?  (the decisive test)")
    print("   The client's sheet does TEXT(barcode,\"0000000000000\") - pad to 13 - and")
    print("   this system does the same. Below: what would happen WITHOUT padding.")
    print()
    in_scope_skus = {lst.seller_sku for lst in index.all_in_scope}
    naive_hits = padded_only = 0
    examples: list[tuple[str, str, str]] = []

    for canonical in vendor:
        # Reconstruct what the vendor actually put on the wire: zeros stripped.
        as_sent = canonical.lstrip("0") or "0"

        # The naive approach - plain string concatenation, no padding. Note this
        # must NOT go through build_sku(), which always pads; using it here was a
        # bug that made this whole test report zero.
        naive = f"{prefixes[0]}{as_sent}"
        correct = build_sku(prefixes[0], canonical)

        if naive in in_scope_skus:
            naive_hits += 1
        elif correct and correct in in_scope_skus:
            padded_only += 1
            if len(examples) < 5:
                examples.append((as_sent, naive, correct))

    total_reachable = naive_hits + padded_only
    print(f"   reachable WITHOUT padding   : {naive_hits:>8,}")
    print(f"   reachable ONLY WITH padding : {padded_only:>8,}")
    if total_reachable:
        print(f"   -> padding accounts for {100 * padded_only / total_reachable:.1f}% of all matches")
        print(f"   -> without it, {padded_only:,} listings would silently never update,")
        print("      because Amazon reports success for a SKU that does not exist")
    if examples:
        print()
        print("   examples:")
        for as_sent, naive, correct in examples:
            print(f"      vendor sends {as_sent}")
            print(f"         naive   -> {naive:<26} does not exist")
            print(f"         padded  -> {correct:<26} exists")

    # -------------------------------------------------------------- decisions
    heading("5. WHAT THE SYSTEM WOULD DO ON ITS FIRST RUN")
    rules = QuantityRules(
        safety_buffer=args.safety_buffer,
        max_quantity=args.max_quantity,
        out_of_stock_at=args.out_of_stock_at,
        min_change_to_push=0,
        allow_increases=True,
    )
    print(f"   rules: show vendor stock, hold back {rules.safety_buffer}, cap at "
          f"{rules.max_quantity}, out of stock at {rules.out_of_stock_at}")
    print()

    mapper = Mapper(index, sku_prefix=prefixes[0])
    engine = DecisionEngine(rules)
    decisions = []

    # Only barcodes Amazon actually knows about are worth mapping, plus in-stock
    # products that might be worth listing. The other ~1.04 million rows are
    # products the client has never listed AND that the vendor has none of --
    # counting those as "unmatched" would drown the real signal, which is
    # "the vendor has stock of something we could be selling".
    unmapped_in_stock = 0
    for canonical, (stock, fmt, _title) in vendor.items():
        known = bool(index.by_barcode(canonical))
        if not known and stock <= 0:
            continue  # not listed, and nothing to sell. Genuinely irrelevant.

        match = mapper.match(canonical)
        if not match.matched:
            if stock > 0:
                unmapped_in_stock += 1
            continue
        decisions.append(engine.decide(match, stock, product_format=fmt))

    # Products the vendor no longer carries at all.
    dropped = []
    for lst in index.all_in_scope:
        if lst.barcode and lst.barcode not in vendor:
            d = engine.decide_dropped(lst, missing_from_full_feeds=1, threshold=1)
            if d is not None:
                dropped.append(d)
    decisions.extend(dropped)

    pushable = [d for d in decisions if d.should_push]
    by_dir = Counter(d.direction.value for d in pushable)

    print(f"   products considered        : {engine.stats.considered:>8,}")
    print(f"   already correct            : {engine.stats.already_correct:>8,}")
    print(f"   WOULD CHANGE               : {len(pushable):>8,}")
    print()
    print(f"      take off sale (-> 0)    : {by_dir.get('to_zero', 0):>8,}   stops cancelled orders")
    print(f"      reduce                  : {by_dir.get('down', 0):>8,}")
    print(f"      raise                   : {by_dir.get('up', 0):>8,}   recovers lost sales")
    print(f"      of which vendor-dropped : {len(dropped):>8,}")
    print()
    print("   skipped, with reasons:")
    for reason, n in sorted(engine.stats.skipped.items(), key=lambda kv: -kv[1]):
        print(f"      {reason:<24} {n:>8,}")
    print()
    print(f"   unmatched (never sent)     : {mapper.stats.unmapped:>8,}   {mapper.stats.unmapped_reasons}")

    # ------------------------------------------------------------ ramp + rails
    heading("6. THE FIRST RUN, WITH THE BRAKE ON")
    batch, deferred = apply_change_limit(decisions, args.change_limit)
    print(f"   per-run change limit       : {args.change_limit:,}")
    print(f"   this run would send        : {len(batch):,}")
    print(f"   deferred to later runs     : {len(deferred):,}")
    if deferred:
        runs = -(-len(pushable) // args.change_limit)
        print(f"   runs needed to clear it    : {runs}")
    print()
    print("   priority order (dangerous direction first):")
    for d in batch[:6]:
        print(f"      {d.seller_sku:<26} {d.current_quantity} -> {d.desired_quantity:<4} {d.reason[:44]}")

    heading("7. SAFETY CHECKS ON THAT BATCH")
    settings = {
        "guardrail_max_percent_changed": 25.0,
        "guardrail_max_zeroing": 2000,
        "unmapped_spike_threshold": 500,
    }
    verdict = evaluate_batch(
        batch,
        in_scope_total=index.in_scope_count,
        settings=settings,
        unmapped=unmapped_in_stock,
        considered=mapper.stats.considered,
        hours_since_catalog_sync=1.0,
    )
    for r in verdict.results:
        mark = "PASS" if r.passed else ("HALT" if r.halts else "WARN")
        print(f"   [{mark}] {r.name}")
        print(f"          {r.message.splitlines()[0]}")
    print()
    print(f"   VERDICT: {'PROCEED' if verdict.passed else 'RUN WOULD BE STOPPED'}")

    # ------------------------------------------------------------------ json
    report = {
        "listings_file": args.listings.name,
        "feed_file": args.feed.name,
        "prefixes_in_scope": prefixes,
        "amazon": {
            "listings": rstats.parsed_rows,
            "by_status": rstats.by_status,
            "by_prefix": rstats.by_prefix,
            "in_scope": index.in_scope_count,
            "out_of_scope": index.out_of_scope_count,
        },
        "vendor": {
            "rows": fstats.total_lines,
            "usable": fstats.usable_rows,
            "rejected": fstats.rejected_rows,
            "in_stock": fstats.in_stock_rows,
            "max_stock": fstats.max_stock_seen,
        },
        "coverage": {
            "percent": round(coverage.listing_coverage_percent, 2),
            "matched_listings": coverage.matched_listings,
            "unmatched_listings": coverage.unmatched_listings,
            "by_prefix": coverage.by_prefix,
        },
        "padding": {
            "reachable_without_padding": naive_hits,
            "reachable_only_with_padding": padded_only,
        },
        "first_run": {
            "considered": engine.stats.considered,
            "already_correct": engine.stats.already_correct,
            "would_change": len(pushable),
            "to_zero": by_dir.get("to_zero", 0),
            "down": by_dir.get("down", 0),
            "up": by_dir.get("up", 0),
            "vendor_dropped": len(dropped),
            "skipped": engine.stats.skipped,
            "unmapped_in_stock": unmapped_in_stock,
            "unmapped_total": mapper.stats.unmapped,
            "batch_size": len(batch),
            "deferred": len(deferred),
        },
        "guardrails": verdict.as_dict(),
    }
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n   JSON written to {args.json}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
