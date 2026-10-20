from typing import Any
from uuid import UUID

from fastapi import HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def get_or_404(db: AsyncSession, model: Any, id_: UUID, company_id: UUID, what: str = "Resource") -> Any:
    """Tenant-scoped fetch. Another tenant's id is indistinguishable from a missing one."""
    obj = await db.scalar(select(model).where(model.id == id_, model.company_id == company_id))
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")
    return obj


async def flush_or_409(db: AsyncSession, detail: str) -> None:
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from None


class Page:
    def __init__(self, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        self.limit, self.offset = limit, offset

def contains_pattern(term: str) -> str:
    """ILIKE pattern for 'contains term', with the user's % and _ taken literally."""
    escaped = term.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
