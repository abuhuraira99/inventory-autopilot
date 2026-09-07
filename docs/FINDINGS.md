# Stage 0 — what the data actually showed

Measured on **4 September 2026** against the client's real files, before any code was
written to touch Amazon. Every number here is reproducible:

```bash
python scripts/stage0_coverage.py \
  --listings "All+Listings+Report_09-04-2026.txt" \
  --feed     "Full Feed/FULL_FEED_110708_20260903.zip"
```

No credentials and no network needed. That was the point: the client learns something
concrete about their own account before agreeing to let software write to it, and the
answer is worth having whether or not the automation ships.

---

## 1 · The vendor's feed, as it really is

Files from the All Media Supply FTP server:

| Filename | On disk | Uncompressed | Rows |
|---|---|---|---|
| `FULL_FEED_110708_20260901.zip` | 27,530,843 B | 74,674,748 B | 1,150,544 |
| `FULL_FEED_110708_20260902.zip` | 27,906,707 B | 75,470,681 B | — |
| `FULL_FEED_110708_20260903.zip` | 27,803,235 B | 75,496,523 B | 1,158,340 |
| `DELTA_FEED_110708_20260904_80.zip` | 3,768 B | 6,838 B | 105 |
| `DELTA_FEED_110708_20260904_88.zip` | — | — | 41 |
| … 20 delta files, sequence 80–99 | | | 23–320 each |

**The filename carries the date.** That was the single most useful discovery of the
exercise:

```
<KIND>_FEED_<account>_<YYYYMMDD>[_<sequence>].zip
```

The client had asked for "only today's files" and worried about timezones. Because the
date is in the filename, the rule is an exact string comparison rather than a
timestamp heuristic — no clock skew, no ambiguity, no daylight-saving edge case. The
sequence number gives a total order within a day, so files are always applied
oldest-first even if the directory listing arrives jumbled.

The zip timestamps confirm the cadence exactly: sequence 80 at 07:14, 81 at 07:19, 82 at
07:24 — five minutes apart. Full feeds are zipped around 23:07–23:10.

### Format

Named `.csv`, actually **pipe-delimited**:

```
barcode|artist|title|price|stock|format
5413356068320|GARNIER,LAURENT|RETROSPECTIVE|12.12|0|CD
```

All 23 files, full and delta, share the same header.

### Contents of one full feed

| | |
|---|---|
| Rows read | 1,158,493 |
| Usable | 1,158,340 |
| Unreadable | 153 — all `barcode_wrong_length` |
| In stock (>0) | 116,093 |
| **Out of stock** | **1,042,247 — 90.0% of the catalogue** |
| Largest stock value | 5,653 |
| Failing the barcode check digit | 393 (0.03%) |
| Duplicate barcodes within one file | **0** |

That last row matters: the client said "there will be a unique barcode for every single
product", and 1.15 million rows agree. The duplicate-handling code exists anyway, but as
insurance rather than a routine path.

Parse performance, measured: **11.2 seconds, 103,619 rows/second**, streaming at flat
memory.

---

## 2 · Amazon's side

`All+Listings+Report_09-04-2026.txt`, 96,010 rows. Five columns only:

```
seller-sku    asin1    price    quantity    status
```

**No `product-id` and no `fulfillment-channel`.** The usual advice — match on product-id,
filter on fulfillment-channel — cannot be followed on this account. The barcode has to be
recovered from inside the SKU instead.

The file also carries a UTF-8 **byte-order mark**, which attaches to the first column
name and turns `seller-sku` into `﻿seller-sku`, silently breaking a naive `DictReader`.
That cost twenty minutes and is now handled by reading with `utf-8-sig`.

### By status

| Status | Count |
|---|---|
| Active | 79,802 |
| Inactive | 15,768 |
| Incomplete | 440 |

### By SKU prefix — each one is a different supplier

| Prefix | Listings | Present in the AMS feed | |
|---|---|---|---|
| `HA-AMS-` | **45,511** | **99.5%** | ← All Media Supply |
| `HA-INGR-` | 22,519 | 0.0% | Ingram |
| `RA-OLD-` | 2,170 | 84.6% | ambiguous |
| `HB-HM-` | 1,906 | 2.5% | |
| `FA-OLD-` | 1,861 | 68.3% | ambiguous |
| `AS-OLD-` | 1,592 | 79.1% | ambiguous |
| `RA-THB-` | 461 | — | |
| `MR-OLD-` | 340 | 73.2% | ambiguous |
| `FA-THB-` | 328 | — | |
| `HB-WOB-` | 239 | — | |
| others | ~19,000 | mixed | |

**This is what makes a safe scope filter possible.** `HA-AMS-` identifies the vendor's
catalogue at 99.5% accuracy, so the prefix can be trusted. Touching an `HA-INGR-` listing
would corrupt Ingram's inventory.

The `*-OLD-` prefixes are the interesting case: 68–85% match, which looks like older AMS
listings. They are **deliberately excluded**, because under the "missing from the full
feed means set to 0" rule the 15–32% that are absent would be switched off — possibly
wrongly. The client agreed with that call. Adding them later is a settings change.

Also found: the SKU prefixes correspond to `merchant_shipping_group` aliases inside the
client's own template (`AMS`, `INGRAM`, `OLD`, `HM`, `WOB`, `PUBLICATION`), which
independently confirms the prefix-means-supplier reading.

---

## 3 · The coverage number

> ### **99.5%**
> 45,291 of 45,511 in-scope Amazon listings match a row in the vendor's feed.

The remaining 220 are products Amazon still lists that the vendor no longer carries —
genuinely discontinued, and exactly what the dropped-product rule is for.

This is the direction that matters. The reverse figure — what share of the vendor's 1.15
million products are listed — is naturally low and not interesting: the client lists
about 45,500 of them on purpose.

---

## 4 · The decisive test: does the zero-padding matter?

The vendor strips leading zeros. Amazon keeps them. The client's Google Sheet already
bridges the gap:

```
H2 = TEXT(G2, "0000000000000")     pad to exactly 13
E2 = "HA-AMS-" & H2
```

**How much depends on that formula?** Building the SKU by plain concatenation instead:

| | |
|---|---|
| Reachable **without** padding | **15,572** |
| Reachable **only with** padding | **29,701** |
| Padding's share of all matches | **65.6%** |

Real examples:

```
vendor sends 656605142524
   naive  →  HA-AMS-656605142524     does not exist
   padded →  HA-AMS-0656605142524    exists

vendor sends 90204695072
   naive  →  HA-AMS-90204695072      does not exist
   padded →  HA-AMS-0090204695072    exists
```

Without the padding, **29,701 listings would silently never update** — and there would be
no error to notice, because Amazon returns success for a SKU that does not exist and then
does nothing.

### What this means, honestly

The client's process is **correct**. They were right to push back on the suggestion that
their SKU mapping was a guess: the formula does the padding, and that is precisely why
their manual process works.

What the measurement adds is knowing *how much* rests on it — two thirds of the
catalogue — and that the rule is now written down in one place, doctested, with no
`zfill` allowed anywhere else in the codebase.

### A related finding, from the old database

The previous tool's `inventory.db` holds 126,623 products. Running the GTIN check digit
over all of them:

| Barcode length | Count | Valid as stored | Valid after adding zeros |
|---|---|---|---|
| 13 | 41,836 | 99% | — |
| 12 | 72,631 | 99% | — |
| **11** | **12,015** | **0%** | **99%** |
| **10** | **134** | **0%** | **100%** |
| **9** | **4** | **0%** | **100%** |

And **not one** of the 126,623 barcodes started with `0` — statistically impossible for a
catalogue of American music and DVDs, where a large share of barcodes begin with zero.

So ~12,153 stored barcodes (about 10%) are damaged **in that database**. It did not break
the manual process because the spreadsheet re-padded them on the way out. It is recorded
here because it explains where the padding requirement comes from, and because the new
system stores the canonical form instead.

---

## 5 · What the system would do on its first run

Rules as the client specified: publish the vendor's stock, no safety margin, cap at 15,
out of stock at 0.

| | |
|---|---|
| Products considered | 45,368 |
| Already correct | 3,065 |
| **Would change** | **38,341** |
| — take off sale (→ 0) | **196** |
| — reduce | 31 |
| — raise | 38,114 |
| of which the vendor has dropped entirely | 90 |
| Skipped: listing not Active | 3,962 |
| In stock at the vendor but not listed | 74,140 |

### The 196

Products Amazon was **actively selling with zero vendor stock**. Each is a cancelled
order waiting to happen, and cancellations damage account health — the hardest thing on
an Amazon account to repair.

These are the reason the project exists.

### The 38,114

Almost every listing sits at quantity 1 while the vendor holds 5, 11 or 27. Those are
sales not being made. They are also why the per-run change limit matters, and why the
client's question was a good one:

> *"isn't this type of change automatically going to occur based on the full feed and the
> delta feed from the particular day on which the system is going to be deployed?"*

Not quite. Today's delta files only contain products that changed **at the vendor
today**. A product sitting at 27 in stock for weeks has nothing new to report, so it
never appears in a delta. Only the daily **full feed** exposes the accumulated backlog —
and it exposes all 38,341 at once.

Hence the brake. At the client's chosen 5,000 per run, the backlog clears in about eight
runs, worst cases first.

### The 74,140

Products the vendor has in stock that are not listed on Amazon. **This is normal and
healthy** — the vendor carries 1.15 million products and the client lists 45,500 of them
by choice. They are listing opportunities, and they are the same set that appears in the
team's "New Products" report.

The guardrail default was raised from 500 to 90,000 once this was measured. A threshold
that fires on the normal state is worse than no threshold: people learn to ignore it.

---

## 6 · The first batch, with the brake on

| | |
|---|---|
| Change limit | 5,000 |
| Sent this run | 5,000 |
| Deferred | 33,341 |
| Runs to clear the backlog | 8 |

Priority order, dangerous direction first:

```
HA-AMS-0199316179293    5 → 0    the vendor has dropped this product
HA-AMS-0820200662170    5 → 0    vendor has 0, out of stock; Amazon shows 5
HA-AMS-0602517522961    2 → 0    the vendor has dropped this product
HA-AMS-0805772629424    2 → 0    vendor has 0, out of stock; Amazon shows 2
```

### Safety checks on that batch

| Check | Result | |
|---|---|---|
| Percentage changed | **PASS** | 5,000 of 45,511 = 11.0%, within 25% |
| Zeroing limit | **PASS** | 196, within 2,000 |
| No price fields | **PASS** | quantities only |
| Unmatched spike | **PASS** | 74,140, within 90,000 |
| Catalogue freshness | **PASS** | |

**Verdict: proceed.** Which is the right answer — the system must be able to start.

---

## 7 · Amazon errors the client already hits

From the processing report of their last manual upload: **656 SKUs processed, 603
successful, 53 "successful with other errors", 0 unsuccessful.**

Those 53 were real failures wearing a success label. Treating `DONE` as success would
have hidden every one of them, which is why `parse_processing_report` reads the per-SKU
issues rather than the summary.

| Code | Count | Meaning |
|---|---|---|
| 13013 | 53 | "offer cannot be added because the product is not in the catalog" — a new-product problem, not a stock problem |
| 8684 | 4 | "SKU is associated to more than 1 GCID" — needs fixing in Seller Central; no quantity update will work until then |
| 8560 | 2 | identifier not matched. Amazon's own advice: *"UPCs should have 12 digits, EANs 13"* |

All three are handled with a plain-language explanation in
`amazon/client.py::_explain_error`, so an operator sees what to do rather than a bare
code.

---

## 8 · Live data-entry damage found on the account

Real SKUs, currently active:

```
": HA-INGR-9798385266500"     ← a leading colon and space, from a paste
"A-AMS-088438889432"          ← missing the leading H
"18-HM-7756615"
```

None are in scope, so the system will not touch them. They are kept as test fixtures so
the parser is never allowed to choke on them.

---

## 9 · The Amazon app was missing a permission — now fixed

**Resolved 7 September 2026:** `Product Listing` was ticked and a new refresh token
issued. The Client Secret was also supplied. Both blockers are cleared; this section is
kept as the record of what was found and why it mattered.

From the screenshot of Developer Central supplied on 5 September 2026:

| Role | State | |
|---|---|---|
| Pricing | ☑ ticked | **should be removed** — this system must never change a price |
| Inventory and Order Tracking | ☑ ticked | correct, needed to read reports |
| Brand Analytics | ☐ | correct |
| **Product Listing** | **☐ NOT ticked** | ⚠️ **this is the role that permits a quantity change** |

Marketplaces authorised: Mexico, Canada, United States, Brazil. The client sells in the
US only, so the system targets `ATVPDKIKX0DER`.

This is exactly the failure predicted at the design stage: a read-only permission looks
almost identical in the console and fails only at the very first attempt to push. Caught
in week zero rather than week four.

**Also missing:** the LWA **Client Secret**. Of the credentials supplied — refresh token,
merchant token, application id, client id — the secret was the one absent, and nothing
can talk to Amazon without it.

Both fixes: [AMAZON-APP-SETUP.md](AMAZON-APP-SETUP.md).

⚠️ Changing the roles **invalidates the current refresh token**. A new one must be
generated afterwards.

---

## What Stage 0 was worth

Before a single line of code touched Amazon, the exercise produced:

1. **99.5% coverage** — the project is viable, with a number rather than a hope
2. **196 products actively overselling** — a concrete, quantified problem
3. **38,341 quantities out of step** — the scale of the backlog, and the reason for the
   per-run brake
4. **65.6% of matches depend on zero-padding** — the single most important rule, measured
5. **The filename carries the date** — the "today only" requirement became trivial
6. **The report is missing two columns** — the mapping strategy had to change, and it was
   better to learn that from a file than from a failing run
7. **The app is missing `Product Listing`** — a week-four disaster found in week zero
8. **The `*-OLD-` prefixes are ambiguous** — a scope decision made on evidence

Cost: a few files emailed over, and an afternoon.
