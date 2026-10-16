from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.notifications.models import Channel, DeliveryStatus


class TemplateIn(BaseModel):
    event: str = Field(min_length=3, max_length=64)
    channel: Channel
    language: str = Field(default="en", min_length=2, max_length=8)
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    is_active: bool = True


class TemplateUpdate(BaseModel):
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=4000)
    is_active: bool | None = None


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    event: str
    channel: Channel
    language: str
    subject: str | None
    body: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class NotificationLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event: str
    channel: Channel
    recipient_user_id: UUID | None
    recipient_address: str
    subject: str | None
    body_preview: str | None
    status: DeliveryStatus
    error: str | None
    attempts: int
    context: dict
    created_at: datetime