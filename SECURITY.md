# Security policy

## Reporting a vulnerability

Please do **not** open a public issue.

Email the maintainer directly with the details, and allow a reasonable period for a fix
before any disclosure. This software holds credentials for a live Amazon seller account,
so a public report is a public exploit.

If it helps, include: what you can reach, what you can do with it, and whether it needs
an authenticated dashboard session.

## What this software protects, and how

The threat model is documented in full in [docs/SECURITY.md](docs/SECURITY.md). The short
version:

| | |
|---|---|
| The two Amazon secrets and the vendor password | AES-256-GCM at rest under `MASTER_KEY`, entered through the dashboard, never written to `.env`, never rendered back to a page |
| `MASTER_KEY` and `SESSION_SECRET` | in `.env`, permissions restricted to the service account, never committed |
| The dashboard | bound to `127.0.0.1`. There is no public login page to attack |
| Login | Argon2id, lockout with exponential backoff, optional TOTP |
| The container | non-root, read-only root filesystem, all capabilities dropped |
| Outbound payloads | every one is walked and refused if it contains anything price-shaped |

## Things deliberately absent

- **No CI secrets.** The workflow cannot authenticate to Amazon or to the vendor. A
  pipeline that could reach a live seller account is a pipeline that could change a live
  listing.
- **No automatic deployment.** Pushing to `main` changes no server. Somebody runs a
  deploy script, deliberately.
- **No price capability.** Not as a setting — as a structural refusal in the transport.

## Known gaps

Stated openly in **Known limitations** in [README.md](README.md), including that CSRF
protection rests on `SameSite=Lax` rather than tokens, and that the advisory run lock is
released if its database connection drops.

## Supported versions

The `main` branch. This is a single-deployment system rather than a distributed product,
so there are no maintained older releases.
