# Inventory Autopilot — a guide for the client

Plain English, no jargon. If a word needs explaining, it is explained.

**What this system does:** keeps the stock numbers on Amazon the same as the stock
numbers at your vendor, automatically.

**What it never does:** touch your prices. You set those yourself, because you add
shipping, tax and your margin. This is built into the software as a rule that cannot be
switched off, not as a setting somebody could click by accident.

---

## Part 1 — Words you will see

| Word | What it means |
|---|---|
| **Full feed** | The vendor's complete product list. Arrives once a day. About 1.15 million rows. |
| **Delta feed** | A small version of the same list, containing only what changed. Arrives about every five minutes. |
| **Barcode** | The number printed on the product itself. The vendor uses this. |
| **SKU** | The nickname *you* gave the product on Amazon, like `HA-AMS-0008811065126`. Amazon only understands SKUs, not barcodes. |
| **Practice mode** | The system works everything out and shows you exactly what it *would* do, but sends nothing. |
| **A run** | One cycle: check the vendor, work out what changed, decide what to do. |
| **A batch** | A group of changes sent to Amazon together. This is also what you undo. |
| **The dashboard** | A normal website with buttons and settings. This is how you control everything. |

---

## Part 2 — What was wrong, in numbers

We measured your real account on 4 September 2026, using your own files.

| What we found | Number |
|---|---|
| Products in the vendor's daily list | 1,158,340 |
| Of those, in stock at the vendor | 116,093 |
| Listings on your Amazon account | 96,010 |
| Of those, from this vendor (`HA-AMS-`) | 45,511 |
| **Products Amazon was selling that the vendor had NONE of** | **196** |
| **Quantities that did not match the vendor** | **38,341** |

Those 196 products are cancelled orders waiting to happen. Each one damages your
account health, and account health is the hardest thing to repair.

The 38,341 is the backlog that built up because the job could only be done by hand once
a day.

### One more thing we found

Your vendor sends barcodes with the zeros at the front removed. Amazon keeps them.

```
The vendor sends:   656605142524
Amazon holds it as: 0656605142524
```

Your Google Sheet already fixes this — the formula `TEXT(barcode,"0000000000000")` puts
the zeros back. **That is why your manual process works.** It is worth knowing how much
depends on it: we measured that **65.6% of your products can only be found because of
those zeros**. Without them, two thirds of your catalogue would silently never update,
and you would never see an error — because when you upload a SKU that does not exist,
Amazon reports success and then does nothing.

The new system does the same padding, and it is written down in one place with tests
around it so it cannot be lost.

---

## Part 3 — How the new system works

Eight steps, every cycle.

**1. It checks the vendor's folder.** On its own, as often as you set. It downloads only
files it has never seen before — it compares a fingerprint of the contents, so even if
the vendor re-uploads yesterday's file under a new name, it is skipped.

**2. It checks the file is not broken.** This step matters more than it sounds. A
half-finished download looks exactly like *"the vendor has sold out of everything"*.
Processing one would take your whole catalogue off sale. So the file has to open
cleanly, and a full feed has to have roughly its usual number of rows, or the run stops
and emails you.

**3. It records what the vendor has.** Plus a history, so months later you can still
ask "when did this go out of stock at the vendor?"

**4. It asks Amazon what Amazon currently shows.** Once a day it downloads your All
Listings Report. This is the important part of the design, and worth understanding:

> The obvious way to build this is to compare today's vendor file with yesterday's. That
> breaks permanently the first time an upload fails. Monday the vendor says 0, a change
> is detected, the upload fails silently. Tuesday the vendor still says 0, so comparing
> to Monday shows "no change" — and nothing is ever sent again. Amazon keeps selling a
> product with no stock, forever, and nothing records that it happened.
>
> Instead this system compares *what the quantity should be* against *what Amazon
> actually shows*. If a push fails, the difference is still there next cycle and gets
> retried. The system fixes its own mistakes.

**5. It matches barcodes to your real SKUs.** Using Amazon's own report, so the answer
comes from Amazon rather than from a guess. Anything it cannot confirm goes into a list
for a human — it is never sent on a guess.

**6. It works out the new quantity** using your rules (below).

**7. It runs the safety checks.** Details in Part 5.

**8. It sends the quantity, then checks Amazon again** to confirm the change actually
landed. Amazon can accept a batch and still quietly reject individual rows — your last
manual upload had 53 of those in 656. Without checking, you would never know.

---

## Part 4 — Your rules, as you gave them

These are all settings on the dashboard. You can change any of them yourself, at any
time, without a developer.

| You said | The setting |
|---|---|
| "it must show the current stock that is available on the vendor" | Safety margin: **0** — the vendor's number is published as-is |
| "never more than 15" | Maximum quantity: **15** |
| "when it is 0 on the vendor" | Out of stock when the vendor has: **0** |
| "set it to zero straight away but dont delete it" | Dropped product → **0 after 1 full feed**, and nothing is ever deleted |
| "every smallest or tiniest one, it must be perfectly synced" | Smallest change worth sending: **0** |
| "5000 skus instead of 2000 in one run" | Maximum changes per run: **5,000** |
| `HA-AMS-` only | Only `HA-AMS-` listings are ever touched |

### About "dropped straight away, but never deleted"

When the vendor stops carrying a product it simply disappears from the daily full feed.
The system sets its quantity to 0 — but the listing itself is never removed, from this
system or from Amazon. It keeps its reviews, its ranking and its history, and it comes
back the instant the vendor restocks.

### About the 5,000-per-run limit

You asked a good question: *"isn't this going to happen automatically from the feeds
anyway?"*

Not quite, and it is worth being clear about why. Today's delta files only contain
products that **changed at the vendor today**. Those 38,341 mismatched products have not
changed at the vendor for weeks — a product sitting at 27 in stock has nothing new to
report — so they would never appear in a delta.

Only the **daily full feed** exposes the whole backlog, and it exposes all 38,341 at
once. That is exactly the run that needs a brake. At 5,000 per run the backlog clears in
about eight runs, worst cases first, and no single run ever looks like a runaway.

### Which products go first

When a run hits its limit, the order is deliberate:

1. Products going **off sale** — these stop cancelled orders and protect account health
2. Products going **down**
3. Products going **up** — these recover sales, and can wait a cycle

Losing a sale is bad. Damaging account health is worse and takes longer to repair.

---

## Part 5 — How we keep your account safe

### Three modes

The system does exactly the same work in all three. Only the last step differs.

| Mode | What happens | When |
|---|---|---|
| **Practice** | Shows you exactly what it *would* send. Sends nothing. | Weeks 1–3. Being wrong costs nothing. |
| **Ask first** | Prepares the changes, emails you, waits for you to click Approve. | Weeks 4–5. |
| **Automatic** | Sends on its own. | Only once you are confident. |

### The safety checks, before every single send

| If this happens | It protects against | The system |
|---|---|---|
| More than 25% of your catalogue would change | a corrupt or wrong-vendor file | **stops and emails you** |
| More than 2,000 products would go off sale | the worst case — a catalogue-wide wipe | **stops and emails you** |
| A full feed has under half its usual rows | a half-downloaded file | **stops and emails you** |
| The vendor's columns have changed | stock being read from the price column | **stops and emails you** |
| The file is damaged | a file grabbed while the vendor was still uploading | **stops and emails you** |
| A quantity is above your maximum | a silly vendor number becoming a silly Amazon promise | reduces it and logs it |
| The message contains a price | the one thing you said must never happen | **refuses. Always.** |

Every threshold above is a setting you control — except the last one. "Never send a
price" is built into the code, with a final check that refuses to transmit anything
price-shaped. A rule that important should not be one careless click from being switched
off.

### The pause button

One click stops everything. No confirmation, because when you are watching something go
wrong the last thing you want is to be made to type a word. The system checks the pause
switch before every run, so a paused system stays paused even if a cycle was already
queued. Nothing is lost — the work is picked up when you resume.

### Undo

Before every send, the system records the current quantity of every product it is about
to change — taken from Amazon itself, not calculated. So:

- **Undo one batch** — one click puts those numbers back
- **Undo the last few batches** — for unwinding a bad afternoon
- **Restore to a snapshot** — put the account back to any past day's report

An undo asks you to type `UNDO` first. That friction is deliberate: it is one of only
two actions that can move thousands of listings from a standing start.

### One honest warning

Amazon does provide a sandbox for testing, but it returns made-up data and knows nothing
about your real products. It can prove the wiring works. It cannot prove the behaviour is
right.

**There is no way to fully rehearse this without touching your real account.** Anyone
who says otherwise is wrong.

So the plan is not to avoid touching it. The plan is to make the first touch small and
reversible:

- start with 25–50 slow-selling products, not your best sellers
- in "ask first" mode, so a person sees every change
- **press the undo button on purpose, before we widen it**

Nobody should trust an undo button that has never been pressed.

---

## Part 6 — What the system knows about you, and what it does not

### It holds four secrets

1. Your vendor's FTP password
2. Your Amazon app's client secret
3. Your Amazon refresh token
4. Your email password, if you want alerts

All four are encrypted. You type them in yourself, on the dashboard, and after that the
page can only ever show you the last four characters. There is no part of the software
that can show a stored password on a screen.

### It deliberately holds none of this

- ❌ Your Seller Central password
- ❌ Any card details
- ❌ Any customer names, addresses or phone numbers
- ❌ Any order or financial information

That is a design decision, not luck. An inventory system has no reason to see buyer
information, so the Amazon app is not even asked for permission to see it. The safest
way to protect data is to be unable to reach it.

### Your own emergency switch

In Seller Central: Apps and Services → Develop Apps → your app → Authorize → **Revoke**.

One click and this system is cut off from Amazon completely. Nothing else on your account
is affected. That switch belongs to you and works whether or not anyone else agrees.

---

## Part 7 — The plan

Each stage has to work before the next one starts.

| Stage | What happens | Risk to your account |
|---|---|---|
| **0** · 2–3 days | Measure the catalogue. No code touches Amazon. You get a real number for how much of your catalogue can be updated correctly. **Already done — the answer is 99.5%.** | **None** |
| **1** · week 1–2 | Collecting and reading the vendor's files, the dashboard, and the same five report files your team uses. Amazon is not touched at all. Nobody opens FileZilla again, and the files are ready every hour instead of once a day. | **None** |
| **2** · week 3 | Practice mode. Everything running, nothing sent. The dashboard says "this run would have changed 412 products" and lists them. Your team compares that against what they would have done by hand. **This is where trust is earned, and being wrong here costs nothing.** | **None** |
| **3** · week 4 | 25–50 chosen products, in "ask first" mode. Every change reviewed by a person. Undo pressed on purpose. | Limited to a small list you chose |
| **4** · week 5 | Whole catalogue, still with a person clicking Approve. The old manual process stays available throughout. | Real, but reviewed and undoable |
| **5** · week 6+ | Automatic. Only now does the cycle speed up from hourly to every 15 or 5 minutes — which is where the original problem finally disappears. | Real, guardrailed, undoable |

---

## Part 8 — What it costs to run

| | Per month |
|---|---|
| A server in America (4 GB memory) | $12–24 |
| Backups kept off the server | $2–5 |
| Web address | $0–1 |
| Alerts and monitoring | $0 |
| Amazon's API | $0 — Amazon does not charge |
| **Total** | **$15–30** |

You already have a server, so this may be $0 in new spending.

Ready-made software that does something similar costs **$100–500 per month per
account**, would not understand your vendor's file format, and usually changes prices
along with quantities — which is exactly what you do not want.

---

## Part 9 — Two things still needed before the first real update

### 1. The Amazon app is missing a permission

When we looked at your app on 5 September 2026, it had these ticked:

- ✅ Pricing
- ✅ Inventory and Order Tracking
- ⬜ Brand Analytics
- ⬜ **Product Listing** ← this one is the problem

**`Product Listing` is the permission that allows a quantity to be changed.** Without it
every update will be refused, and it would only show up at the very first attempt to
push.

`Pricing` should also be **unticked**. This system must never change a price, and the
cleanest way to guarantee that is for Amazon itself to refuse.

Step-by-step instructions: [AMAZON-APP-SETUP.md](AMAZON-APP-SETUP.md).

⚠️ **Important:** changing the roles invalidates your current refresh token. You will
need to click Authorize again afterwards and save the new one.

### 2. The Client Secret has not been supplied

Of the credentials sent over, this is the one that was missing. It is on the same Seller
Central screen as the Client ID:

**Apps and Services → Develop Apps → your app → LWA credentials → View**

Nothing can talk to Amazon without it. Please type it directly into the dashboard rather
than sending it — that way it is encrypted immediately and your developer never holds it.

---

## Questions you might reasonably ask

**Can it see my customers' details?**
No. That permission is not requested at all, so Amazon would refuse the request even if
somebody broke into the server.

**Can it change my prices?**
No, for two independent reasons. The `Pricing` permission is being removed, so Amazon
would refuse. And the software has a final check that refuses to send any message
containing a price. Both would have to fail at the same moment.

**Can it delete my listings?**
No. It only ever changes one field: the quantity. It cannot create, delete or restructure
a listing.

**What if it does something wrong?**
Every change is recorded with the old value. Any batch can be reversed with one click.
There is a pause button that stops everything immediately.

**What if the vendor's server is down?**
The run records the failure, emails you, and tries again next cycle. Nothing on Amazon
changes.

**What if the vendor sends a broken file?**
It is caught and quarantined. Two independent checks catch this: the archive has to open
cleanly, and a full feed has to have roughly its usual number of rows.

**What if I want to stop it completely?**
Revoke the app in Seller Central. One click, and it is cut off.

**Does it work while my computer is off?**
Yes. It runs on the server, not on anybody's desktop.
