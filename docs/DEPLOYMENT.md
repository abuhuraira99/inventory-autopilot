# Deployment

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
| **The Amazon Client Secret** | ⚠️ Not yet supplied. See [AMAZON-APP-SETUP.md](AMAZON-APP-SETUP.md). |

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
sudo systemctl restart sshd

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

If the release included a migration, `alembic downgrade -1` first — and read what it
does before running it.

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
