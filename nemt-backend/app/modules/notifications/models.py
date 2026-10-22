"""Notification templates and log.

A template is data: subject + body keyed by (company, event, channel, language). Editing it
in the admin UI changes the message without a redeploy. Templates may carry a different
version per tenant, so one company can word "Your driver is on the way" differently than
another.

The log is append-only: every send attempt, success or failure, is recorded. Retries write
new rows so the full history survives a broker failure.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, IdMixin, TenantMixin, TimestampMixin, str_enum


class Channel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"      # e.g. no email on file, or template disabled


class NotificationTemplate(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "notification_templates"

    event: Mapped[str] = mapped_column(String(64))        # e.g. "trip.assigned.patient"
    channel: Mapped[Channel] = mapped_column(str_enum(Channel, "notification_channel"))
    language: Mapped[str] = mapped_column(String(8), default="en", server_default="en")
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_notification_templates_company_id_id"),
        UniqueConstraint("company_id", "event", "channel", "language",
                         name="uq_notification_templates_key"),
        Index("ix_notification_templates_company_event", "company_id", "event"),
    )


class NotificationLog(TenantMixin, Base):
    """Append-only: every attempt lives here, success or failure."""

    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event: Mapped[str] = mapped_column(String(64))
    channel: Mapped[Channel] = mapped_column(str_enum(Channel, "notification_log_channel"))
    recipient_user_id: Mapped[UUID | None]
    recipient_address: Mapped[str] = mapped_column(String(320))   # email, phone, or device id
    subject: Mapped[str | None] = mapped_column(String(200))
    body_preview: Mapped[str | None] = mapped_column(String(200))  # first 200 chars, for debugging
    status: Mapped[DeliveryStatus] = mapped_column(str_enum(DeliveryStatus, "notification_delivery_status"))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_notification_logs_company_created", "company_id", "created_at"),
        Index("ix_notification_logs_recipient", "company_id", "recipient_user_id"),
        CheckConstraint("attempts >= 1", name="ck_notification_logs_attempts_positive"),
    )