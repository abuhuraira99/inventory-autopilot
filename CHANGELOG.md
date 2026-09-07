# Changelog

Notable changes, newest first. Dates are the date of the work.

This file leads with the defects rather than the features, because on a system that
writes to a live seller account the interesting history is what was wrong and how it
was found. Every entry below was found by reading the code or by writing a test for it,
not by it failing in production.

---

## [Unreleased] — 7 September 2026

Pre-deployment audit. Nothing here has run against the live Amazon account yet.

### Fixed — data loss

- **The undo trail was not durable.** A run was a single database transaction: ingest,
  parse, upsert 1.15M rows, decide, send to Amazon, verify — committed once at the end.
  There was not one `commit()` in the entire engine layer. A container killed mid-send
  rolled back `push_items`, so Amazon was changed and the record of what it had been was
  gone; those products could not be put back. There is now an explicit durability
  barrier, and a batch stranded mid-send is settled by the next run.
- **Undo silently did nothing, and reported success.** The undo planner compared
  `amazon_listings.quantity` — a cache refreshed once a day plus whatever verification
  samples, which is 100 items out of 5,000 — against the value it would restore, and
  skipped the item when they matched. For ~4,900 items in a full batch those two are the
  same value, so pressing Undo shortly after a large run skipped nearly every product
  and reported "already showing 7" for products Amazon was showing as 0. It now ranks
  the evidence per item and errs towards restoring.
- **`alembic downgrade -1` dropped every table** — and the deploy script instructed operators
  to run it after a failed release. The system's own rollback procedure destroyed the
  record of everything it had ever changed. Now guarded behind an explicit opt-in, and
  the deploy script no longer suggests it.

### Fixed — could not work at all

- **`SpApiClient` could not be constructed.** It requests HTTP/2 from httpx, which
  raises `ImportError` when `h2` is absent, and `requirements.txt` pinned plain `httpx`.
  Every SP-API path — read catalogue, patch quantity, submit feed, undo, the connection
  test — died on the first line of the constructor. No test noticed because no test had
  ever built a client. The pin is now `httpx[http2]` and the constructor negotiates
  rather than demands.

### Fixed — would have broken later

- **The initial migration built the schema from the live models** (`create_all`), so it
  described whatever the models said on the day it ran. That breaks the first time a
  second revision exists, and only on fresh databases — the worst shape a schema bug can
  take. Now frozen explicit DDL: 17 tables, 64 indexes, with a test that fails if the
  models and the migrations drift apart.
- **Nothing stopped production running on SQLite**, where the exclusive run lock
  silently degrades to a no-op — losing the only protection against two runs
  double-writing to the account. Production now refuses to start.

### Fixed — silent disk exhaustion

Three things grew without limit. A full disk stops the sync *quietly*: the download
fails, nothing can be written — including the alert about it — and Amazon carries on
showing whatever it last showed.

- Processed vendor archives were never deleted (~2.2 GB/month).
- `report_retention_days` existed, appeared on the Settings page, and its own help text
  promised that old files were deleted. No code read it.
- `vendor_product_history` was never pruned. It takes a row for every stock *and price*
  change, and the first full feed alone writes 1,158,340 of them.

All three now have retention settings, and free space is checked on every run with a
critical alert below a configurable threshold.

### Fixed — correctness and clarity

- `SFTPClient.from_transport` returns `None` rather than raising, so the next line
  failed with an `AttributeError` that told an operator nothing. *(Found by mypy.)*
- `max()` in the vendor connection test could be handed a `None` timestamp, because MLSD
  is optional and some servers omit it. *(Found by mypy.)*
- Three parser coercion helpers were annotated `str` while deliberately guarding against
  `None`, so a type checker reported those guards as dead code. The annotations were
  wrong; the guards matter, because the column indices come from a client-editable
  setting. *(Found by mypy.)*
- Each feed archive was decompressed twice per run — `testzip()` expands every byte, and
  the pipeline and the parser each called it.
- Nothing bounded how far an archive could expand. Size and compression-ratio limits are
  now checked from the central directory, *before* anything is decompressed.
- `invalid_client` from Amazon now names the Client Secret's rotation deadline as the
  first possibility. It is exactly what Amazon returns after that date, and it looks
  identical to a bug in this software.
- An exhausted network failure reaching Amazon's login service is now reported as a
  network problem rather than a raw `httpx.ConnectError`, which reads like a credential
  fault and sends people to re-check credentials that are correct.
- `systemctl restart sshd` in the deployment guide fails on Ubuntu 22.04+, where the
  unit is `ssh` — in the hardening step, immediately after disabling password logins.

### Added

- **Windows Server deployment path** (four PowerShell scripts in `scripts/`, and
  Part 2 of `docs/DEPLOYMENT.md`), because a real deployment has a Windows
  VPS that cannot be changed. The application needed no changes; verified serving on
  Windows with the full header set. What is honestly worse there — container isolation,
  RDP exposure, memory — is documented rather than glossed over.
- `scripts/setup-env.sh` / `scripts/setup-env.ps1`, so nobody hand-writes a 24-field `.env`. Both refuse
  to overwrite an existing one: `MASTER_KEY` is the only thing that can decrypt the
  stored credentials, and a second run would not reset them, it would make them
  permanently unreadable.
- `scripts/setup-db.ps1` creates the PostgreSQL role using the password already in `.env`, so
  the two cannot disagree — then logs in *as* the application role to prove it, because
  creating a role is not evidence that the app can connect.
- mypy, configured and enforced in CI, clean across all 42 modules.
- 84 tests, taking the suite from 187 to 271 and coverage from 53% to ~69%. Coverage was
  inverted with respect to risk: the well-tested modules were the pure ones, while
  `pipeline.py` sat at 11%, `pusher.py` at 20% and `rollback.py` at 22%. Four of the
  defects above were sitting in those gaps.

### Security

- Three truncated fragments of the client's real Amazon identifiers were removed from
  documentation. Neither value is a secret — the Client ID is the public half of the LWA
  pair, the Solution ID is shown to every seller during authorisation — and four
  characters authorise nothing, but there is no reason for a real client identifier to
  sit in a public repository. Verified across all commits: no credential has ever been
  committed, nor has `.env`, nor any client data.
- The `.env` written on Windows is UTF-8 **without** a BOM. `python-dotenv` treats a BOM
  as part of the first key name, so `MASTER_KEY` silently becomes `﻿MASTER_KEY` and
  the app reports the key as missing while the file looks perfectly correct on screen.

---

## [1.0.0] — 5 September 2026

Initial build. Eight-stage pipeline, Material Design 3 dashboard with no build step,
three operating modes (practice / ask first / automatic), circuit breakers, one-click
undo, and the five report files the client's team already uses.

Measured against the client's real data on 4 September 2026: 1,158,340 vendor products,
96,010 Amazon listings of which 45,511 belong to this vendor, **99.5% coverage**, and
**196 products Amazon was selling with zero vendor stock**.

See [docs/FINDINGS.md](docs/FINDINGS.md) for every measurement and how
it was taken.
