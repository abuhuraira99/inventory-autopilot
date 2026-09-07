# Deploying on Windows Server

The reference version of this. For a step-by-step walkthrough aimed at someone who has not
deployed before, the operator has a separate guide; this document is for a developer who
wants to know what the Windows deployment actually is and where it differs from the
supported one.

**The supported deployment is Linux + Docker** — see [DEPLOYMENT.md](DEPLOYMENT.md). This
path exists because a real deployment had a Windows VPS that could not be changed.

---

## What is the same, and what is not

The **application** is unchanged and needs no Windows-specific code. Verified directly:

```
GET /health   -> {"status":"ok","version":"1.0.0","database":"ok"}
GET /login    -> 200, renders
GET /         -> 307 to /login  (auth enforced)
```

with the full header set present — `Content-Security-Policy: default-src 'self'`,
`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`,
`Permissions-Policy`. The 255 tests also run on Windows; the suite is developed there.

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

## The scripts

Windows equivalents of the shell scripts, in the repository root:

| Script | Linux counterpart | Job |
|---|---|---|
| `setup-env.ps1` | `setup-env.sh` | generate keys, ask four questions, write `.env` |
| `setup-db.ps1` | *(none — Docker does it)* | create the PostgreSQL role and database |
| `deploy.ps1` | `deploy.sh` | backup → pull → deps → migrate → restart → health check |
| `backup.ps1` | the cron snippet in DEPLOYMENT.md | nightly `pg_dump`, zipped, 30-day retention |

Decisions inside them worth knowing:

**`setup-env.ps1`**
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

**`setup-db.ps1`**
- Reads `POSTGRES_PASSWORD` from `.env` and creates the role with it, so the two cannot
  disagree. A mismatch surfaces as `password authentication failed for user "autopilot"`,
  which does not say which of the two values is wrong.
- Idempotent: `ALTER ROLE` if the role exists, so `.env` stays the single source of truth.
- Grants `ALL ON SCHEMA public` explicitly. From PostgreSQL 15 the public schema is no
  longer writable by non-owners, which catches people out.
- Finishes by connecting **as the application role with the password from `.env`** and
  reporting `current_user`. Creating a role is not evidence that the app can log in;
  `pg_hba.conf` set to `trust` or `ident` will let the setup succeed and the app fail.

**`deploy.ps1`**
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

## Registering the service

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

## Operational differences to be aware of

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

## If Docker is preferred on the same Windows box

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
