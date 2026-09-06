"""
Sending alerts.

TWO PRINCIPLES
==============
1. **The dashboard is the record; email is a convenience.** Every alert is
   written to the ``notifications`` table before any attempt to send it. If the
   mail server is down, or was never configured, the operator can still see
   everything that happened. An alerting system that loses information when
   email fails is worse than none, because it creates false confidence.

2. **Do not train people to ignore alerts.** With a 15-minute cycle, mailing
   after every run would be 96 messages a day, and nobody reads 96 messages a
   day. So ``alert_on_every_run`` is off by default and only three things mail
   by default: a guardrail stopping a run, a push failing, and a batch waiting
   for approval. Everything else lands in the daily digest.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core import settings_store
from app.models import Notification, Run, RunStatus, utcnow
from app.security import credentials as creds

log = logging.getLogger(__name__)

#: Alert kinds that always mail, whatever ``alert_on_every_run`` says.
ALWAYS_MAIL = {"guardrail", "push_failure", "approval_needed", "vendor_unreachable", "auth_failure"}


def queue(
    session: Session,
    *,
    kind: str,
    severity: str,
    subject: str,
    body: str,
    run_id: int | None = None,
    recipients: list[str] | None = None,
) -> Notification:
    """
    Record an alert. Does not send -- :func:`flush_queue` does that.

    Separating recording from sending is what makes an SMTP outage harmless:
    the alert exists whether or not it can be delivered.
    """
    if recipients is None:
        cfg = settings_store.get_all(session)
        recipients = list(
            cfg.get("alert_emails_critical" if severity != "info" else "alert_emails_approval") or []
        )

    note = Notification(
        kind=kind,
        severity=severity,
        subject=subject[:300],
        body=body,
        run_id=run_id,
        recipients=recipients,
    )
    session.add(note)
    session.flush()
    return note


def flush_queue(session: Session, *, limit: int = 50) -> tuple[int, int]:
    """
    Try to send everything unsent. Returns ``(sent, failed)``.

    Called at the end of every run and by a periodic job. A failure is recorded
    on the row and retried next time; it never raises, because a mail problem
    must not be able to fail an inventory run that otherwise succeeded.
    """
    pending = list(
        session.execute(
            select(Notification)
            .where(Notification.sent.is_(False))
            .order_by(Notification.at)
            .limit(limit)
        ).scalars()
    )
    if not pending:
        return 0, 0

    if not settings.smtp_host:
        # Not an error: the system is designed to work without email. Say so
        # once rather than logging per message.
        log.debug("%d alerts waiting, but no mail server is configured", len(pending))
        return 0, 0

    sent = failed = 0
    password = creds.get_secret(session, "smtp_password") or settings.smtp_password

    try:
        with _smtp(password) as server:
            for note in pending:
                if not note.recipients:
                    # Nobody to tell. Mark it done so it does not retry
                    # forever; it is still on the dashboard.
                    note.sent = True
                    note.send_error = "no recipients configured"
                    continue
                try:
                    server.send_message(_build_message(note))
                    note.sent = True
                    note.send_error = None
                    sent += 1
                except Exception as exc:  # noqa: BLE001 - one bad address must not stop the rest
                    note.send_error = str(exc)[:500]
                    failed += 1
                    log.warning("could not email alert %d: %s", note.id, exc)
    except Exception as exc:  # noqa: BLE001
        log.error("could not connect to the mail server: %s", exc)
        for note in pending:
            note.send_error = f"mail server unreachable: {exc}"[:500]
        return 0, len(pending)

    session.flush()
    if sent or failed:
        log.info("alerts: %d sent, %d failed", sent, failed)
    return sent, failed


def _smtp(password: str) -> smtplib.SMTP:
    """Open an SMTP connection, using TLS wherever the server supports it."""
    if settings.smtp_port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        if settings.smtp_starttls:
            server.starttls()
    if settings.smtp_user and password:
        server.login(settings.smtp_user, password)
    return server


def _build_message(note: Notification) -> EmailMessage:
    """
    Compose the email.

    Plain text only. An inventory alert is read on a phone at an awkward hour,
    and plain text is legible everywhere with no rendering surprises.
    """
    msg = EmailMessage()
    tag = {"critical": "[URGENT]", "warning": "[Warning]", "info": ""}.get(note.severity, "")
    msg["Subject"] = f"{tag} {note.subject}".strip()
    msg["From"] = formataddr(("Inventory Autopilot", settings.smtp_from))
    msg["To"] = ", ".join(note.recipients or [])
    msg["Date"] = formatdate(localtime=True)

    footer = (
        "\n\n"
        "--\n"
        "Inventory Autopilot\n"
        f"Dashboard: {settings.base_url}\n"
    )
    if note.run_id:
        footer += f"This run: {settings.base_url}/runs/{note.run_id}\n"
    footer += (
        "\nThis system only ever changes stock quantities. It never changes prices.\n"
    )

    msg.set_content(note.body + footer)
    return msg


# ===========================================================================
# The daily digest
# ===========================================================================

def build_daily_digest(session: Session) -> tuple[str, str]:
    """
    Compose the daily summary. Returns ``(subject, body)``.

    Written to be read in fifteen seconds on a phone: the numbers first, then
    anything needing attention, then the detail. The client should be able to
    tell from the subject line alone whether they need to open it.
    """
    from datetime import timedelta

    since = utcnow() - timedelta(days=1)
    runs = list(
        session.execute(
            select(Run).where(Run.started_at >= since).order_by(Run.started_at)
        ).scalars()
    )

    if not runs:
        return (
            "Inventory Autopilot: nothing ran in the last 24 hours",
            "No sync runs happened in the last 24 hours.\n\n"
            "That is worth checking. Either the system is paused, or the scheduler "
            "has stopped. Open the dashboard to see which.",
        )

    pushed = sum(r.pushed_changes for r in runs)
    proposed = sum(r.proposed_changes for r in runs)
    halted = [r for r in runs if r.status is RunStatus.HALTED_BY_GUARDRAIL]
    failed = [r for r in runs if r.status is RunStatus.FAILED]
    waiting = [r for r in runs if r.status is RunStatus.AWAITING_APPROVAL]
    files = sum(r.files_processed for r in runs)
    rows = sum(r.rows_read for r in runs)

    # The subject line carries the headline, so the message does not have to
    # be opened to know whether anything is wrong.
    if failed or halted:
        subject = f"Inventory Autopilot: {len(failed) + len(halted)} run(s) need attention"
    elif waiting:
        subject = f"Inventory Autopilot: {len(waiting)} batch(es) waiting for approval"
    else:
        subject = f"Inventory Autopilot: {pushed:,} quantities updated in the last 24 hours"

    lines = [
        "Last 24 hours",
        "=" * 40,
        f"  Runs                  {len(runs)}",
        f"  Vendor files handled  {files}",
        f"  Product rows read     {rows:,}",
        f"  Changes proposed      {proposed:,}",
        f"  Changes sent          {pushed:,}",
        "",
    ]

    if failed:
        lines += ["NEEDS ATTENTION - failed runs", "-" * 40]
        for r in failed[:5]:
            lines.append(f"  Run {r.id} at {r.started_at:%H:%M}: {(r.error or '')[:180]}")
        lines.append("")

    if halted:
        lines += ["STOPPED BY A SAFETY RULE", "-" * 40]
        for r in halted[:5]:
            first = (r.guardrail_message or "").splitlines()[0] if r.guardrail_message else ""
            lines.append(f"  Run {r.id} at {r.started_at:%H:%M}: {first[:180]}")
        lines.append("")

    if waiting:
        lines += ["WAITING FOR YOUR APPROVAL", "-" * 40]
        for r in waiting[:5]:
            lines.append(
                f"  Run {r.id}: {r.proposed_changes:,} changes ready  "
                f"{settings.base_url}/runs/{r.id}"
            )
        lines.append("")

    unmapped = max((r.unmapped_count for r in runs), default=0)
    if unmapped:
        lines += [
            "FOR INFORMATION",
            "-" * 40,
            f"  {unmapped:,} products the vendor has in stock are not listed on Amazon.",
            "  These are listing opportunities, and they also appear in the",
            "  New Products report. Nothing was changed for them.",
            "",
        ]

    return subject, "\n".join(lines)


def send_daily_digest(session: Session) -> bool:
    """Queue the digest for whoever is on the summary list."""
    cfg = settings_store.get_all(session)
    recipients = list(cfg.get("alert_emails_summary") or [])
    if not recipients:
        log.debug("no summary recipients configured; skipping the digest")
        return False

    subject, body = build_daily_digest(session)
    queue(
        session,
        kind="run_summary",
        severity="info",
        subject=subject,
        body=body,
        recipients=recipients,
    )
    return True
