# Security

This software can change stock quantities on an Amazon account that earns the client's
living. That is the thing worth protecting, and it shapes every decision below.

---

## Threat model

Being explicit about what is being defended against, and what is not.

### What we defend against

| Threat | Realistic? | Defence |
|---|---|---|
| **A leaked database dump** — a backup copied to the wrong place, a `pg_dump` pasted into a chat, a stolen disk image | Yes. The most likely of all of these. | Credentials are AES-256-GCM ciphertext; the key lives only in the environment. A dump on its own is useless. |
| **A leaked credential in a log** — logs get tailed into tickets, pasted into chats, shipped to aggregators | Yes | Redaction filter on the root logger, so it applies to app, uvicorn and third-party output alike |
| **Somebody finding the dashboard and guessing the password** | Yes | No public login page by default (private tunnel); Argon2id; lockout with backoff; optional TOTP |
| **A price accidentally written to a live listing** | Yes — a refactor could do it | Structural refusal in `amazon/guard.py`, plus the Amazon role removed. Both must fail. |
| **A bad vendor file wiping the catalogue** | Yes — a truncated download is ordinary | Archive CRC check, row-count floor, zeroing limit, percentage limit |
| **A compromised container reaching the rest of the host** | Possible | Non-root user, read-only filesystem, all capabilities dropped, `no-new-privileges` |
| **Cross-site scripting stealing the session** | Possible | HttpOnly cookie, `default-src 'self'` CSP with no inline-script exception |
| **An open redirect used for phishing** | Possible | Only local paths honoured in `next` |

### What we do not defend against

Said plainly rather than implied:

- **A compromised server host.** Root on the box means the environment, which means the
  master key. Nothing in the application can prevent that; server hardening and access
  control are the mitigation.
- **A malicious operator.** Somebody with the dashboard password can pause the system,
  change the safety limits and send changes. Every action is attributed and recorded in
  an append-only audit trail, so it is *detectable* — but not prevented.
- **Amazon or the vendor themselves.** If the vendor publishes wrong data, the guardrails
  catch the obviously-wrong shapes; subtly wrong data will be applied.
- **A targeted attack on the crypto.** Not the threat here. The threat is a copied file.

---

## What the system holds, and what it deliberately does not

### Holds — four secrets, and nothing else

| Secret | Why | Where |
|---|---|---|
| Vendor FTP password | to download the feeds | `credentials`, encrypted |
| Amazon LWA client secret | to obtain an access token | `credentials`, encrypted |
| Amazon refresh token | same | `credentials`, encrypted |
| SMTP password | optional, for alerts | `credentials`, encrypted |

### Does not hold — by design, not by luck

- ❌ the Seller Central password
- ❌ any card details
- ❌ any customer names, addresses or phone numbers
- ❌ any order or financial data

The Amazon app is not granted the personal-data role, so **even a total compromise of
this server gives an attacker no route to buyer information** — Amazon would refuse the
request. The safest way to protect data is to be unable to reach it.

This is worth saying to the client in exactly these words. It is a genuine reduction in
their risk surface compared with handing over a Seller Central login.

---

## Credential handling

### At rest

`app/security/crypto.py`, AES-256-GCM:

- **Authenticated encryption**, so tampering is detected rather than silently producing
  garbage plaintext
- **A fresh random 96-bit nonce per value** — never reused, the one rule you must not
  break with GCM
- **`aad` bound to the key name**, so an attacker with database write access cannot move
  the FTP password ciphertext into the refresh-token row and learn anything
- **Versioned format** (`v1:nonce:ciphertext`) so it can change later without making
  existing rows unreadable

The key comes from `MASTER_KEY` in the environment. Base64, hex or raw text are all
accepted — an operator copying from a password manager should not have to know which we
wanted. Anything that is not 32 decoded bytes is stretched with SHA-256, **and warns**,
because a human-chosen passphrase has far less entropy than 32 random bytes.

### In transit

The recommended flow is that the developer never receives them:

1. The server is deployed with the password fields blank
2. The client types their own credentials into the dashboard over HTTPS
3. They are encrypted before the request finishes

If one genuinely must move between people, it moves through a password manager's
expiring one-time link. Never chat, never email, never a screenshot.

### On screen

Credentials are **write-only**. The settings page shows the last four characters and
nothing more. There is no code path in the application that returns a plaintext to a
template or a JSON response — and `tests/test_web.py::test_no_secret_is_ever_rendered`
walks every page asserting exactly that, so a refactor cannot break it silently.

Reads of the plaintext happen in exactly two places: `vendor/ftp_client.py` and
`amazon/lwa.py`, both via `credentials.get_secret`. If you want to know everywhere a
secret can be read, it is those two files.

### In logs

`RedactingFilter` is installed on the **root** logger, so it applies to application
logs, uvicorn access logs and third-party library output. It strips:

- Amazon tokens (`Atzr|…`, `Atza|…`)
- LWA client ids and application ids (`amzn1.application-oa2-client.…`, `amzn1.sp.solution.…`)
- `Bearer` and `x-amz-access-token` headers
- anything in a key/value context naming itself a password, secret or token
- FTP URLs carrying inline credentials

Being careful at every call site is necessary but not sufficient — eventually somebody
logs an exception whose message happens to contain a token. This is the net under the
tightrope.

> **A bug worth recording.** The first version of this filter called `str()` on every log
> argument in order to redact it. That turned integers into strings and made every `%d`
> format specifier in the codebase raise `TypeError` at log time — so the logging that was
> supposed to be protected stopped working entirely. It now redacts strings only.
> Non-string arguments cannot carry a secret anyway.

---

## The price invariant

The client's requirement: this system updates quantity and must never touch prices.

Their own filled-in template shows how easy the mistake would be — quantity in column C,
price in column G, adjacent:

```
A7='HA-AMS-0634457195035' | C7='1' | G7='15.99'
                             ^ours     ^emphatically not ours
```

**Three independent layers.** All three would have to fail at once.

1. **The Amazon role.** With `Pricing` unticked, Amazon itself refuses a price change.
   *(As inspected on 2026-09-05 the app still has this role — it should be removed. See
   [AMAZON-APP-SETUP.md](AMAZON-APP-SETUP.md).)*
2. **The builders.** `amazon/listings.py` and `amazon/feeds.py` construct only a
   `fulfillment_availability` patch. There is no code that can express a price.
3. **The guard.** `amazon/guard.py` walks every outbound payload and raises
   `PriceFieldRefused` on anything price-shaped — at any nesting depth, in keys or in
   attribute-path values. It is called from `amazon/client.py`, the only transport, so
   there is no path to the network that skips it.

Deliberately **not** a setting. The `never_send_price` row is marked `locked` and exists
only so nobody can create a setting that *appears* to turn it off. A promise this
important should not be one careless click from being switched off.

The substring list is deliberately broad — `price`, `pricing`, `currency`, `tax`,
`discount`, `amount`, `msrp`, `map`, `value_with_tax`… A false positive costs one
confused developer five minutes. A false negative overwrites a price the client spent
real effort calculating, and they might not notice for weeks.

---

## The dashboard

### Login

- **Argon2id**, memory-hard, 64 MB / 3 iterations. Not bcrypt, not PBKDF2, and
  emphatically not a bare SHA-256. Login happens a few times a day, so 100 ms is free and
  an offline attack on a leaked hash is expensive.
- **Transparent rehash** on sign-in when the cost parameters are raised.
- **12-character minimum**, with the reason given in the error rather than a bare rule.
- **Lockout** after 5 failures, doubling from 5 to a 60-minute cap.
- **Generic failure message.** Every failure says "Email or password is not correct."
  Distinguishing "no such account" from "wrong password" tells an attacker which
  addresses are worth attacking. A dummy hash is computed for a non-existent account so
  the timing does not reveal it either.
- **Optional TOTP.** The highest-value addition for a *shared* credential — it makes a
  leaked password insufficient. Enabling it requires a working code first, because
  switching it on blind would lock the only account out.

### Sessions

Signed with `itsdangerous`, carrying only an id, an email and a role — nothing secret.
Cookie flags: `HttpOnly` (so a cross-site script cannot read it), `Secure` in production,
`SameSite=Lax` (which blocks cross-site form posts while letting an ordinary link work).
12-hour expiry. Rotating `SESSION_SECRET` signs everybody out, which is a feature after
a suspected leak.

### Headers

```
Content-Security-Policy: default-src 'self'; script-src 'self';
    style-src 'self' 'unsafe-inline'; img-src 'self' data:;
    frame-ancestors 'none'; object-src 'none'; base-uri 'self'
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: same-origin
Strict-Transport-Security: max-age=63072000; includeSubDomains   (production)
```

The CSP can be this strict *because* the dashboard has no build step and loads no
third-party assets. That is a real security benefit of the no-npm choice, not just a
convenience one. `'unsafe-inline'` is present for styles only — a handful of inline
`style` attributes drive data-driven widths. **Scripts get no such exception.**

### Actions

Every state change is a POST. A GET that changes state can be triggered by a prefetching
browser, a link preview or a bookmark — and one of these buttons sends thousands of
quantity changes to a live account.

Friction is matched to consequence rather than applied uniformly:

| Action | Confirmation |
|---|---|
| Pause / resume | one click — pausing is never the dangerous direction |
| Run now | one click |
| Approve a batch | one click, after seeing the full list |
| Undo a batch | type `UNDO` |
| Undo the last N | type `UNDO` |

---

## The API

There is **no API route that changes anything**. Every mutation is a form POST behind a
session cookie. `tests/test_web.py::test_api_has_no_write_routes` asserts it.

An API key that could zero 45,000 listings would be a liability with no compensating
benefit — the only consumer is a page served from the same origin.

`/health` is unauthenticated for the uptime monitor and deliberately uninformative: up
or down, and the version. `/api/health/detail` names what is misconfigured, which is
exactly why *it* requires a login.

---

## Network

### To the vendor

The vendor provides **explicit FTP over TLS on port 21** — the connection opens plaintext
and is upgraded with `AUTH TLS` *before* the password is sent. That is genuinely
encrypted; it is not the same as plain FTP.

- The default TLS context verifies the certificate chain and hostname. A self-signed
  certificate fails loudly, which is correct: trusting one should be an explicit,
  documented decision.
- The data channel is also encrypted (`PROT P`), and the control session is reused for it
  because many FTPS servers require that.
- **Plain FTP is refused** unless `ALLOW_PLAINTEXT_FTP=true` is set deliberately. A
  misconfiguration must not be able to silently downgrade and leak the vendor password.
- The client is **read-only by construction** — there is no code path that can delete or
  rename anything on the vendor's server.

### To Amazon

HTTPS only, HTTP/2, certificate verification on. Rate limits enforced locally at a
fraction of Amazon's published ceilings — being a noisy neighbour on a live account is
not acceptable, and a throttled or suspended application would be self-inflicted.

### To the dashboard

Bound to `127.0.0.1` in `docker-compose.yml`. **There is no public login page by
default.** Reached through a Cloudflare Tunnel or Tailscale, optionally with Cloudflare
Access in front for an identity check before the login page is even shown.

That is a large amount of security for very little effort, and it is why the deployment
guide leads with it.

---

## The container

```yaml
read_only: true                 # only /app/data and /tmp are writable
tmpfs: [/tmp:size=512M]
security_opt: [no-new-privileges:true]
cap_drop: [ALL]
user: autopilot (uid 1001)      # never root
```

Two-stage build: the compiler and build headers live in the first stage and are not
shipped. A smaller image is also a smaller attack surface.

The database container is **not published**. Only the app container can reach it, over
the internal Docker network.

---

## Audit trail

Append-only. Nothing in the application updates or deletes a row in `audit_events`.

Recorded: every setting change (with before and after), every credential change, every
approval and rejection, every rollback, every sign-in and failed sign-in, the pause
switch, and every manual run — each with the actor, the timestamp and the IP.

With a single shared login the attribution is coarse. Knowing that an action happened at
all, and being able to see exactly what it did, is still most of the value.

**Credential changes record that they happened and never any part of the value** — not
even the last four characters. An audit log is exactly the sort of thing that gets
exported and shared.

`X-Forwarded-For` is trusted for **one hop only**, because the recommended deployment is
behind a tunnel. Trusting the whole chain would let a caller forge any address they liked
into the audit log.

---

## Testing on a live account

Said plainly because it matters more than any of the above.

Amazon's sandbox returns canned data and knows nothing about this seller's real
listings. It can prove the plumbing works; it cannot prove the behaviour is right.
**There is no way to fully rehearse without touching the live account.**

So the plan is not to avoid touching it — that option does not exist. The plan is to make
the first touch small and reversible:

1. Weeks in **practice mode**, where being wrong costs nothing
2. A whitelist of **25–50 slow-moving products**, in **ask-first** mode
3. **Press undo on purpose, before widening it.** Nobody should trust an undo button
   that has never been pressed.
4. Only then the full catalogue, still with a human approving
5. Only then automatic

Every push is reversible **by construction**: `decision.py` refuses to push a listing
whose Amazon quantity is unknown, precisely so that `push_items.previous_quantity` is
never empty and there is always something to restore.

---

## If something goes wrong

### Suspected credential leak

```bash
# 1. Stop everything — one click on the dashboard, or:
docker compose stop app

# 2. Cut Amazon off. This is the client's own switch and needs nobody's agreement:
#    Seller Central → Apps and Services → Develop Apps → Authorize → Revoke

# 3. Change the vendor's FTP password with the vendor

# 4. Rotate the master key and the session secret in .env, then re-enter all
#    credentials in the dashboard (the old ciphertext becomes unreadable, which
#    is the desired outcome)

# 5. Read the audit trail for what was done and by whom
```

### Suspected dashboard compromise

Rotate `SESSION_SECRET` and restart — every session is invalidated immediately. Then
change the password, and turn on TOTP if it was off.

### A bad batch reached Amazon

Dashboard → the batch → **Undo**. Or Runs → **Undo recent changes** for several at once.
Applied newest-first, so an older batch's "previous" value cannot overwrite a newer one
and leave the account in a state that never existed.

---

## Reporting a vulnerability

Open a private security advisory on the repository, or email the maintainer. Please do
not open a public issue for anything that could be used against a live seller account.
