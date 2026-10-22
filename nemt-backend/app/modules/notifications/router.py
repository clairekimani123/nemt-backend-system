"""Admin endpoints to read and edit templates, and to inspect the log."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud import get_or_404
from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.modules.audit import service as audit
from app.modules.notifications.models import Channel, NotificationLog, NotificationTemplate
from app.modules.notifications.schemas import (
    NotificationLogOut,
    TemplateIn,
    TemplateOut,
    TemplateUpdate,
)
from app.modules.users.models import User

router = APIRouter(prefix="/notifications", tags=["notifications"])
READ = Depends(require_permission(Permission.SETTINGS_WRITE))   # owner-level config
WRITE = Depends(require_permission(Permission.SETTINGS_WRITE))


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(event: str | None = None, channel: Channel | None = None,
                         actor: User = READ, db: AsyncSession = Depends(get_db)):
    stmt = select(NotificationTemplate).where(NotificationTemplate.company_id == actor.company_id)
    if event:
        stmt = stmt.where(NotificationTemplate.event == event)
    if channel:
        stmt = stmt.where(NotificationTemplate.channel == channel)
    return (await db.scalars(stmt.order_by(NotificationTemplate.event,
                                            NotificationTemplate.channel))).all()


@router.post("/templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(body: TemplateIn, request: Request, actor: User = WRITE,
                          db: AsyncSession = Depends(get_db)):
    t = NotificationTemplate(
        company_id=actor.company_id, event=body.event, channel=body.channel,
        language=body.language, subject=body.subject, body=body.body, is_active=body.is_active,
    )
    db.add(t)
    try:
        await db.flush()
    except Exception:
        await db.rollback()
        # UNIQUE(company_id, event, channel, language) is the only realistic conflict
        from fastapi import HTTPException
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "A template for that event/channel/language already exists") from None
    await audit.record(db, action="notification.template_created", actor=actor,
                       resource_type="notification_template", resource_id=t.id, request=request)
    await db.commit()
    await db.refresh(t) 
    return t


@router.patch("/templates/{template_id}", response_model=TemplateOut)
async def update_template(template_id: UUID, body: TemplateUpdate, request: Request,
                          actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    t = await get_or_404(db, NotificationTemplate, template_id, actor.company_id, "Template")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(t, k, v)
    await audit.record(db, action="notification.template_updated", actor=actor,
                       resource_type="notification_template", resource_id=t.id, request=request)
    await db.commit()
    await db.refresh(t) 
    return t


@router.get("/log", response_model=list[NotificationLogOut])
async def list_log(
    event: str | None = None,
    recipient_user_id: UUID | None = None,
    since_minutes: int = Query(60, ge=1, le=10_080),
    limit: int = Query(100, ge=1, le=500),
    actor: User = READ, db: AsyncSession = Depends(get_db),
):
    cutoff = datetime.now(UTC) - timedelta(minutes=since_minutes)
    stmt = select(NotificationLog).where(NotificationLog.company_id == actor.company_id,
                                         NotificationLog.created_at >= cutoff)
    if event:
        stmt = stmt.where(NotificationLog.event == event)
    if recipient_user_id:
        stmt = stmt.where(NotificationLog.recipient_user_id == recipient_user_id)
    return (await db.scalars(stmt.order_by(NotificationLog.id.desc()).limit(limit))).all()