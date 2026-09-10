# Working on this codebase

Read this before your first change. It is short, and most of it is about the five
things in here that are load-bearing in ways that are not obvious from the code.

---

## Getting set up

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt    # .venv\Scripts\pip on Windows

pytest                # 320 tests, no network, no live database
ruff check .          # linting; see the note below about formatting
mypy                  # configured in pyproject.toml; passes clean
```

All three run in CI on every push and pull request. The test suite touches neither
the network nor a real database — deliberately, because a test that can reach Amazon
is a test that can change a live listing.

---

## The five things you must not break

These are not style preferences. Each one has a comment in the code explaining it, and
each one exists because getting it wrong costs the client money.

### 1. Barcodes are padded to 13 digits, in exactly one place

The vendor strips leading zeros from barcodes. Amazon does not.

```
vendor sends  656605142524
  naive       HA-AMS-656605142524    ← does not exist
  padded      HA-AMS-0656605142524   ← exists
```

**29,701 of 45,273 matches (65.6%) depend on this.** Get it wrong and two thirds of the
catalogue silently stops updating — because Amazon reports *success* for a SKU that does
not exist, and then does nothing.

The rule lives in [`app/core/barcode.py`](app/core/barcode.py) and nowhere else. **No
other module may call `zfill`.** If you need padding somewhere new, call into that
module.

### 2. Quantity only. Never a price

The client sets their own prices after adding shipping and margin. Overwriting one is
the single worst thing this system could do, and the vendor's feed carries price and
stock in adjacent columns — so the mistake is one column index away at all times.

[`app/amazon/guard.py`](app/amazon/guard.py) walks every outbound payload and refuses
to transmit anything price-shaped. It is called from `SpApiClient.request`, which is the
only way anything reaches the network.

**This is not a setting and must not become one.** A promise that important should not
be one careless click from being switched off.

### 3. The durability barrier

A run has a side effect no database transaction can contain: it changes quantities on
Amazon. So the ordering rule is absolute:

> The record of what we are about to do must be durable **before** we do it.

`push_items` — one row per SKU, each holding `previous_quantity` read from Amazon — is
written and **committed** before `send_batch` sends a single byte. The barrier is marked
in [`app/engine/pipeline.py`](app/engine/pipeline.py) and explained in
[`app/db.py:checkpoint`](app/db.py).

Do not move work above that line, and do not remove the commit. Without it, a container
killed mid-send leaves Amazon changed with no record of what it had been, and those
products cannot be put back.

### 4. Decisions compare against Amazon, never against the previous feed

Storing Amazon's own reported quantity and diffing against that is what makes every run
idempotent and self-healing. Comparing against yesterday's feed breaks permanently the
first time a push fails:

```
Monday     vendor says 0 → change detected → upload → FAILS SILENTLY
Tuesday    vendor still 0 → "no change" → nothing sent
forever    Amazon keeps selling a product with no stock
```

### 5. Full feeds and delta feeds are not interchangeable

This is the distinction most likely to be broken by a well-meaning refactor, because the
two files look alike and differ only in what their *silence* means.

| | Delta feed | Full feed |
|---|---|---|
| Contains | only what changed | everything the vendor carries |
| A barcode's **absence** means | **unchanged** | **the vendor has dropped it** |
| May zero a missing product | **never** | yes, per the threshold setting |
| Triggers a full reconcile | no | yes |
| Real size | 23–320 rows | ~1,158,340 rows |

Read one as the other and you get one of two failures: dead stock left on sale forever,
or the catalogue zeroed. `FeedKind` is explicit on every `FeedFile` row for exactly this
reason, and `_mark_missing_from_full_feed` in
[`app/engine/pipeline.py`](app/engine/pipeline.py) is the only place the dropped-product
counter moves. The longer version, including why a full feed forces a reconcile, is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Conventions

**Migrations are frozen.** The initial revision is explicit DDL and must not be
regenerated. [`tests/test_migrations.py`](tests/test_migrations.py) applies the real
migrations to an empty database and compares the result against `app/models.py` column
by column, so a model change without a migration fails with a named difference. Add a
new revision; never edit an old one.

**`ruff format` is deliberately not used**, and CI does not run it. The rate-limit table
in `app/amazon/client.py`, the settings declarations in `app/core/settings_store.py`, the
design tokens in the stylesheets and the barcode-length tables in the docstrings are
aligned by hand because the alignment carries information. `ruff check` **is** enforced.
If you disagree, the conversation to have is about the aligned tables, not the setting.

**Comments explain why, not what.** The code says what it does. A comment that repeats it
is noise; a comment naming the failure a line prevents is the most valuable thing in the
file. Several comments here name a specific bug that was once present — leave those in,
they are the reason the line looks the way it does.

**Settings, not constants.** Anything the client might reasonably want to change belongs
in [`app/core/settings_store.py`](app/core/settings_store.py). Adding a spec there makes
it appear on the Settings page automatically, with validation and an audit trail. There
is no second place where behaviour is configured.

**Commit messages explain the reasoning.** They are long here on purpose: the subject
says what changed, the body says what was wrong, why the fix is shaped the way it is,
and what was considered and rejected. `git log` is the densest documentation in the
project.

---

## Testing expectations

New code that can change a live Amazon account needs a test that describes the failure
it prevents, not just that the function returns the right value. The existing tests are
written that way — read [`tests/test_pusher.py`](tests/test_pusher.py) for the house
style.

Amazon is faked at the **HTTP transport** with `httpx.MockTransport`, underneath a real
`SpApiClient`. Never fake at the client level: that would bypass the rate limiter, the
retry logic, the token refresh and the price guard, which is to say everything the
client exists to do.

Anything genuinely needing a live service is marked `@pytest.mark.integration` and
excluded by default.

CI enforces a coverage floor of 65% (currently ~69%). It is a ratchet against
regression, not a target — and *where* the coverage sits matters more than the number.
See the table in [README.md](README.md).

---

## Things known to be missing

Listed in **Known limitations** in [README.md](README.md), including the honest ones:
this has never run against the real Amazon account, `vendor/ftp_client.py` is thinly
covered and why a mock would not help, and CSRF protection rests on `SameSite=Lax`
rather than tokens.

If you are looking for a first contribution, those are better places to start than
anything cosmetic.
