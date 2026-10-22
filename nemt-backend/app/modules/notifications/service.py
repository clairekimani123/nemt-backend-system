"""The notification engine: look up a template, render it, dispatch, log.

Called from anywhere that wants to tell a person something. Failing to send must never
break the caller, so every exception is caught and logged as a failed delivery.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.channels import RenderedMessage, get_channel
from app.modules.notifications.models import (
    Channel,
    DeliveryStatus,
    NotificationLog,
    NotificationTemplate,
)

log = logging.getLogger(__name__)

# {{placeholder}} — conservative: no expressions, no filters, no code execution
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def render(text: str, context: dict[str, Any]) -> str:
    """Replace {{key}} with context[key]. Missing keys become empty strings; that's deliberate —
    a missing optional field (e.g. a companion name) should not crash a delivery."""
    return _PLACEHOLDER.sub(lambda m: str(context.get(m.group(1), "")), text)


async def _template(db: AsyncSession, company_id: UUID, event: str, channel: Channel,
                    language: str) -> NotificationTemplate | None:
    return await db.scalar(select(NotificationTemplate).where(
        NotificationTemplate.company_id == company_id,
        NotificationTemplate.event == event,
        NotificationTemplate.channel == channel,
        NotificationTemplate.language == language,
        NotificationTemplate.is_active.is_(True),
    ))


def _log(db: AsyncSession, *, company_id: UUID, event: str, channel: str, recipient_user_id: UUID | None,
         recipient_address: str, subject: str | None, body: str, status: DeliveryStatus,
         error: str | None = None, attempts: int = 1, context: dict) -> None:
    db.add(NotificationLog(
        company_id=company_id, event=event, channel=channel,
        recipient_user_id=recipient_user_id, recipient_address=recipient_address,
        subject=subject, body_preview=body[:200] if body else None,
        status=status, error=error, attempts=attempts, context=context,
    ))


async def notify(
    db: AsyncSession,
    *,
    company_id: UUID,
    event: str,
    channel: Channel,
    to: str,
    context: dict[str, Any],
    recipient_user_id: UUID | None = None,
    language: str = "en",
) -> None:
    """Send one notification. Never raises on delivery failure — writes a log row instead.

    The caller is responsible for `await db.commit()`; we don't commit inside because the
    caller usually wants the log to be in the same transaction as the domain change.
    """
    template = await _template(db, company_id, event, channel, language)
    if template is None:
        _log(db, company_id=company_id, event=event, channel=channel.value,
             recipient_user_id=recipient_user_id, recipient_address=to,
             subject=None, body="", status=DeliveryStatus.SKIPPED,
             error="no active template", context=context)
        return

    subject = render(template.subject, context) if template.subject else None
    body = render(template.body, context)

    try:
        get_channel(channel.value).send(RenderedMessage(to=to, subject=subject, body=body))
        status = DeliveryStatus.SENT
        error = None
    except Exception as exc:                                # pragma: no cover - defensive
        log.exception("notify failed: event=%s to=%s", event, to)
        status = DeliveryStatus.FAILED
        error = str(exc)[:500]

    _log(db, company_id=company_id, event=event, channel=channel.value,
         recipient_user_id=recipient_user_id, recipient_address=to,
         subject=subject, body=body, status=status, error=error, context=context)