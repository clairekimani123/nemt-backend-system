from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog
from app.modules.users.models import User


async def record(
    db: AsyncSession,
    *,
    action: str,
    actor: User | None = None,
    company_id: UUID | None = None,
    resource_type: str | None = None,
    resource_id: object | None = None,
    request: Request | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the current transaction. Never put PHI in `meta`; log IDs."""
    db.add(
        AuditLog(
            company_id=company_id or (actor.company_id if actor else None),
            actor_user_id=actor.id if actor else None,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            ip=request.client.host if request and request.client else None,
            user_agent=(request.headers.get("user-agent", "")[:300] if request else None),
            meta=meta or {},
        )
    )