# Changelog

Notable changes, newest first. Dates are the date of the work.

This file leads with the defects rather than the features, because on a system that
writes to a live seller account the interesting history is what was wrong and how it
was found. Every entry below was found by reading the code or by writing a test for it,
not by it failing in production.

---

## [Unreleased] — 8 September 2026

Pre-deployment audit, then the first real deployment onto the Windows VPS. Nothing here
has run against the live Amazon account yet.

### Fixed — found by deploying it for real

Everything in this group was invisible until the software was installed on a machine
nobody had installed it on before. Two were defects in code that the whole test suite,
ruff and mypy all passed over; one was a gate that had never once run.

- **"Connected, but the folder contains no zip files" sent the operator to a setting that
  does not exist.** The next thing the first deployment hit, immediately after the trust
  store above. Everything typed was correct, the login genuinely succeeded, and the
  message said *check the folder path in Settings* — where there has never been a folder
  field, because the folder is `VENDOR_FTP_PATH` in `.env`. A correct connection reported
  as a dead end is worse than an error: there is nothing to search for.
  The test now asks for the subfolder names when it finds no feeds, and says which file
  and which key to change, with a worked example using a real subfolder name. If there
  are no subfolders either, it says *that* instead and points at the vendor, rather than
  inventing a folder to try. `list_directories()` is on the `VendorClient` protocol and
  both transports implement it; it is diagnostic only, is asked for solely when the
  listing is empty, and returns `[]` rather than raising, because a server that will not
  name its subfolders has still plainly connected.

- **The vendor connection could not verify the vendor's certificate, because the
  application had two different trust stores.** Amazon worked on the first attempt; FTPS
  failed every time with `CERTIFICATE_VERIFY_FAILED — unable to get local issuer
  certificate`. Nothing was wrong with the vendor, the chain or the credentials: the
  vendor's certificate is valid and its chain is complete, and it chains up through
  *Sectigo Public Server Authentication Root R46*, a root created in 2021 that a Windows
  Server 2016 certificate store has never heard of. Windows fetches missing roots on
  demand for its own TLS stack, so Chrome on that same machine loaded the site happily
  while Python refused it — a difference that makes the failure look like a vendor
  outage.
  The real defect is that the two halves of the application trusted different things.
  `httpx` builds its default context from the `certifi` bundle, which is why every Amazon
  call worked; `app/vendor/ftp_client.py` called `ssl.create_default_context()` with no
  arguments, which on Windows means "trust whatever happens to be in this machine's
  store" — a property of the machine rather than of the release. FTPS now uses the same
  `certifi` roots, so the answer to *is this certificate trusted* is identical on every
  machine the system is ever installed on, and `certifi` is pinned explicitly in
  `requirements.txt` because it is now imported by name.
  **Verification was not weakened to achieve this.** Switching verification off is the
  tempting one-line fix and would have accepted any interceptor on a live seller
  account's supply feed, silently, with the button still showing green.
  `tests/test_vendor_tls.py` asserts the roots are certifi's exactly, that
  `verify_mode`/`check_hostname` stay mandatory, and that the pin stays direct.

- **The log redaction filter destroyed the message it was protecting, and printed the
  secret anyway.** `RedactingFilter` redacted the format template and the arguments
  separately. The "names itself a secret" pattern matched `password : %s` and replaced the
  *value* part — which in a template is the placeholder — so the template came out one
  `%s` short while `record.args` still held all three arguments. Every such record then
  died inside the handler with `TypeError: not all arguments converted during string
  formatting`, **and Python's logging error path printed the unformatted message followed
  by `Arguments:` with the raw secret still in it.** The redaction did not merely fail, it
  inverted: the message was destroyed *and* the secret was published.
  It killed the single most important log line on a new install — the banner carrying the
  generated administrator password, printed exactly once, stored nowhere, unrecoverable.
  On the first real deployment the operator's only copy of that password arrived via the
  crash dump. The filter now interpolates first and redacts the finished line, so the
  patterns see values rather than placeholders, `%d` keeps working, arguments are dropped
  once folded in, and a secret is caught wherever it came from.
  **`RedactingFilter` had no test coverage at all** despite sitting on the root logger and
  on uvicorn's loggers — which is how a filter that has now broken twice in the same way
  broke twice. It has its own file, `tests/test_logging_redaction.py`, 11 tests, including
  the `%d` failure from the earlier version so that one cannot return either.
- **The administrator banner deliberately has no colons after its labels.** Interpolating
  before redacting is correct, and it means `password: <value>` in any message is stripped
  — including that banner, whose entire purpose is to display the value. The banner is now
  a module constant with the labels unpunctuated, and two tests fail if a colon is tidied
  back in, because the consequence is an operator locked out of a fresh install with no
  way to recover the password.
- **CI had never executed. Not one of 16 runs.** `.github/workflows/ci.yml` had an
  unquoted `DATABASE_URL: sqlite+pysqlite:///:memory:`. The trailing colon of `:memory:`
  makes that a YAML syntax error, so GitHub failed each run at startup with **zero jobs
  scheduled** — while the README badge showed red and every claim of the form "CI enforces
  this" was false. Quoting the value exposed the four jobs that were supposed to have been
  running all along. Fixing it paid for itself within minutes: it immediately caught the
  next item.
- **A damaged feed file aborted the whole run instead of being quarantined.**
  `verify_archive` converted only `zipfile.BadZipFile` into `FeedFormatError`, but
  `testzip()` signals damage in more ways than that — a structurally broken deflate stream
  raises `zlib.error`, a corrupted version field raises `NotImplementedError`, a corrupted
  offset raises `OSError`. Walking a byte flip across every position of a real archive,
  **75 of 295 corruptions escaped** as something other than `FeedFormatError`, which meant
  they sailed past the pipeline's per-file quarantine handler and failed the entire run as
  "failed unexpectedly" with a zlib traceback attached — instead of setting one bad file
  aside and carrying on. Reproduced on Windows/CPython 3.14, the deployment target, so
  never a Linux-only artefact. The pre-existing test flipped one byte in the middle and
  therefore tested whichever failure mode that single position happened to produce: it
  passed on Windows/3.14 and failed on Linux/3.12 for the same reason a real damaged feed
  would have, which is luck. Now converted by contract rather than by a list of types,
  because the list cannot be derived by inspection.
- **`alembic upgrade head` could not find the database outside Docker.**
  `migrations/env.py` read `DATABASE_URL` from `os.environ` and nothing loaded `.env`.
  Under Compose the URL is injected as a real environment variable, so the file worked
  there and only there. Every native deployment failed at the point where it creates its
  tables — SETUP.md Step 7 — and `scripts/deploy.ps1` would have failed identically at
  step 5 of 6 on every future release carrying a migration. Fixed in `env.py` rather than
  in the two callers, so running alembic by hand works too. Reading the URL from
  `app.config` instead was rejected: that module has a fallback default, so a missing
  setting would have quietly migrated a *different* database than intended.
- **`python-dotenv` is now pinned explicitly.** It was already installed, but only as a
  transitive dependency of `pydantic-settings`. `migrations/env.py` imports it by name,
  and this project has been bitten by that exact assumption before — `SpApiClient` could
  not be constructed at all because httpx's `h2` extra was assumed rather than pinned.

### Changed — CI tests what actually ships

- **The Python jobs now pin 3.14, not 3.12.** Nothing in this project uses 3.12: the
  Windows VPS runs 3.14.7 and so does development. The gate was proving something true
  about a version nobody deploys — and the damaged-archive bug above is precisely what
  that gap looks like, the same code and the same test giving two answers on two
  interpreters. The Docker job deliberately keeps the Dockerfile's 3.12 base image, since
  that is a genuinely different deployment target, so both versions this can be deployed
  on are now exercised. `mypy` and `ruff` stay pinned at the *oldest* supported version
  on purpose; raising them would silently narrow what the project claims to support.

### Documented — what a Windows Server 2016 deployment actually does

None of these are software faults, and all of them cost real time on the first run. They
are now written into the deployment guide rather than rediscovered:

- Chocolatey installs .NET Framework 4.8 on Server 2016 and needs a **full machine
  reboot**, not just a new shell.
- The PostgreSQL package now **generates a random `postgres` password** and prints it
  once, rather than using the published default the guide described.
- Chocolatey sits on `Installing postgresql16...` for 5–15 minutes with no output, and can
  finish the install without ever reporting it. The service being `Running` is the real
  signal.
- The guide told the operator to replace a placeholder password. Run verbatim, it sets the
  database administrator password to the literal words `PutYourOwnLongPasswordHere`. It
  now generates one instead.
- **Free disk was 5.1 GB, not the ~15 GB assumed** — .NET 4.8's backups and the Windows
  Update cache, not the application. A safe cleanup recovered 5 GB; the heavier DISM
  component-store cleanup was measured as not worth it (`Reclaimable Packages : 0`).
- Console mechanics that produce confusing errors: `.\` required to run a script, `>`
  characters copied out of indented note blocks, Ctrl+V not being paste in this console
  (fatal at a masked password prompt), `powershell` typed inside PowerShell inheriting a
  stale `PATH` so a freshly installed git looks absent, and Python 3.14's coloured
  tracebacks rendering **invisibly** on the default console — an error message that
  appears blank.

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
