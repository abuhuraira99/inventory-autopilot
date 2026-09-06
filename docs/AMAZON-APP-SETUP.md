# Amazon app setup

**Give this file to whoever has the Seller Central login.** Written for someone who has
never done it before. About 15 minutes.

---

## Why this matters right now

Amazon controls what an application is allowed to do through **roles**. The app for this
account already exists, but when it was inspected on 5 September 2026 it was missing the
one role that actually matters for this project.

| Role | State then | Should be |
|---|---|---|
| Pricing | ☑ ticked | ☐ **remove it** |
| Inventory and Order Tracking | ☑ ticked | ☑ keep |
| Brand Analytics | ☐ | ☐ |
| **Product Listing** | ☐ **not ticked** | ☑ **tick it** |

**`Product Listing` is the role that permits a quantity to be changed.** Without it
every update is refused — and a read-only role looks almost identical in the console, so
this only shows up at the very first attempt to push. Ten minutes now; a very unwelcome
surprise in week four.

**`Pricing` should be removed.** This system must never change a price, and the cleanest
guarantee is for Amazon itself to refuse. The software also refuses independently, but
two locks are better than one.

---

## Part 1 · Look at what is there now

Do not create a new app. Edit the existing one.

### Step 1 — Open Developer Central

1. Sign in to **Seller Central**
2. Top menu → **Apps and Services**
3. → **Develop Apps**

You should see a list of apps. The one for this project is named **`testing`**, with
Application ID `amzn1.sp.solution.00000000-0000-0000-0000-000000000000`.

> **Cannot find "Develop Apps"?** It is sometimes under **Partner Network → Develop
> Apps**, or on `solutionproviderportal.amazon.com`. If the menu looks different, take a
> screenshot of what you *do* see rather than guessing.

### Step 2 — Look at the roles

Expand the app (the arrow on the right, or **View**) and find the section listing
**Roles** — sometimes labelled *Permissions* or *Data access*.

### Step 3 — Compare

**These two must be ticked:**

| Role | What it allows | Why we need it |
|---|---|---|
| ☑ **Product Listing** | *changing* a listing, including its quantity | **the one that actually updates stock** |
| ☑ **Inventory and Order Tracking** | *reading* the inventory reports | how we learn what Amazon currently shows |

**These must NOT be ticked. Untick any that are:**

| Role | Why not |
|---|---|
| ☐ Anything mentioning **PII** or **Personally Identifiable Information** | Customer names, addresses, phone numbers. An inventory system has no reason to see any of it. The safest way to protect customer data is to be unable to reach it. |
| ☐ **Pricing** | Your team sets prices. This system must never be able to change one, so it should not even have permission to try. |
| ☐ **Finance and Accounting** | Nothing to do with stock. |
| ☐ **Buyer Communication** / **Buyer Solicitation** | We never message customers. |
| ☐ **Tax Invoicing** / **Tax Remittance** | Not our business. |
| ☐ **Direct-to-Consumer Shipping** | Not our business. |
| ☐ **Amazon Fulfillment** | Only for products Amazon stores. This account is entirely merchant-fulfilled. |
| ☐ **Brand Analytics** / **Selling Partner Insights** | Reporting features we do not use. |

### Step 4 — Screenshot it

Whatever you find, screenshot the app name and the ticked roles, and send it back. Then
follow whichever part below applies.

---

## Part 2 · Fixing the roles

**Do not delete the app.** Edit it.

### Step 1

**Develop Apps** → find the app → **Edit app**.

### Step 2

1. **Tick `Product Listing`**
2. **Untick `Pricing`**
3. Leave `Inventory and Order Tracking` ticked
4. Untick anything from the "must NOT be ticked" table
5. **Save**

### Step 3 — If Amazon asks why

Normal, not a problem. Answer honestly:

> "We are building an internal tool for our own seller account. It reads our inventory
> reports and updates the quantity on our own listings automatically, based on our
> supplier's stock feed. It does not access any customer information and does not change
> prices."

### Step 4 — ⚠️ Re-authorise. This is not optional.

**Changing the roles invalidates the existing refresh token.** The old one will keep
being accepted for a while and then fail with a confusing `invalid_grant`. Generate a new
one now — Part 3.

---

## Part 3 · The refresh token

A **refresh token** is a long-life key. The system shows it to Amazon and Amazon hands
back a short-life key, valid one hour, to do the actual work. That is safer than storing
a password anywhere.

This app is private to your own account, so you can authorise it yourself — Amazon calls
this **self-authorisation**.

### Steps

1. **Apps and Services** → **Develop Apps**
2. Find the app
3. Actions menu (usually a dropdown on the right) → **Authorize**
4. → **Authorize app**
5. Amazon shows a **refresh token**: very long, starts with `Atzr|`
6. **Copy it all and store it safely straight away.** Amazon may not show it again.

### Things worth knowing

- **It does not expire on its own.** It works until somebody revokes it or the app's
  settings change significantly.
- **Changing the roles again means repeating this.** The old token stops working.
- **You can revoke it at any time**, on the same page. That single click cuts this system
  off from Amazon completely, and nothing else on the account is affected.

That last point is your own emergency switch. It works whether or not anybody else
agrees, and it needs no developer.

---

## Part 4 · The four values, and how to send them safely

You will end up with:

| Value | Where | Looks like |
|---|---|---|
| **Client ID** | Develop Apps → your app → LWA credentials | `amzn1.application-oa2-client.4462…` |
| **Client Secret** | same screen → **View** | a long random string, **no** `amzn1.` prefix |
| **Refresh Token** | Authorize → Authorize app | `Atzr\|IwEBI…` about 400 characters |
| **Seller ID** | Settings → Account Info → Merchant Token | `A1EXAMPLESELLER` |

Of these, **the Client Secret is the one that has not been supplied**, and nothing can
talk to Amazon without it.

### 🔐 Do not send these through WhatsApp, email, chat, SMS or a screenshot

Those places keep copies forever, in places nobody controls.

### The best option: do not send them at all

Once the server is running, **type them into the dashboard yourself** — Settings →
Credentials. They are encrypted the moment they are saved, and afterwards the page can
only show the last four characters. There is no part of the software that can display a
stored secret on a screen.

This way your developer never sees them and never can. It protects you, and it protects
them.

### If one must be sent

A password manager's one-time expiring link:

- **Bitwarden Send** (free) — bitwarden.com/products/send
- **1Password**, if you already use it

Set it to expire after one view.

### Two mistakes the dashboard will catch for you

It refuses these at entry, with an explanation, rather than letting you find out later:

- **A truncated refresh token.** Amazon's are ~400 characters and it is genuinely easy to
  lose the end when copying. Anything under 200 is rejected.
- **The Client ID pasted into the Client Secret field.** Very common — they sit next to
  each other. Anything starting `amzn1.` is rejected as a secret.

---

## Part 5 · Questions you may reasonably have

**Can this system see my customers' details?**
No. That permission is not requested at all, so Amazon would refuse the request even if
somebody broke into the server.

**Can it change my prices?**
No, for two independent reasons. The `Pricing` role is being removed, so Amazon would
refuse. And the software has a final check that refuses to send any message containing a
price — it walks every outgoing message looking for one. Both would have to fail at the
same moment.

**Can it delete my listings?**
No. It only ever changes one field: the quantity. It cannot create, delete or restructure
a listing. When the vendor drops a product the quantity goes to 0 and the listing stays —
keeping its reviews, its ranking and its history.

**What if it does something wrong?**
Every change is recorded with the old value, taken from Amazon itself. Any batch can be
reversed with one click. There is a pause button that stops everything immediately.

**What if I want to stop it completely?**
**Authorize → Revoke**, on the app page. One click and it is cut off from Amazon. Nothing
else on your account is affected.

**Does Amazon charge for this?**
No. API access is free.

**Will this affect my account health?**
It should improve it. On 4 September 2026 there were **196 products Amazon was actively
selling that the vendor had none of** — each one a cancelled order waiting to happen.
That is exactly what this fixes.

---

## Part 6 · Checklist to send back

```
[ ] Found the Develop Apps page                          yes / no
[ ] App name:                                            ______________________
[ ] "Product Listing" was ticked before I changed it     yes / no
[ ] "Product Listing" is ticked NOW                      yes / no
[ ] "Pricing" is now UNTICKED                            yes / no
[ ] "Inventory and Order Tracking" is ticked             yes / no
[ ] Any PII / customer-data role ticked?                 yes / no
[ ] Screenshot of the roles page attached                yes / no
[ ] Re-authorised after changing the roles               yes / no
[ ] New refresh token saved safely (NOT sent by chat)    yes / no
[ ] Client Secret found and saved safely                 yes / no
[ ] Seller ID / Merchant Token found                     yes / no

[ ] Products are shipped by:  the seller (Merchant)  /  Amazon (FBA)  /  both
[ ] Selling in:               USA only  /  USA + Canada  /  + Mexico
```

---

## One last note

Seller Central's menus and labels change fairly often, so something here may not match
exactly what you see.

**If anything looks different from this description, do not guess — take a screenshot and
send it.** Guessing on a live account is how accidents happen, and a screenshot takes
five seconds.
