# Architecture

For a developer picking this up. Read [`app/core/barcode.py`](../app/core/barcode.py)
first — the whole project hinges on what it documents.

---

## The shape of it

```
                     ┌──────────────────────────────────────────┐
                     │  Vendor FTPS  (ftp.vendor.example.com)   │
                     │  FULL_FEED_110708_20260904.zip           │
                     │  DELTA_FEED_110708_20260904_80.zip …     │
                     └───────────────────┬──────────────────────┘
                                         │ read-only
                                         ▼
  ┌───────────────────────────────────────────────────────────────────────┐
  │                        THE RUN  (one at a time)                       │
  │                                                                       │
  │  1 FETCH      vendor/ftp_client.py    new files only, by content hash │
  │  2 READ       vendor/parser.py        streaming, sanity gates         │
  │  3 STORE      → vendor_products, vendor_product_history               │
  │  4 ASK        amazon/reports.py       → amazon_listings               │
  │  5 MATCH      engine/mapping.py       barcode → CONFIRMED SKU         │
  │  6 DECIDE     engine/decision.py      rules, vs Amazon's own quantity │
  │  7 GATE       engine/guardrails.py    ══ a run can stop here ══       │
  │  8 SEND       engine/pusher.py        quantity only, then verify      │
  │                                                                       │
  │  orchestrated by engine/pipeline.py                                   │
  └───────────────────────────────┬───────────────────────────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
        ┌───────────────────────┐   ┌────────────────────────────┐
        │  amazon/guard.py      │   │  Amazon SP-API             │
        │  refuses ANY payload  │──▶│  PATCH /listings/… (small) │
        │  containing a price   │   │  POST  /feeds/…    (bulk)  │
        └───────────────────────┘   └────────────────────────────┘

  PostgreSQL ── settings · credentials(enc) · audit · runs · batches ·
                push_items(the undo trail) · reports
```

---

## The four decisions that shaped everything

### 1. Pad the barcode to 13 digits, in exactly one place

The vendor strips leading zeros; Amazon keeps them. Measured against the real catalogue
on 2026-09-04:

```
reachable WITHOUT padding :   15,572
reachable ONLY WITH padding:  29,701     ← 65.6% of all matches
```

The failure is silent — Amazon returns success for a SKU that does not exist and then
does nothing — so a bug here is invisible until somebody notices sales have quietly
stopped.

Consequence: `app/core/barcode.py` is the single source of that rule, its examples are
doctests, and **no other module is allowed to call `zfill`**. If you find yourself
writing `zfill(13)`, call `normalise()` instead.

### 2. Compare against Amazon's reported quantity, never against the last feed

```
feed-vs-feed:                          desired-vs-Amazon:
  Mon  vendor 0, push FAILS              Mon  want 0, Amazon 5, push FAILS
  Tue  0 vs 0 → "no change"              Tue  want 0, Amazon 5 → retry
  ...  broken forever, invisibly         Wed  confirmed
```

Consequence: `amazon_listings.quantity` is load-bearing. The catalogue refresh is not a
nice-to-have; without it the decision engine has no baseline. It also means every run is
idempotent — running twice is harmless — which is what makes retries safe.

### 3. Nothing unverified is ever sent

`engine/mapping.py` resolves in tiers, and every tier ends at a listing Amazon has
confirmed exists. There is deliberately **no** tier that says "construct the SKU and
hope". The formula is used only to *look up* a listing already in the index.

Consequence: `MatchResult.matched` is False rather than "probably fine", and unmatched
in-stock products go into `unmapped_barcodes` for a human.

### 4. The price invariant is structural, not configurable

`amazon/guard.py` walks every outbound payload and raises on anything price-shaped. It
is called from `amazon/client.py`, which is the only transport — so there is no path to
the network that skips it.

Consequence: the `never_send_price` settings row exists only so nobody can create a
setting that *appears* to turn it off. The real enforcement is code.

---

## Where each thing lives

| Concern | File | Note |
|---|---|---|
| Barcode rules | `core/barcode.py` | the padding rule; doctested |
| Client-editable behaviour | `core/settings_store.py` | **every** setting, with its default and help text |
| Infrastructure config | `config.py` | environment only; nothing behavioural |
| Which day a file belongs to | `vendor/filename.py` | the date is in the filename, so no timezone guessing |
| Reading feeds | `vendor/parser.py` | streaming; 103,000 rows/second |
| Fetching feeds | `vendor/ftp_client.py` | read-only by construction: no delete or rename path |
| Amazon transport | `amazon/client.py` | rate limits, retries, and the price gate |
| Price refusal | `amazon/guard.py` | the invariant |
| Barcode → SKU | `engine/mapping.py` | tiers, scope, and the coverage measurement |
| Quantity rules | `engine/decision.py` | pure functions, no database, no network |
| Circuit breakers | `engine/guardrails.py` | returns verdicts, never raises |
| Send + verify | `engine/pusher.py` | writes the undo trail *before* sending |
| Undo | `engine/rollback.py` | a rollback is a new batch, never an edit |
| Orchestration | `engine/pipeline.py` | the only place stages are sequenced |

**The rule of thumb:** if a value might ever need changing, it belongs in
`settings_store.py`. If it identifies the machine or the account, it belongs in
`config.py`. There is no third place.

---

## Concurrency

Exactly one run at a time, enforced by a **PostgreSQL advisory lock**
(`app/db.py::run_lock`), not by the scheduler.

Why an advisory lock rather than a row lock or a file lock:

- it is held by the session, so a crashed worker releases it automatically — no stale
  lock file to clear by hand at 3am
- it works across processes and containers, which a `threading.Lock` does not
- `pg_try_advisory_lock` returns immediately, so a scheduler firing every 15 minutes
  steps aside rather than queueing identical work

Two overlapping runs would each read Amazon's quantity, each compute a change from the
same starting point, and each push — producing double writes and a corrupt undo trail.

The lock is taken by: the scheduled sync, the catalogue refresh, "Run now", batch
approval, and every rollback.

---

## Transactions, and the one rule that governs them

A run has a side effect no database transaction can contain: it changes quantities on
Amazon. The moment a quantity is patched, that change exists in the world whether or not
our transaction later commits. Which gives the ordering rule the whole design obeys:

> **The record of what we are about to do must be durable before we do it.**

Concretely, `push_items` — one row per SKU, each carrying `previous_quantity` read from
Amazon — is written and **committed** before `send_batch` transmits a single byte. The
barrier is marked in `app/engine/pipeline.py` and explained in `app/db.py:checkpoint`.

### What it looked like when this was wrong

A run used to be a single transaction: ingest, parse, upsert 1.15 million rows, decide,
create the batch, send to Amazon, verify — committed once at the very end. Two
consequences, both bad:

| | |
|---|---|
| A container killed mid-send | Amazon changed, and the rollback discarded every `previous_quantity`. Those products could not be put back — the one promise this system makes, broken by a `docker compose down` |
| Transaction length | One transaction spanning a million-row upsert *and* minutes of rate-limited HTTP holds a snapshot open long enough to block autovacuum and bloat the tables |

### The checkpoints

```
run opened ──▶ vendor data stored ──▶ reports written ──▶ batch recorded
                                                              │
                                              ═══ DURABILITY BARRIER ═══
                                                              │
                                                    send ──▶ verify
```

Committing mid-run means a later failure does **not** roll back earlier stages. That is
intended, not a compromise. Partial progress with an accurate record beats atomicity that
cannot include Amazon anyway, and every stage is written to be idempotent and to
re-derive its state from Amazon on the next run.

The vendor checkpoint earns its place too: `feed_files.content_sha256` is what stops a
file being processed twice. Uncommitted, a later failure would roll that record back and
the same archive would be downloaded and reprocessed on every subsequent cycle.

### Recovery

Because `SENDING` is committed before the first call, an interrupted send is *visible*
rather than invisible — which then obliges something to act on it. `_recover_interrupted_batches`
runs at the start of every run, before any new work.

It can assume any batch still marked `SENDING` is abandoned, with no age heuristic: the
advisory run lock guarantees at most one run at a time, so there is no other run it could
belong to. Accepted items are verified against Amazon; items still `PENDING` were never
sent and are marked `SKIPPED` rather than blindly resent, because this run's own decision
pass will propose them again if Amazon still disagrees with the vendor — and doing it
there keeps them inside the guardrails and the change limit.

---

## Full feeds and delta feeds are not interchangeable

This is the distinction most likely to be broken by a well-meaning refactor.

| | Delta feed | Full feed |
|---|---|---|
| Contains | only what changed | everything the vendor carries |
| A barcode's **absence** means | **unchanged** | **the vendor has dropped it** |
| May zero a missing product | **never** | yes, per the threshold setting |
| Triggers a full reconcile | no | yes |
| Real size | 23–320 rows | ~1,158,340 rows |

Confusing them in one direction strands dead stock on sale forever. In the other it
zeroes the catalogue. `FeedKind` is explicit on every `FeedFile` row for that reason,
and `_mark_missing_from_full_feed` is the only place the dropped-product counter moves.

### Why a full feed forces a reconcile

Delta files only mention products that changed **at the vendor**. A product sitting at 27
in stock for weeks has nothing new to report, so it never appears in a delta — even if
Amazon shows 1. The accumulated drift (38,341 products on 2026-09-04) is therefore only
visible when the whole in-scope catalogue is walked, which is what a full feed triggers.

That is also why the per-run change limit exists: the reconcile surfaces the entire
backlog at once.

---

## The database, and what each table is for

```
settings            client-editable behaviour, key/JSON so no migration per knob
credentials         the four secrets, AES-256-GCM, aad-bound to the key name
users               one shared admin login (the client asked for exactly one)
audit_events        append-only; who changed what, when, from where

feed_files          one row per file EVER; content_sha256 is UNIQUE
vendor_products     what the vendor has; barcode is the CANONICAL 13-digit form
vendor_product_hist every change; answers "when did this sell out?" months later
rejected_rows       feed rows that could not be read (~153/day), kept not dropped

amazon_listings     what Amazon says  ← the self-healing baseline
catalog_syncs       each report download; snapshot_path enables restore-to-a-day

runs                every cycle, with the settings snapshot that governed it
push_batches        the unit of APPROVAL and of ROLLBACK
push_items          per-SKU before/after  ← THE UNDO TRAIL. immutable.

unmapped_barcodes   the human queue (in-stock only, or it would be 1.1M rows)
sku_overrides       manual mappings
notifications       alerts with delivery state, so an SMTP outage loses nothing
report_files        the five .xlsx files, per run
```

### Two type variants worth knowing about

Both in `app/models.py`, both so the test suite can run on SQLite while production gets
the right PostgreSQL type:

```python
JSONType  = JSON().with_variant(JSONB(), "postgresql")
BigIntPk  = BigInteger().with_variant(Integer, "sqlite")
```

`BigIntPk` is not cosmetic: SQLite only auto-increments a column declared *exactly*
`INTEGER PRIMARY KEY`. A `BIGINT PRIMARY KEY` there has no default, so every insert
fails with a NOT NULL violation. Production wants BIGINT because `push_items` gains a
row per SKU per batch.

### `push_items.previous_quantity`

The most important column in the database. It is captured from `amazon_listings`
**immediately before the send**, so it reflects Amazon's own reported quantity rather
than anything this system inferred. Everything about undo depends on it.

Be precise about what that does and does not claim. `amazon_listings` is the catalogue
cache, refreshed by the All Listings Report rather than by a live read per SKU at push
time — 5,000 individual reads would cost 5,000 requests for a batch. So
`previous_quantity` is Amazon's last *reported* value, which can be up to a catalogue
refresh old; `check_catalog_freshness` warns once that gets beyond 48 hours. The
consequence worth knowing: if somebody edits a quantity by hand in Seller Central
between the last refresh and a push, undo restores the value from before their edit.
That is inherent to reconciling against a periodically-fetched report, not a defect —
but it is the reason catalogue freshness is a guardrail and not a detail.

This is also why `decision.py` refuses to push a listing whose Amazon quantity is
unknown (`SkipReason.NO_AMAZON_QUANTITY`) — pushing blind would work, but it would make
the undo record meaningless. **Every push is reversible by construction.**

---

## The Amazon side

Amazon dropped the AWS SigV4 signing requirement from SP-API, so this needs **no AWS
account, no IAM user and no role**. Only:

```
POST https://api.amazon.com/auth/o2/token      refresh token → access token (1 hour)
     header x-amz-access-token: <that>          on every SP-API call
```

### Two write paths, chosen by size

| | Listings Items | Feeds |
|---|---|---|
| Shape | `PATCH /listings/2021-08-01/items/{seller}/{sku}` | one `JSON_LISTINGS_FEED` document |
| Latency | seconds | minutes (submit → poll → report) |
| Feedback | per SKU, immediately | a processing report to parse |
| Used for | delta runs (a few hundred) | the daily reconcile (thousands) |
| Rate limit | ~5/s per seller (we use 2/s) | 1 per 2 minutes |

Crossover is the `feeds_threshold`, default 500.

### The values are real, not guessed

Every constant came out of the client's own Price & Quantity template rather than from
documentation. The template's attribute row gives the exact strings for this account:

```
contribution_sku#1.value                               → the SKU
fulfillment_availability#1.fulfillment_channel_code    → "DEFAULT" (merchant fulfilled)
fulfillment_availability#1.quantity                    → THE FIELD WE WRITE
fulfillment_availability#1.lead_time_to_ship_max_days  → handling time
```

and its settings blob, base64-decoded:

```
ptds=UFJPRFVDVA==                    → product type is "PRODUCT"
AttributeDefaultValues={"product_type#1.value":"PRODUCT",
                        "record_action#1.value":"partial_update"}
primaryMarketplaceId=…ATVPDKIKX0DER  → US marketplace
fulfillment_channel_code aliases: "Fulfillment by Merchant (Default)" → "DEFAULT"
```

### The trap: `replace` replaces the whole object

`fulfillment_availability` is an **array**, and a JSON-Patch `replace` swaps the entire
array. A naive quantity patch therefore **deletes the seller's handling time**, because
`lead_time_to_ship_max_days` lives inside the same object.

That is not cosmetic: Amazon falls back to a default, the promised delivery date
changes, and late-shipment metrics suffer — on the very account whose health this system
exists to protect.

So `build_quantity_patch` takes the existing values and sends them back unchanged. They
are cached on `amazon_listings.lead_time_to_ship_days` so the common path costs no extra
call. Tested in `tests/test_engine.py::test_handling_time_is_preserved`.

### This account's report is missing two columns

The All Listings Report here contains only:

```
seller-sku    asin1    price    quantity    status
```

No `product-id` and no `fulfillment-channel`. The usual advice — match on product-id,
filter on fulfillment-channel — cannot be followed. So:

- the barcode is recovered from **inside the SKU** (`split_sku`), which works because
  every SKU on this account embeds a 13-digit barcode
- the fulfilment channel defaults to merchant-fulfilled (confirmed by the client and by
  their filled template) and is corrected per SKU whenever a listing is read individually

The file also carries a **UTF-8 byte-order mark**, which attaches itself to the first
column name and turns `seller-sku` into `﻿seller-sku` — silently breaking a naive
`DictReader`. Read with `utf-8-sig`.

---

## Why these choices, and not the obvious alternatives

| Chose | Instead of | Because |
|---|---|---|
| PostgreSQL | SQLite | a full feed upserts 1.15M rows while the dashboard serves reads; SQLite's single-writer lock makes that minutes of blocked requests |
| APScheduler | Celery | one job at a time on one machine. A broker + worker + beat is three more processes to install, monitor and restart, solving a distribution problem that does not exist |
| Advisory lock | scheduler `max_instances` | the lock holds across processes, so a second container or a "Run now" click also steps aside |
| Streaming stdlib CSV | pandas | 1.15M `FeedRow` objects is ~1 GB; the stream is flat memory and removes a 60 MB dependency from the image |
| Jinja2 + hand-written MD3 CSS | React/Vue + a bundler | **no build step to break at deploy time.** ~34 KB of assets, and the CSP stays `default-src 'self'` with no exception for scripts |
| XlsxWriter | CSV | Excel destroys leading zeros in a CSV on open, and 65.6% of matches depend on those zeros. Real `.xlsx` with text-typed columns cannot be mangled |
| Synchronous httpx | async | the pipeline is one job on a worker thread; async would add complexity for throughput this workload does not need |
| Hand formatting, `ruff check` only | `ruff format` | the rate-limit tables, settings specs and design-token blocks are aligned on purpose; Black-style reflow destroys information |

---

## Testing

273 tests, ~69% coverage, mypy clean. **No network, no live database.** A test that could
reach Amazon is a test that could change a live listing, and that must not exist in a CI
pipeline. Anything needing a live service is `@pytest.mark.integration` and excluded by
default.

Amazon is faked at the HTTP transport (`httpx.MockTransport`) underneath a real
`SpApiClient`, never at the client level. That choice matters: it keeps the rate limiter,
the retry and backoff logic, the 401 token refresh, the error extraction and — above all
— the price guard inside `SpApiClient.request` on the tested path. Faking one level higher
would silently exclude the check the client exists to perform.

| File | What it protects |
|---|---|
| `test_barcode.py` | the padding rule, with real vendor/Amazon barcode pairs |
| `test_engine.py` | quantity rules, scope filter, guardrails, the price invariant |
| `test_pusher.py` | the write path: the durability barrier, per-SKU failure isolation, "ACCEPTED but not applied", undo, recovery after a kill mid-send |
| `test_pipeline_end_to_end.py` | a whole run with both edges faked; pause, guardrail halt, approval, idempotence, dedupe |
| `test_migrations.py` | `migrations/` and `app/models.py` cannot drift apart |
| `test_parser_safety.py` | truncated downloads, CRC failures, expansion bounds, header changes |
| `test_lwa.py` | token caching, the double-checked lock, and the operator-facing auth messages |
| `test_config_guards.py` | the configurations production must refuse to start with |
| `test_web.py` | every page renders; nothing is public; **no secret ever reaches a browser** |
| doctests in `app/` | the documented examples are executed, so docs cannot drift |

### The one deliberate gap

`vendor/ftp_client.py` sits at 23%. Exercising FTPS meaningfully needs a real server —
a mock that returns whatever the test wants proves only that the mock works, and the
failures that matter here (TLS session reuse across the data channel, passive mode
through NAT, MLSD missing on some servers) are exactly the ones a mock cannot produce.
It is covered by the connection test on the Settings page and by the deployment
checklist in `OPERATIONS.md` instead. Stated rather than hidden behind an average.

`tests/conftest.py` holds real production data as fixtures — actual feed rows, actual
SKUs including the malformed ones found live (`": HA-INGR-…"`, `"A-AMS-…"` with a
missing letter). Using real shapes rather than invented ones is the point: the bugs this
system must not have are bugs about *this* vendor's quirks.

Two bugs were found by these tests during development, both worth knowing about:

1. `RedactingFilter` called `str()` on every log argument, turning integers into strings
   and making every `%d` in the codebase raise `TypeError` at log time — so the logging
   that was meant to be protected stopped working entirely.
2. `Mapper._finish` **mutated** the shared `ListingEntry` when blacklisting, so the flag
   leaked between mappers built over the same index. It now returns a replacement.

---

## Adding things

**A setting:** three lines in `core/settings_store.py::SPECS`. It appears on the page
with its label, help text, type and bounds. No migration, no template edit.

**A guardrail:** a `check_*` function in `engine/guardrails.py` returning a
`GuardrailResult`, then add it to `evaluate_batch` or `evaluate_feed`. Return a verdict,
never raise — all checks must run so one alert reports everything that is wrong.

**A page:** a route in `routers/`, a template extending `base.html`. Take
`who: SessionData = Depends(require_login)` — `tests/test_web.py::test_requires_login`
fails if you forget, which is the point.

**A vendor:** the column map, delimiter and SKU prefix are already settings. A second
vendor needs a `vendor_id` on `feed_files` and `vendor_products`, and the scope check
becomes per-vendor. The schema was shaped with that in mind but it is not built.

**An Amazon field:** think hard. The system deliberately writes one field. Adding
another means widening `guard.ALLOWED_WRITE_FIELDS`, and anything price-shaped will be
refused — correctly.
