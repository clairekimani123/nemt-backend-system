"""Mail port + adapters. Services depend on `Mailer`, never on SMTP directly,
so swapping Mailpit -> SES/SendGrid later is a config change."""

import smtplib
from email.message import EmailMessage
from typing import ClassVar, Protocol

from app.core.config import settings


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class SmtpMailer:
    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = settings.mail_from, to, subject
        msg.set_content(body)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as s:
            s.send_message(msg)


class MemoryMailer:
    """For tests."""

    outbox: ClassVar[list[dict[str, str]]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.outbox.append({"to": to, "subject": subject, "body": body})


def get_mailer() -> Mailer:
    return MemoryMailer() if settings.mail_backend == "memory" else SmtpMailer()
