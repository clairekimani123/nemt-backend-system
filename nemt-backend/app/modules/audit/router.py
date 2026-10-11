from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.modules.audit.models import AuditLog
from app.modules.audit.schemas import AuditOut
from app.modules.users.models import User

router = APIRouter(prefix="/audit-logs", tags=["audit"])


@router.get("", response_model=list[AuditOut])
async def list_audit_logs(
    action: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    actor: User = Depends(require_permission(Permission.AUDIT_READ)),
    db: AsyncSession = Depends(get_db),
):
    q = select(AuditLog).where(AuditLog.company_id == actor.company_id)
    if action:
        q = q.where(AuditLog.action == action)
    return (await db.scalars(q.order_by(AuditLog.id.desc()).limit(limit).offset(offset))).all()