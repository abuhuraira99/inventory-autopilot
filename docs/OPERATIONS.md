# Operations

Running this day to day, and what to do when something looks wrong.

---

## The rollout — do not skip stages

Each stage earns the right to start the next. The temptation is to jump to automatic
because the numbers look right; the reason not to is that nobody has yet compared a
proposal against what a human would have done.

| Stage | Mode | Scope | Duration | Risk |
|---|---|---|---|---|
| **0** Measure | — | none | 2–3 days | none · **done: 99.5%** |
| **1** Ingest | practice | vendor only, Amazon untouched | 1–2 weeks | none |
| **2** Propose | practice | full catalogue, nothing sent | 1 week+ | none |
| **3** Pilot | ask first | 25–50 whitelisted products | 1 week | contained |
| **4** Full, reviewed | ask first | full catalogue | 1–2 weeks | real, reviewed |
| **5** Automatic | automatic | full catalogue | ongoing | real, guardrailed |

### Stage 3 · The pilot

The only stage with a mandatory checklist.

1. **Pick 25–50 slow-moving products.** Not the best sellers. Something going wrong on a
   slow mover is a bad afternoon; on a best seller it is a bad month.
2. Settings → **Never-touch list** → paste every *other* in-scope SKU. Easier in
   practice: set **SKU prefixes in scope** to a temporary prefix that matches only the
   pilot set, or use the never-touch list with the bulk of the catalogue.
3. Mode → **Ask first**.
4. Run, review the proposal in full, approve.
5. Check in Seller Central that the quantities changed as expected.
6. **Press Undo on purpose.** Then check Seller Central again.
7. Only when step 6 has been done, widen the scope.

> **Step 6 is not optional.** Nobody should trust an undo button that has never been
> pressed, and the moment you need it is the worst possible time to discover it does not
> work.

### Stage 4 → 5

Move to automatic when all of these are true:

- two consecutive weeks with no guardrail halts you did not expect
- every proposal you reviewed was one you would have approved
- the undo has been exercised
- alert emails are arriving and being read
- an external uptime monitor is watching `/health`

---

## The daily view

Open the dashboard. Three things answer "do I need to do anything?"

1. **Is there a red or amber band at the top?** A pause band, or a "needs setting up"
   banner.
2. **Is anything waiting for approval?** It appears at the top of the status page.
3. **"Selling with no stock"** on the main card. This should trend toward zero and stay
   there. It is the number that maps directly onto cancelled orders.

Then, once a week, glance at:

- **Runs** — a healthy pattern is mostly "Nothing to change" with occasional small
  batches. "Nothing to change" is a **good** outcome, not a null result.
- **Unmatched** — a slowly growing list is normal (the vendor adds products). A sudden
  jump means something changed.
- **Reports** — the five files, if the team still wants them.

---

## What "healthy" looks like

| | Expected |
|---|---|
| Runs per day | 24 hourly, or 96 at a 15-minute cycle |
| Most run outcomes | "Nothing to change" |
| Vendor files per day | one full feed, plus deltas |
| Full feed rows | ~1,150,000 (a swing of more than 20% is worth a look) |
| Unreadable rows per feed | ~150 (0.013%) |
| Products going off sale per day | tens to a few hundred |
| Products in stock but unlisted | ~74,000 — **normal**, these are opportunities |
| Coverage | 99%+ |

---

## When something goes wrong

### A run was stopped by a safety rule

**This is the system working.** It found something it was not willing to act on and told
you rather than proceeding.

1. Open the run. The message says exactly which rule fired and with what numbers.
2. Decide whether the data is genuinely wrong or genuinely unusual.

| Rule | Usually means | Do |
|---|---|---|
| Feed has too few rows | a truncated download, or the vendor was still uploading | nothing. It retries next cycle with the complete file. |
| Too many going to zero | a bad vendor file, **or** a genuinely large vendor sell-out | look at the products. If real, approve manually or raise the limit. |
| Too much of the catalogue changing | usually the first full-feed reconcile with a backlog | expected on day one. The change limit is the right control, not this one. |
| Columns changed | the vendor changed their format | update **Which column is which** in Settings. Do **not** assume the columns still mean what they used to. |
| Damaged archive | a file grabbed mid-upload | nothing. It retries. |

### Amazon rejected some changes

Open the run → **What Amazon rejected**, grouped by code. A repeated code is *one*
catalogue problem to fix, not many separate ones.

| Code | Meaning | Fix |
|---|---|---|
| **8684** | the SKU is linked to more than one Amazon catalogue entry (GCID) | Must be fixed in Seller Central. No quantity update will work on that SKU until it is. Affected 4 SKUs in the client's last manual upload. |
| **13013** | the product is not in Amazon's catalogue | A new-product problem, not a stock problem. Accounted for 53 of 656 in the last manual upload. |
| **8560** | identifier not matched | Amazon's own advice: UPCs 12 digits, EANs 13. Check the barcode. |
| **403** | the app is missing a role | [AMAZON-APP-SETUP.md](AMAZON-APP-SETUP.md). This is the expected first-deployment failure. |

After fixing a catalogue problem, use **Retry the N that failed** — which marks them for
the next ordinary run rather than pushing from the button, so they still go through the
full guardrail and approval path.

### Some changes "did not take effect"

The verification step read Amazon back and found a different number.

- **Occasionally, right after a push** — normal. Amazon's listing updates are eventually
  consistent. It settles.
- **Consistently for the same SKUs** — the SKU probably does not exist under that exact
  name. Check it in Seller Central; the run detail shows the SKU that was attempted.
- **Suddenly, for many SKUs** — check whether somebody is also changing quantities by
  hand or with another tool. Two systems fighting over one number is a losing game.

### The vendor is unreachable

The run records it, emails you and retries next cycle. Nothing on Amazon changes.

If it persists: Settings → **Test the vendor connection**. The message distinguishes
"wrong password" from "server unreachable" from "IP not allowed", which determines who
has to fix it.

### Amazon rejected the token (`invalid_grant`)

Almost always: **the app's roles were changed after the token was created.** Changing
roles invalidates the old token.

Fix: Seller Central → Apps and Services → Develop Apps → Authorize → **Authorize app**,
then paste the new token into Settings.

### Everything looks stale

Check, in this order:

1. Is it **paused**? (red band at the top)
2. What does the footer say for **"next check"**?
3. `docker compose logs app | tail -50`
4. `docker compose ps` — is the container up?

---

## Undo

### One batch

Run detail or batch page → **Undo** → type `UNDO`.

You see a preview first: how many products, how many go to 0, and anything that cannot be
reversed with the reason why.

### Several batches

Runs page → **Undo recent changes** → how many → type `UNDO`.

Applied **newest first**. That ordering matters: applied oldest-first, an older batch's
"previous" value would overwrite a newer one and leave the account in a state that never
existed.

### Back to a whole past day

The emergency option, when many batches have gone wrong and unwinding them one at a time
would be slower than going back to a known-good day.

Every catalogue refresh saves the report to `data/backups/catalog-*.tsv`. Those are the
snapshots. Ask your developer to run the restore — it is deliberately not a dashboard
button, because it is a large action that deserves a conversation.

### What cannot be undone

- items Amazon **rejected** — nothing happened, so there is nothing to reverse
- listings that have since been **deleted** in Seller Central
- items whose previous quantity was never recorded — this should not occur, because the
  decision engine refuses to push a listing whose Amazon quantity it does not know,
  precisely so that every push stays reversible

---

## Changing behaviour

All on the Settings page. Every change is recorded in the audit trail with who and when.

### The ones worth thinking about

**Sync interval.** Start at 60. Move to 15 once you trust it. Amazon's limits are not the
constraint — your confidence is.

**Safety margin** (currently 0). If cancelled orders start appearing, raise this to 1 or
2 before anything else. It is the single most effective protection.

**Out-of-stock threshold** (currently 0). Raising it to 1 or 2 means a product comes off
sale slightly early. That costs a few sales and prevents a lot of cancellations.

**Maximum quantity** (currently 15). The largest value seen in a real feed was 5,653.
Promising that on Amazon has risk and no upside.

**Changes per run** (currently 5,000). Lower it if a run ever feels too large to review.

**Allow quantity increases.** Turning this **off** makes the system only ever reduce or
zero a quantity. That protects account health with no chance of overselling, at the cost
of leaving sales on the table. A useful setting for a nervous week.

### Do not touch unless the vendor changed something

**Column mapping**, **delimiter**, **SKU prefix rule**. If the vendor genuinely changes
format, update the mapping — but never assume the columns still mean what they used to.
Stock read from the price column produces plausible nonsense.

---

## Routine maintenance

### Weekly

- glance at Runs for unexpected halts
- glance at Unmatched for a sudden jump
- confirm backups ran: `ls -lh data/backups/ | tail -5`

### Monthly

- `docker compose logs app | grep '"level":"ERROR"' | tail -50`
- disk: `df -h` and `du -sh data/*`
- **restore a backup into a scratch database.** See [DEPLOYMENT.md](DEPLOYMENT.md#11--an-external-uptime-check).
  A backup nobody has restored is a rumour.
- check the audit trail for anything you do not recognise

### Quarterly

- `apt update && apt upgrade`
- rotate the dashboard password
- review the SKU prefixes in scope — has the client added a supplier?
- review the alert recipients — has anyone left?

---

## Backup and restore

Nightly `pg_dump`, gzipped, into `data/backups/`, 30 days retained. Setup in
[DEPLOYMENT.md](DEPLOYMENT.md#10--backups).

**Copy them off the machine.** A backup on the same disk as the database protects against
nothing that matters.

### What matters most

| | Recoverable? |
|---|---|
| `push_items` — the undo trail | ❌ **No.** This is the one thing that cannot be rebuilt. |
| `audit_events` | ❌ No |
| settings and credentials | ⚠️ re-enterable, but the credentials need `MASTER_KEY` |
| `vendor_products` | ✅ rebuilt from the next full feed |
| `amazon_listings` | ✅ rebuilt from the next catalogue refresh |
| report files | ✅ regenerated |

### Restoring

```bash
docker compose stop app
gunzip -c data/backups/db-YYYYMMDD-HHMMSS.sql.gz | \
  docker compose exec -T db psql -U autopilot -d autopilot
docker compose start app
```

Then **check the dashboard before letting a run happen**: pause the system, refresh the
Amazon catalogue, and confirm the drift figures look sane. A restored database has an old
picture of Amazon, and acting on it would produce a confusing batch.

---

## Alerts

| Alert | Severity | Means |
|---|---|---|
| Stopped by a safety rule | critical | the system refused to act. Read the run. |
| Could not send changes | critical | Amazon or the network. Often the missing role. |
| Could not read the vendor's files | critical | vendor down, or credentials wrong |
| Could not refresh the catalogue | warning | using an older snapshot; it retries tomorrow |
| Changes need approval | info | only in ask-first mode |
| Amazon rejected N changes | warning | usually a catalogue problem on specific SKUs |
| Daily summary | info | the 24-hour picture |

`alert_on_every_run` is **off** by default. At a 15-minute cycle it would be 96 emails a
day, and nobody reads 96 emails a day — which is worse than no alerts at all.

Alerts always appear on the dashboard whether or not email is configured. Email is a
convenience; the dashboard is the record.

---

## Escalation

**Something is wrong and I do not know what.**

1. **Pause.** One click. It is never the wrong first move.
2. Screenshot the dashboard and the run detail.
3. `docker compose logs app --tail 200 > /tmp/logs.txt`
4. Send both. **Check the log for anything that looks like a credential before sending
   it** — the redaction filter should have removed them, but a two-second glance costs
   nothing.

**Amazon has suspended or warned the account.**

1. **Pause immediately.**
2. **Revoke the app** in Seller Central. That is the client's own switch and needs
   nobody's agreement.
3. Read the audit trail: what was sent, when, and by whose approval.
4. Deal with Amazon before restarting anything.

**Products are being oversold.**

1. Pause.
2. Check "Selling with no stock" on the dashboard. If it is high, the system has not been
   running or has been stopped by a guardrail.
3. Check when the Amazon catalogue was last refreshed. A stale snapshot means the
   comparison is against old numbers.
4. Once running again, raise the **out-of-stock threshold** to 1 or 2 and the **safety
   margin** to 1. Those two settings, in that order, are the effective response.
