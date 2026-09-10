# Inventory Autopilot

[![CI](https://github.com/abuhuraira99/inventory-autopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/abuhuraira99/inventory-autopilot/actions/workflows/ci.yml)
[![Python 3.12 | 3.14](https://img.shields.io/badge/python-3.12%20%7C%203.14-blue.svg)](https://www.python.org/downloads/)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-green.svg)](LICENSE)
[![Checks: 329 tests · ruff · mypy](https://img.shields.io/badge/checks-329%20tests%20%C2%B7%20ruff%20%C2%B7%20mypy-brightgreen.svg)](CONTRIBUTING.md)

Keeps Amazon stock quantities in step with a supplier's data feed, automatically.

**It changes stock quantity and nothing else. It never changes prices.**

---

## The problem this solves

A vendor publishes a full product feed once a day and a small "delta" feed roughly
every five minutes. Keeping Amazon in step by hand means: open FileZilla, download a
zip, run it through a local database, email five spreadsheets, rebuild the SKU list
with a spreadsheet formula, and upload a template in Seller Central. That takes hours
and can realistically be done once a day.

The arithmetic does not work. Amazon keeps advertising stock the vendor no longer has,
orders arrive that cannot be fulfilled, they get cancelled, and Amazon penalises the
account for it.

This system replaces the person with a service that runs on its own clock and does the
same job in seconds — with safety rails, a full audit trail, and one-click undo.

### Measured on the live account, 4 September 2026

| | |
|---|---|
| Vendor products in the daily feed | **1,158,340** (75 MB, parsed in 11 seconds) |
| In stock at the vendor | 116,093 |
| Listings on the Amazon account | 96,010, of which **45,511** belong to this vendor |
| Coverage — listings matched to a vendor row | **99.5%** |
| **Products Amazon was selling with zero vendor stock** | **196** |
| Quantities out of step with the vendor | **38,341** |

That last figure is the accumulated cost of doing this by hand.

---

## Start here

| If you are… | Read |
|---|---|
| the client, and want to know what it does | [docs/CLIENT-GUIDE.md](docs/CLIENT-GUIDE.md) — plain English, no jargon |
| deploying it | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — Linux and Windows, side by side |
| setting up the Amazon app | [docs/AMAZON-APP-SETUP.md](docs/AMAZON-APP-SETUP.md) |
| running it day to day | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| a developer picking this up | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), then [`app/core/barcode.py`](app/core/barcode.py) |
| reviewing the security | [docs/SECURITY.md](docs/SECURITY.md), and [SECURITY.md](SECURITY.md) for the reporting policy |
| **reviewing this codebase** | [CONTRIBUTING.md](CONTRIBUTING.md) — the five things that are load-bearing, and why |
| wondering what has changed, and what was wrong | [CHANGELOG.md](CHANGELOG.md) |
| wondering what the data actually showed | [docs/FINDINGS.md](docs/FINDINGS.md) |

---

## How it works

Eight stages. Data moves one way.

```
  1        2         3          4           5         6         7          8
FETCH →  READ  →  STORE  →  ASK AMAZON → MATCH →  DECIDE →  SAFETY →  SEND
files   & check   vendor    what it has  barcode   new qty    CHECK   & VERIFY
                   data                   to SKU               │
                                                     (a run can be stopped here)
```

1. **Fetch** — poll the vendor's folder; download only files never seen before, by
   content hash. Verify the archive opens. Never delete anything remotely.
2. **Read** — stream the pipe-delimited file, repair barcodes, apply sanity gates.
3. **Store** — what the vendor has now, plus a full change history.
4. **Ask Amazon** — pull the All Listings Report so we know what Amazon currently shows.
5. **Match** — barcode → *confirmed* Amazon SKU. Anything unconfirmed is held, never sent.
6. **Decide** — apply the client's rules, then compare against Amazon's own quantity.
7. **Safety** — circuit breakers, then the mode: practice / ask first / automatic.
8. **Send** — write the quantity field only, then read Amazon back to confirm it landed.

---

## The three things that make this different

### 1. The zero-padding rule

The vendor strips leading zeros from barcodes. Amazon does not. Measured against the
real catalogue:

```
vendor sends  656605142524
  naive       HA-AMS-656605142524    ← does not exist
  padded      HA-AMS-0656605142524   ← exists
```

**29,701 of 45,273 matches (65.6%) depend on padding the barcode to 13 digits.**
Get it wrong and two thirds of the catalogue silently never updates — because Amazon
reports *success* for a SKU that does not exist, then does nothing.

The client's own spreadsheet already does this (`TEXT(barcode,"0000000000000")`), which
is why the manual process works. The rule lives in one place:
[`app/core/barcode.py`](app/core/barcode.py).

### 2. It compares against Amazon, not against yesterday's feed

The naive design compares today's feed with the previous one. That breaks permanently
the first time a push fails:

```
Monday     vendor says 0 → change detected → upload → FAILS SILENTLY
Tuesday    vendor still 0 → "no change" → nothing sent
forever    Amazon keeps selling a product with no stock
```

Storing Amazon's *own reported quantity* and comparing against that makes every run
idempotent and self-healing: a failed push is simply retried next cycle.

### 3. Nothing unverified is ever sent

A barcode with no confirmed Amazon listing goes into a queue for a human. It is never
pushed on a constructed SKU and a hope.

---

## Safety

Three modes, and weeks are meant to be spent in the first two:

| Mode | What happens |
|---|---|
| **Practice** | Works everything out, shows exactly what it *would* send, sends nothing |
| **Ask first** | Prepares the batch, notifies a human, waits for Approve |
| **Automatic** | Sends on its own, with the guardrails watching |

Circuit breakers, checked before every send:

| Condition | Protects against | Action |
|---|---|---|
| >25% of the catalogue would change | a corrupt or wrong-vendor file | **halt** |
| >2,000 products would go to zero | a catalogue-wide wipe | **halt** |
| full feed under 50% of its usual rows | a truncated download read as "all sold out" | **halt** |
| CSV columns changed | stock read from the price column | **halt** |
| damaged archive | a file grabbed mid-upload | **halt** |
| **payload contains a price** | the one thing the client forbade | **refuse, always** |

That last row is not a setting. It is enforced in
[`app/amazon/guard.py`](app/amazon/guard.py), which walks every outbound payload and
refuses to transmit anything price-shaped. A promise that important should not be one
careless click from being switched off.

**Undo:** every push records the previous quantity of every SKU, taken from Amazon
itself. One click puts them back. See [`app/engine/rollback.py`](app/engine/rollback.py).

Two properties make that promise real rather than nominal, and both were mistakes worth
naming:

- **The record is committed before the send begins.** A run used to be one long
  transaction committed at the end, so a container killed mid-send left Amazon changed
  with no record of what it had been. There is now an explicit durability barrier in
  [`app/engine/pipeline.py`](app/engine/pipeline.py) — see
  [`app/db.py:checkpoint`](app/db.py) for the reasoning. A batch interrupted mid-flight
  survives as `SENDING`, and the next run settles it against Amazon.
- **Undo trusts what was sent, not the daily catalogue cache.** `amazon_listings.quantity`
  is refreshed once a day plus whatever verification samples — 100 items out of 5,000. The
  first version of the undo planner compared that cache against the value it would restore
  and skipped the item when they matched, which meant that pressing Undo shortly after a
  large run skipped nearly every product and reported "already showing 7" for products
  Amazon was showing as 0. It now uses the strongest evidence available per item, and
  errs towards restoring: writing a quantity Amazon already holds is a no-op, while
  failing to restore one leaves stock on sale that does not exist.

**Honest caveat:** Amazon's sandbox returns canned data and knows nothing about the real
listings, so there is no way to fully rehearse without touching the live account. The
plan is to make the first touch small and reversible — a whitelist of 25–50 slow-moving
products, in "ask first" mode, and **press undo on purpose before widening it**.

---

## What the client controls without a developer

Every one of these is a field on the Settings page, with an audit trail:

mode · pause everything · check interval · timezone · SKU prefixes in scope ·
safety buffer · maximum quantity · out-of-stock threshold · minimum change worth
sending · dropped-product rule · never-touch list · all guardrail thresholds ·
column mapping · SKU prefix rule · report retention · alert recipients

There is no second place where behaviour is configured. Adding a knob is three lines in
[`app/core/settings_store.py`](app/core/settings_store.py) and it appears on the page
automatically.

---

## Running it

```bash
git clone https://github.com/abuhuraira99/inventory-autopilot.git
cd inventory-autopilot

cp .env.example .env
# Generate the two required keys and paste them into .env:
python -c "import secrets,base64; print('MASTER_KEY=' + base64.b64encode(secrets.token_bytes(32)).decode())"
python -c "import secrets; print('SESSION_SECRET=' + secrets.token_urlsafe(32))"

docker compose up -d --build
docker compose run --rm app alembic upgrade head
docker compose logs -f app     # the first-run admin password is printed here, once
```

Then open <http://localhost:8000>. The dashboard is bound to localhost only — reach it
through a Cloudflare Tunnel or Tailscale rather than exposing it. Full instructions in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

### Before any credentials: measure the catalogue

This runs offline, against files the client can email over, and answers the only
question that matters at the start:

```bash
python scripts/stage0_coverage.py \
  --listings "All+Listings+Report.txt" \
  --feed     "FULL_FEED_110708_20260903.zip"
```

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
pytest                    # 329 tests, no network, no live database
ruff check .
mypy                      # configured in pyproject.toml; passes clean
```

**329 tests, ~69% coverage, and mypy clean.** The suite never touches the network or a
real database — a test that could reach Amazon is a test that could change a live
listing. Amazon is faked at the HTTP transport with `httpx.MockTransport` underneath a
real `SpApiClient`, so the rate limiter, the retry logic, the token refresh and the
price guard all run for real. Anything needing a live service is marked
`@pytest.mark.integration` and excluded by default.

Coverage is uneven on purpose. What matters is *where* it is:

| | |
|---|---|
| `engine/decision.py` · `core/barcode.py` | 98% · 97% |
| `engine/guardrails.py` · `engine/mapping.py` · `amazon/guard.py` | 95% · 94% · 94% |
| `amazon/listings.py` · `engine/report_builder.py` | 86% · 86% |
| `engine/pipeline.py` · `amazon/client.py` · `vendor/parser.py` | 74% · 73% · 74% |
| `engine/pusher.py` · `engine/rollback.py` | 69% · 69% |
| `vendor/ftp_client.py` | 23% — needs a real server; see docs/OPERATIONS.md |

The highest-value tests, if you only read a few:

- [`tests/test_barcode.py`](tests/test_barcode.py) — the padding rule, with the real
  vendor/Amazon barcode pairs
- [`tests/test_pusher.py`](tests/test_pusher.py) — the write path: the durability
  barrier, per-SKU failure isolation, "Amazon said ACCEPTED and did nothing", undo, and
  recovery from a run killed mid-send
- [`tests/test_pipeline_end_to_end.py`](tests/test_pipeline_end_to_end.py) — a whole run
  with the vendor and Amazon faked at their edges, including idempotence
- [`tests/test_migrations.py`](tests/test_migrations.py) — the schema in `migrations/`
  and the schema in `app/models.py` cannot drift apart
- [`tests/test_web.py`](tests/test_web.py) — every page renders, nothing is public, and
  **no stored secret is ever rendered to a browser**

### Stack, and why

| Choice | Reason |
|---|---|
| Python + FastAPI | one language across the pipeline and the dashboard |
| PostgreSQL | a full feed upserts 1.15M rows while the dashboard serves reads; SQLite's single-writer lock makes that unusable |
| APScheduler, not Celery | one job at a time on one machine; a broker plus worker plus beat would be three more things to break |
| Jinja2 + hand-written MD3 CSS | **no build step.** Nothing to compile at deploy time, no npm, ~34 KB of assets, and the CSP can stay `default-src 'self'` |
| XlsxWriter, not CSV | Excel silently destroys leading zeros in a CSV — which is very likely how ~12,000 barcodes came to be damaged. Real `.xlsx` with text-typed columns cannot be mangled |
| Hand-formatted, `ruff check` enforced | `ruff format` is deliberately not used: the rate-limit tables, settings specs and design-token blocks are aligned on purpose and Black-style reflow would destroy meaning |

---

## Repository layout

```
app/
  config.py              environment settings; the ONLY place infrastructure lives
  models.py              the schema, with the reasoning for every table
  db.py                  engine, sessions, and the exclusive run lock
  core/
    barcode.py           the zero-padding rules — read this first
    settings_store.py    every client-editable setting, with its default
  vendor/
    filename.py          which day a file belongs to, and full vs delta
    ftp_client.py        FTPS/SFTP, read-only by construction
    parser.py            streaming pipe-delimited reader
  amazon/
    guard.py             refuses to transmit anything price-shaped
    lwa.py               refresh token → access token
    client.py            rate limiting, retries, and the safety gate
    reports.py           reading the All Listings Report
    listings.py          patching one SKU's quantity
    feeds.py             bulk quantity updates
  engine/
    mapping.py           barcode → confirmed Amazon SKU
    decision.py          the client's quantity rules
    guardrails.py        the circuit breakers
    pusher.py            send, then read back to confirm
    rollback.py          undo
    report_builder.py    the five .xlsx files the client's team uses
    pipeline.py          the orchestrator
  security/              crypto, stored credentials, dashboard login
  routers/               the web interface
  templates/, static/    Material Design 3, no build step

docs/                    architecture, deployment, security, operations
migrations/              Alembic, with a frozen explicit-DDL baseline
scripts/stage0_coverage.py   measure the catalogue offline
tests/                   329 tests
```

---

## Status

| | |
|---|---|
| Vendor side (fetch, parse, store, reports) | ✅ built and validated against real feeds |
| Amazon read (catalogue, mapping, coverage) | ✅ built and validated against the real report |
| Amazon write (quantity patch, bulk feed) | ✅ built — **needs the app's `Product Listing` role** |
| Dashboard, settings, audit, undo | ✅ built, 329 tests, mypy clean |

**Both original blockers are now cleared** (confirmed 7 September 2026):

1. ✅ **The `Product Listing` role is ticked.** This is the role that permits a quantity
   change; without it every push returns 403. A new refresh token was issued *after* the
   role change, which is the correct order — changing roles invalidates the old token.
2. ✅ **The LWA Client Secret has been supplied.**

**One thing to diarise:** Amazon prints a **rotation deadline of 12 February 2027** on
the LWA credentials screen. After that date the secret stops working and Amazon returns
`invalid_client`, which looks exactly like a bug. It is the only hard expiry in the
system.

**One optional tidy-up:** the `Pricing` role is still ticked. Not blocking — the
software refuses to transmit a price at the transport layer regardless — but removing it
means Amazon *also* refuses, which is a second independent lock on the promise the
client cares most about.

---

## Known limitations

Written down rather than left to be discovered. None of these blocks the staged
rollout in [docs/OPERATIONS.md](docs/OPERATIONS.md); all of them are things a reviewer
would otherwise have to find.

**It has never run against the real Amazon account.** Amazon's sandbox returns canned
data and knows nothing about these listings, so there is no way to rehearse the write
path fully without touching production. Every measurement in this README comes from the
client's real exported files, and the write path is tested against a faked transport —
but "tested" and "proven in production" are different claims and only the first is made
here. This is why the rollout starts in practice mode, then approval mode with a
whitelist of 25–50 slow-moving products, and why pressing Undo on purpose is a required
step before widening it.

**`vendor/ftp_client.py` is 23% covered.** FTPS needs a real server to exercise
meaningfully; a mock returning whatever the test wants proves only that the mock works,
and the failures that matter here (TLS session reuse on the data channel, passive mode
through NAT, servers that omit MLSD) are the ones a mock cannot produce. Covered by the
connection test on the Settings page and the deployment checklist instead.

**There are no CSRF tokens.** Protection comes from `SameSite=Lax` (which blocks
cross-site POSTs in every current browser), every mutation being POST-only, destructive
actions requiring a typed confirmation, and the dashboard having no public address. That
is layered and adequate; an explicit token would still be the next hardening step.

**The run lock is released if its database connection drops mid-run.** A PostgreSQL
advisory lock lives on its session, which is what makes it self-cleaning after a crash —
and also means a network blip releases it early. In this deployment that is theoretical:
one process, one machine, and APScheduler's `max_instances=1` plus a per-session lock
attempt covers the in-process cases. It would become real if the app were ever scaled to
two containers, which would need a lease-based lock instead.

**`amazon/feeds.py` (53%) and `amazon/reports.py` (55%) are the thinner of the tested
modules.** Both are polled asynchronous APIs — submit, wait, fetch a document — and their
happy paths are covered; the timeout and partial-result branches are not.

**Amazon's `Pricing` role is still granted to the app.** The software refuses to
transmit a price at the transport layer regardless
([`app/amazon/guard.py`](app/amazon/guard.py)), so this changes nothing about behaviour.
Removing the role would make Amazon refuse too, which is a second independent lock on
the promise the client cares most about. Doing so invalidates the refresh token and
requires re-authorisation.

---

## Licence

MIT. See [LICENSE](LICENSE).
