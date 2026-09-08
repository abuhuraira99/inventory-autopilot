# Deployment

Two supported platforms. Pick one and read only that half of this document.

| | Linux + Docker | Windows Server |
|---|---|---|
| | [Part 1](#part-1--linux--docker) | [Part 2](#part-2--windows-server) |
| Supervision | Docker `restart: unless-stopped` | Scheduled Task, restarts on failure |
| Runs as | non-root `autopilot` (uid 1001) | `SYSTEM` |
| Filesystem | read-only root + tmpfs | ordinary, writable |
| Capabilities | `cap_drop: ALL`, `no-new-privileges` | not available |
| PostgreSQL | container, private Docker network | Windows service on `127.0.0.1:5432` |
| Remote access | SSH only | RDP — a materially larger exposure |
| RAM | 2 GB works, 4 GB comfortable | 4 GB minimum; Windows itself takes ~2 GB |

**Linux is the more defensible of the two**, because of the container isolation in rows
3–5: an application-level compromise there lands in a read-only, capability-stripped
container as an unprivileged user, where on Windows it lands as `SYSTEM`. Nothing in this
project is known to be exploitable that way, but the blast radius differs and the choice
should be made knowingly rather than by default.

**The application itself is identical on both.** Same code, same tests, same security
headers. What differs is everything around it.

For a step-by-step walkthrough aimed at somebody who has not deployed before, the
operator has a separate guide. This document is the reference version.

---

# Part 1 · Linux + Docker

Target: a small Linux server in the United States. 2 vCPU and 2 GB of RAM is enough;
4 GB is comfortable. Ubuntu 22.04 or 24.04 LTS.

Roughly 30 minutes end to end.

---

## Before you start

| You need | Notes |
|---|---|
| A US server with root or sudo | The client already has one. It must be in the US: the vendor may geo-restrict, and a Pakistani IP on an American seller account is an unnecessary conversation. |
| Docker and Docker Compose | Installed in step 1 |
| The vendor's FTP username | from the vendor's credentials sheet |
| The Amazon Client ID and Seller ID | Not secret; they go in `.env` |
| The Amazon Client Secret and refresh token | ✅ Supplied 7 Sep 2026. Typed into the dashboard in step 8, not into `.env`. |

You do **not** need: an AWS account, an IAM user, a role to assume, or any request
signing. Amazon removed that requirement from SP-API.

---

## 1 · Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker            # or log out and back in

docker --version
docker compose version
```

---

## 2 · Get the code

```bash
sudo mkdir -p /opt/inventory-autopilot
sudo chown $USER:$USER /opt/inventory-autopilot
cd /opt/inventory-autopilot

git clone https://github.com/abuhuraira99/inventory-autopilot.git .
```

---

## 3 · Configure

```bash
cp .env.example .env
chmod 600 .env          # only you can read it
```

Generate the three secrets:

```bash
echo "MASTER_KEY=$(python3 -c 'import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
echo "SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
echo "POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
```

Paste all three into `.env`, then fill in:

```ini
BASE_URL=https://inventory.example.com      # or leave as localhost if tunnelling

VENDOR_FTP_HOST=ftp.vendor.example.com
VENDOR_FTP_PORT=21
VENDOR_FTP_USER=<from the vendor's credentials sheet>
VENDOR_FTP_MODE=ftps                        # explicit TLS. Do NOT use plain ftp.

LWA_CLIENT_ID=amzn1.application-oa2-client.EXAMPLE00000000000000000000000000
SELLER_ID=A1EXAMPLESELLER
MARKETPLACE_ID=ATVPDKIKX0DER
```

Leave the three password fields **blank**. The client types those into the dashboard, so
they are encrypted at rest and the developer never holds them.

> ### 🔐 Back up `MASTER_KEY` now, somewhere separate from the database backups
>
> It decrypts every stored credential. Lose it and the four credentials must be
> re-entered. Store it *with* a database backup and a single leaked backup becomes
> readable — which is the whole point of keeping them apart.

---

## 4 · Start

```bash
docker compose up -d --build          # a few minutes on first build
docker compose run --rm app alembic upgrade head
docker compose ps                     # both services should be healthy
```

---

## 5 · Get the first password

Printed **once**, at first start, and never stored in plaintext:

```bash
docker compose logs app | grep -A 10 "ADMINISTRATOR ACCOUNT CREATED"
```

```
==============================================================
  ADMINISTRATOR ACCOUNT CREATED
==============================================================
  Sign in at http://localhost:8000
    email    : admin@localhost
    password : xK3nP9mQ2wR7vT4yB8zL6cH
  WRITE THIS DOWN NOW. It is not stored anywhere and cannot
  be recovered.
==============================================================
```

Write it down, sign in, change it, and switch on two-factor.

---

## 6 · Reach the dashboard

Port 8000 is bound to `127.0.0.1`, so it is not on the internet. Pick one of these.

### Option A — Cloudflare Tunnel (recommended)

No open port, no public login page, and a free TLS certificate. There is simply nothing
exposed for anyone to find.

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cf.deb
sudo dpkg -i cf.deb

cloudflared tunnel login
cloudflared tunnel create inventory-autopilot
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: <the UUID printed above>
credentials-file: /root/.cloudflared/<UUID>.json
ingress:
  - hostname: inventory.example.com
    service: http://localhost:8000
  - service: http_status:404
```

```bash
cloudflared tunnel route dns inventory-autopilot inventory.example.com
sudo cloudflared service install
```

Then add **Cloudflare Access** in the Zero Trust dashboard, restricted to the client's
email addresses. That puts an identity check in front of the login page — two
independent layers.

Set `BASE_URL=https://inventory.example.com` in `.env` and `docker compose restart app`.

### Option B — Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Reachable only from the client's own devices, over WireGuard. No DNS, no certificate,
nothing public.

### Option C — SSH tunnel (for a quick look)

```bash
ssh -L 8000:localhost:8000 user@server     # then open http://localhost:8000
```

### Option D — Caddy on a public address

Only if a public address is genuinely wanted. Change the compose port to `8000:8000`,
then:

```
inventory.example.com {
    reverse_proxy localhost:8000
}
```

Caddy obtains and renews the certificate automatically. **Never expose port 8000
directly** — the session cookie would cross the network in the clear.

---

## 7 · Harden the server

```bash
# Firewall: SSH only. With a tunnel, nothing else is needed.
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable

# Keys only, no password login
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl restart ssh    # on Ubuntu 22.04+ the unit is 'ssh', not 'sshd'

# Automatic security updates
sudo apt install -y unattended-upgrades fail2ban
sudo dpkg-reconfigure --priority=low unattended-upgrades
```

---

## 8 · Enter the credentials

Sign in → **Settings** → **Credentials**. Three fields:

| Field | Where it comes from |
|---|---|
| Vendor FTP password | the vendor's credentials sheet |
| Amazon Client Secret | Seller Central → Apps and Services → Develop Apps → your app → LWA credentials → View |
| Amazon Refresh Token | same page → Authorize → Authorize app |

Each is encrypted the moment it is saved. Afterwards the page shows only the last four
characters, and there is no code path that returns a stored secret to a browser.

Then press **Test the vendor connection** and **Test Amazon**. Both answer in plain
language.

> The client should type these in themselves. That way the developer never receives them
> and never can — which protects both parties.

---

## 9 · First run, in practice mode

The system starts in **practice mode**. Leave it there.

1. **Refresh Amazon catalogue** — takes a few minutes; Amazon builds the report
   asynchronously
2. **Check now** — downloads the vendor's files
3. Open the run and read what it *would* have done

Nothing has been sent. Compare the proposal against what the team would have done by
hand, for as many days as it takes to be convinced.

---

## 10 · Backups

The database holds the undo trail. It is the one thing here that cannot be rebuilt from
the vendor's files or from Amazon.

```bash
sudo tee /usr/local/bin/autopilot-backup >/dev/null <<'EOF'
#!/bin/bash
set -euo pipefail
cd /opt/inventory-autopilot
STAMP=$(date -u +%Y%m%d-%H%M%S)
OUT="./data/backups/db-${STAMP}.sql.gz"
docker compose exec -T db pg_dump -U autopilot autopilot | gzip > "$OUT"
chmod 600 "$OUT"
find ./data/backups -name 'db-*.sql.gz' -mtime +30 -delete
echo "$(date -uIs) backup ok: $OUT ($(du -h "$OUT" | cut -f1))"
EOF
sudo chmod +x /usr/local/bin/autopilot-backup

# nightly at 02:15 UTC
( crontab -l 2>/dev/null; echo "15 2 * * * /usr/local/bin/autopilot-backup >> /var/log/autopilot-backup.log 2>&1" ) | crontab -
```

**Copy them off the machine.** A backup on the same disk as the database protects
against nothing that matters:

```bash
rclone sync ./data/backups remote:autopilot-backups
```

### Test a restore. A backup nobody has restored is a rumour.

```bash
gunzip -c data/backups/db-YYYYMMDD-HHMMSS.sql.gz | \
  docker compose exec -T db psql -U autopilot -d postgres \
    -c "CREATE DATABASE restore_test;" -d restore_test
docker compose exec -T db psql -U autopilot -d restore_test -c "SELECT count(*) FROM push_items;"
docker compose exec -T db psql -U autopilot -d postgres -c "DROP DATABASE restore_test;"
```

---

## 11 · An external uptime check

The system emails you when a run fails. It cannot email you when the whole machine is
down, so something outside has to watch.

Point a free monitor — Healthchecks.io, UptimeRobot, BetterStack — at:

```
https://inventory.example.com/health
```

That endpoint is unauthenticated on purpose and deliberately uninformative: it says
whether the service is up and nothing about the account or the data.

---

## Updating

**Pushing to GitHub does NOT change the server.** Git is a pull system: nothing reaches
the VPS until somebody tells it to fetch. That is deliberate — an automatic deploy on a
system that writes to a live Amazon account means a bad commit reaches production with
nobody in between.

Use the script in the repository:

```bash
cd /opt/inventory-autopilot
./scripts/deploy.sh
```

It backs up the database FIRST, pulls, rebuilds, applies migrations, restarts, and waits
for the health check. If the health check fails it prints the exact commands to roll
back. It also refuses to run if there are uncommitted changes on the server, rather than
overwriting them.

By hand, if you prefer:

```bash
cd /opt/inventory-autopilot
/usr/local/bin/autopilot-backup                 # always first

git pull
docker compose build
docker compose run --rm app alembic upgrade head
docker compose up -d
docker compose logs -f app
```

### Rolling back

```bash
git log --oneline -10
git checkout <previous-commit>
docker compose up -d --build
```

**Do not run `alembic downgrade`.** Most releases add no migration, and a newer schema
is harmless to older code here — added columns are nullable or defaulted. Downgrading the
*first* revision drops every table, including `push_items`, which is the undo trail and
the one thing in this database that cannot be rebuilt from the vendor's files or from
Amazon. That command is guarded and will refuse unless an explicit opt-in environment
variable is set.

If a release genuinely needs its schema change undone, restore the backup `scripts/deploy.sh`
takes before every deployment: it was taken before the migration ran, so it is by
definition the schema the old code expects.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Refusing to start with a broken configuration` | `MASTER_KEY` or `SESSION_SECRET` missing. The log names which. |
| `The database is not reachable` | The db container is still starting. `docker compose logs db`. It retries. |
| `Amazon rejected the refresh token (invalid_grant)` | Almost always: the app's roles were changed after the token was made. Re-authorise and save the new token. |
| `403 Forbidden` on a push | The app is missing the `Product Listing` role. See [AMAZON-APP-SETUP.md](AMAZON-APP-SETUP.md). |
| `The vendor rejected the login` | Check the username and password. If correct, ask the vendor whether the account is active or IP-restricted. |
| `could not decrypt credential` | `MASTER_KEY` has changed since the credential was saved. Re-enter it in Settings. |
| Dashboard loads but the tiles never update | Session expired, or `/api/status` is blocked. Check the browser console. |
| No runs happening | Paused? `ENABLE_SCHEDULER=false`? Check the footer's "next check" time. |
| Disk filling up | Report files. Lower "Keep report files for" in Settings; the hourly housekeeping job prunes them. |

### Useful commands

```bash
docker compose logs -f app                    # follow
docker compose logs app | grep '"level":"ERROR"'
docker compose exec db psql -U autopilot -d autopilot
docker compose exec app python -c "from app.db import healthcheck; print(healthcheck())"
docker stats --no-stream
```

---

## Running without Docker

Docker is recommended, but a plain install works.

```bash
sudo apt install -y python3.12 python3.12-venv postgresql-16
sudo -u postgres createuser autopilot --pwprompt
sudo -u postgres createdb autopilot -O autopilot

cd /opt/inventory-autopilot
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head
```

`/etc/systemd/system/autopilot.service`:

```ini
[Unit]
Description=Inventory Autopilot
After=network.target postgresql.service

[Service]
Type=simple
User=autopilot
WorkingDirectory=/opt/inventory-autopilot
EnvironmentFile=/opt/inventory-autopilot/.env
ExecStart=/opt/inventory-autopilot/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=10

# Same hardening the container gets
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/inventory-autopilot/data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now autopilot
sudo journalctl -u autopilot -f
```

**One worker, deliberately.** The scheduler runs inside the web process and exactly one
process must own it. Two workers would mean two schedulers — the advisory lock would stop
them colliding, but half the fires would be wasted and the dashboard's "next run" would
flip between two answers. To scale the dashboard, run extra instances with
`ENABLE_SCHEDULER=false`.

---

# Part 2 · Windows Server

### Field notes from the first real deployment

This path has now been walked end to end on a fresh Windows Server 2016 VPS (4 GB RAM,
2 vCPU, 30 GB disk). Read this before you start: none of it is a fault in the
application, and all of it costs an hour if you meet it cold.

**Reboot after installing Chocolatey.** Server 2016 ships without .NET Framework 4.8, so
Chocolatey installs it and cannot finish until Windows restarts. It says so, and it means
the machine, not the shell: `You need to restart this machine prior to using choco`.

**PostgreSQL installs silently for 5–15 minutes, and may never report finishing.**
Chocolatey sits on `Installing postgresql16...` with a blinking cursor. Do not assume it
has hung, and do not Ctrl+C on a guess — ask the system instead:

```powershell
Get-Service postgresql*                        # Running = it is already done
Get-Process *postgres* | Select Name, Id, CPU   # several processes at low CPU = healthy idle DB
```

Once the service is `Running`, Chocolatey is only failing to *report*, and Ctrl+C is safe.

**The `postgres` password is now randomly generated and printed once.** Recent versions of
the package print `WARNING: Generated password: …` rather than using a published default.
Capture it before the window scrolls. Then replace it with one you generated — and
generate it, rather than pasting a placeholder out of a document, which is a mistake that
runs perfectly happily and leaves the database administrator password set to a phrase
printed in a guide.

**Budget disk space properly.** A 30 GB disk left **5.1 GB** free once Windows, .NET 4.8,
PostgreSQL, Python and git were on it — against a plan that assumed about 15 GB. The
consumer is not the application: .NET 4.8 keeps a full backup of every file it replaced,
and Windows Update hoards its downloads. Clearing the update cache, the temp folders and
the recycle bin recovered **5 GB** in seconds and needed no reboot. The heavier DISM
component-store cleanup was measured and rejected — `AnalyzeComponentStore` reported
`Number of Reclaimable Packages : 0`, so it would have freed under a gigabyte in exchange
for permanently losing the ability to uninstall a Windows update. Check that number before
spending 30 minutes on it. This system needs 3–5 GB in normal operation and warns below
2 GB free; a full disk stops the sync *quietly*, so do not run it near the edge.

**Console mechanics that generate misleading errors.** Each of these looks like a broken
install and is not:

| Symptom | Cause |
|---|---|
| `is not recognized as the name of a cmdlet` on a script | missing `.\` prefix — PowerShell will not run a script from the current folder implicitly |
| `The ampersand (&) character is not allowed` | `>` characters copied out of an indented Markdown note block |
| a masked password prompt accepts one asterisk, then `authentication failed` | Ctrl+V is not paste in this console. **Right-click** is |
| a freshly installed `git` reports "not recognized" | the shell was started before the install, or `powershell` was typed *inside* PowerShell, inheriting a stale `PATH`. Close the window; do not reinstall |
| an error message appears **completely blank** | Python 3.14 colours tracebacks and some of that colour is unreadable on the default console. Set `$env:PYTHON_COLORS='0'` |
| a paste leaves a `>>` prompt and nothing runs | a quote mark did not survive the paste. Ctrl+C and type it |

**Two code defects were found only by doing this**, both now fixed: `alembic upgrade head`
could not locate the database outside Docker (`migrations/env.py` never loaded `.env`,
which also broke `scripts/deploy.ps1`), and a damaged feed archive aborted the whole run
instead of being quarantined. See [CHANGELOG.md](../CHANGELOG.md). If your checkout
predates `a9ee8d3`, the migration step will fail with `DATABASE_URL is not set`.

### What is the same, and what is not

The **application** is unchanged and needs no Windows-specific code. Verified directly:

```
GET /health   -> {"status":"ok","version":"1.0.0","database":"ok"}
GET /login    -> 200, renders
GET /         -> 307 to /login  (auth enforced)
```

with the full header set present — `Content-Security-Policy: default-src 'self'`,
`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`,
`Permissions-Policy`. The 273 tests also run on Windows; the suite is developed there.

There are no POSIX-only calls in `app/` — no `os.fork`, `pwd`, `grp`, `fcntl`, `resource`,
`signal.SIGKILL`, and no hardcoded absolute paths. Paths are `pathlib` throughout and
derive from `Settings.data_dir`.

What differs is the **deployment**:

| | Linux (supported) | Windows |
|---|---|---|
| Process supervision | Docker `restart: unless-stopped` | Scheduled Task, `-AtStartup`, restart every 1 min, no time limit |
| Runs as | non-root `autopilot` (uid 1001) | `SYSTEM` |
| Filesystem | `read_only: true` + tmpfs | ordinary filesystem, writable |
| Capabilities | `cap_drop: ALL`, `no-new-privileges` | none of this exists |
| PostgreSQL | container, private Docker network | Windows service on `127.0.0.1:5432` |
| Remote access | SSH only | RDP (3389) — a materially larger exposure |
| Memory floor | ~2 GB total | ~4 GB, comfortably 6 GB |

**The honest summary:** the application is equally safe; the *container isolation* is what
is lost. An application-level compromise on Linux lands in a read-only, capability-stripped
container as an unprivileged user. On Windows it lands as `SYSTEM`. Nothing in this project
is known to be exploitable that way, but the blast radius differs and it should be stated
rather than glossed over.

---

### The scripts

Windows equivalents of the shell scripts, in `scripts/`:

| Script | Linux counterpart | Job |
|---|---|---|
| `scripts/setup-env.ps1` | `scripts/setup-env.sh` | generate keys, ask four questions, write `.env` |
| `scripts/setup-db.ps1` | *(none — Docker does it)* | create the PostgreSQL role and database |
| `scripts/deploy.ps1` | `scripts/deploy.sh` | backup → pull → deps → migrate → restart → health check |
| `scripts/backup.ps1` | the cron snippet in DEPLOYMENT.md | nightly `pg_dump`, zipped, 30-day retention |

Decisions inside them worth knowing:

**`scripts/setup-env.ps1`**
- `RandomNumberGenerator`, not `Get-Random`. The latter is a seeded PRNG for sampling and
  is not key material.
- Writes UTF-8 **without a BOM**, via `UTF8Encoding($false)`. `python-dotenv` treats a BOM
  as part of the first key name, so `MASTER_KEY` silently becomes `\ufeffMASTER_KEY` and
  the app reports the key as missing while the file looks perfectly correct on screen.
  This is asserted by inspecting the first three bytes after generation.
- `POSTGRES_PASSWORD` is hex, because it is substituted into `DATABASE_URL` and a password
  containing `:` `/` `@` or `?` yields a URL that parses wrongly rather than failing.
- ACL is reset to SYSTEM + Administrators + the invoking user, inheritance disabled. The
  third entry is deliberate: without it, reading your own `MASTER_KEY` needs an elevated
  shell every time, and the predictable outcome is somebody running
  `icacls /grant Everyone:F` to stop the nuisance.
- **Refuses to overwrite an existing `.env`.** `MASTER_KEY` is the only thing that can
  decrypt the stored credentials; a second run does not reset them, it makes them
  permanently unreadable.
- Rejects `amzn1.sp.solution.*` (Application ID) and `amzn1.oa2-cs.*` (Client Secret) in
  the Client ID field, and writes nothing when it does.

**`scripts/setup-db.ps1`**
- Reads `POSTGRES_PASSWORD` from `.env` and creates the role with it, so the two cannot
  disagree. A mismatch surfaces as `password authentication failed for user "autopilot"`,
  which does not say which of the two values is wrong.
- Idempotent: `ALTER ROLE` if the role exists, so `.env` stays the single source of truth.
- Grants `ALL ON SCHEMA public` explicitly. From PostgreSQL 15 the public schema is no
  longer writable by non-owners, which catches people out.
- Finishes by connecting **as the application role with the password from `.env`** and
  reporting `current_user`. Creating a role is not evidence that the app can log in;
  `pg_hba.conf` set to `trust` or `ident` will let the setup succeed and the app fail.

**`scripts/deploy.ps1`**
- Backs up **first**, and **refuses to deploy if `pg_dump` cannot be found** rather than
  proceeding without one. The database holds `push_items`, the only unrebuildable data.
- Locates `pg_dump`/`psql` under `C:\Program Files\PostgreSQL\*\bin` because the installer
  does not add itself to `PATH`.
- Refuses on a dirty working tree; `git merge --ff-only`, never a merge commit.
- If the migration fails it stops **before** restarting, so the old code keeps running
  against the old schema.
- On health-check failure it prints the rollback commands, and explicitly says **not** to
  run `alembic downgrade` — downgrading the initial revision drops every table, and is
  guarded for that reason.

---

### Registering the service

There is no third-party dependency here on purpose. `NSSM` is the usual answer and works
well, but it is an unvetted binary on a box holding a live seller account's credentials,
and Task Scheduler is sufficient:

```powershell
$py     = "C:\apps\inventory-autopilot\.venv\Scripts\python.exe"
$action = New-ScheduledTaskAction -Execute $py `
          -Argument "-m uvicorn app.main:app --host 127.0.0.1 --port 8000" `
          -WorkingDirectory "C:\apps\inventory-autopilot"
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 `
            -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "InventoryAutopilot" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force
```

`-ExecutionTimeLimit ([TimeSpan]::Zero)` matters: the default is 72 hours, after which
Task Scheduler would kill a long-running service. `-RestartCount 999` with a one-minute
interval is the closest equivalent to `restart: unless-stopped`.

**Verify by rebooting**, not by starting the task. A task that starts on demand but has a
misconfigured trigger looks identical until the first unplanned reboot.

---

### Operational differences to be aware of

**Logs.** `uvicorn` writes to stdout, which a scheduled task discards. The dashboard is
the primary record — `Runs` explains every cycle, `Audit` records every consequential
action — but there is no equivalent of `docker compose logs` for lower-level detail. To
watch it live, stop the task and run the uvicorn command in a shell. Redirecting to a file
with rotation would be a reasonable improvement.

**The run lock still works.** It is a PostgreSQL advisory lock, so it is unaffected by the
platform. `Settings.startup_problems()` refuses to boot production on SQLite, where that
lock silently degrades to a no-op — that check matters more here, because SQLite is an
easier mistake to make without Compose setting `DATABASE_URL` for you.

**Port 8000 must never be opened.** The dashboard is bound to `127.0.0.1` and reached from
the server's own browser over RDP. Because there is no tunnel step to prompt the question,
it is worth checking explicitly:

```powershell
Get-NetFirewallRule -Enabled True -Direction Inbound |
  Where-Object { ($_ | Get-NetFirewallPortFilter).LocalPort -eq 8000 }
```

**RDP is the real exposure.** Restricting 3389 to a known source address is the single
highest-value hardening step on a Windows deployment, and it has no Linux counterpart in
this project because SSH was the only listening service there.

---

### If Docker is preferred on the same Windows box

Possible, but check first — it needs nested virtualisation, which many VPS providers do not
enable:

```powershell
systeminfo | findstr /i "hyper-v"
```

Docker Desktop is not supported on Windows Server. The workable route is WSL2 with an
Ubuntu distribution and Docker Engine installed inside it, which then runs
`docker-compose.yml` unchanged and restores the container hardening. The cost is another
layer to keep running across reboots (WSL does not start services at boot without a
scheduled task) and the memory of a second kernel on a box that is already short of it.

If the goal is the hardened configuration, a small separate Ubuntu VPS is less work and
less to go wrong than WSL2 on Windows.
