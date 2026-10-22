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
    actor_user_id: UUID | None = None,
    company_id: UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | UUID | None = None,
    request: Request | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the current transaction. Never put PHI in `meta`; log IDs.

    Accepts either a `User` object (`actor=`) or raw ids (`actor_user_id=`, `company_id=`)
    so callers from a service context (no request user) can still log. Prefer `actor=`.
    """
    final_actor_id = actor.id if actor is not None else actor_user_id
    final_company = actor.company_id if actor is not None else company_id
    db.add(
        AuditLog(
            company_id=final_company,
            actor_user_id=final_actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent", "")[:300] if request else None,
            metadata=meta or {},
        )
    )