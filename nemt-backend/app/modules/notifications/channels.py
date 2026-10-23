"""Delivery channels. Each channel takes a rendered message and does one thing: try to send it.

Phase 6 ships `EmailChannel` (via Celery's `send_email` task from Phase 3) and a `NullSms` /
`NullPush` pair that log-and-skip. When you pick an SMS provider, you write one class and
register it in `get_channel`. Nothing else in the module changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RenderedMessage:
    to: str
    subject: str | None
    body: str


class Channel(Protocol):
    name: str

    def send(self, message: RenderedMessage) -> None: ...


class EmailChannel:
    """Enqueue an email task. The Celery worker does the SMTP round-trip; retries happen there."""

    name = "email"

    def send(self, message: RenderedMessage) -> None:
        from app.workers.tasks import send_email
        send_email.delay(message.to, message.subject or "", message.body)


class NullSms:
    """Placeholder until an SMS provider is chosen. Records the attempt as skipped."""

    name = "sms"

    def send(self, message: RenderedMessage) -> None:      # pragma: no cover - trivial
        log.info("SMS to %s (skipped: no provider configured): %s", message.to, message.body[:120])


class NullPush:
    """Same as NullSms, for WebPush/VAPID later."""

    name = "push"

    def send(self, message: RenderedMessage) -> None:      # pragma: no cover - trivial
        log.info("PUSH to %s (skipped: no provider configured): %s", message.to, message.body[:120])


def get_channel(name: str) -> Channel:
    return {
        "email": EmailChannel(),
        "sms": NullSms(),
        "push": NullPush(),
    }[name]