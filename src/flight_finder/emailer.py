"""Sends queued emails over SMTP (e.g. Gmail with an app password). Without SMTP settings
emails stay queued in the `emails` table and show on the dashboard."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)


class Emailer:
    def __init__(self, settings: Settings, db: DB, smtp_factory=smtplib.SMTP):
        self.s, self.db, self._smtp = settings, db, smtp_factory

    @property
    def configured(self) -> bool:
        return self.s.email_configured

    def send_pending(self) -> int:
        """Send every queued email; returns how many went out."""
        if not self.configured:
            return 0
        pending = self.db.unsent_emails()
        if not pending:
            return 0
        sent = 0
        try:
            with self._smtp(self.s.smtp_host, self.s.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                if self.s.smtp_user and self.s.smtp_password:
                    smtp.login(self.s.smtp_user, self.s.smtp_password)
                for row in pending:
                    msg = EmailMessage()
                    msg["Subject"] = row["subject"]
                    msg["From"] = self.s.email_from or self.s.smtp_user or "flight-finder"
                    msg["To"] = ", ".join(self.s.email_to)
                    msg.set_content(row["body"])
                    try:
                        smtp.send_message(msg)
                        self.db.mark_email(row["id"], True)
                        sent += 1
                    except smtplib.SMTPException as e:
                        self.db.mark_email(row["id"], False, str(e)[:300])
                        log.warning("email %s failed: %s", row["id"], e)
        except (OSError, smtplib.SMTPException) as e:
            log.warning("SMTP connection failed: %s", e)  # stays queued; retried next run
        return sent
