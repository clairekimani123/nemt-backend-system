"""Database engine + the tenant context.

How tenant isolation works (defence in depth):
  1. App layer: every query filters by company_id (repository/service code).
  2. DB layer: Row-Level Security policies compare company_id with the Postgres
     setting `app.current_tenant`. We set it with set_config(..., true), i.e.
     transaction-local, so it also works behind a transaction-mode pooler and can
     never leak from one request to the next.
  If we ever forget (1), (2) returns zero rows instead of leaking data.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import settings

_engine_kwargs: dict[str, Any] = (
    {"poolclass": NullPool}
    if settings.env == "test"
    else {"pool_size": 10, "max_overflow": 10, "pool_pre_ping": True}
)
engine = create_async_engine(settings.database_url, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_SET_CTX = text(
    "SELECT set_config('app.current_tenant', :tenant, true),"
    " set_config('app.bypass_rls', :bypass, true)"
)


def _ctx_params(info: dict[str, Any]) -> dict[str, str]:
    tenant = info.get("tenant_id")
    return {"tenant": str(tenant) if tenant else "", "bypass": "on" if info.get("bypass") else "off"}


@event.listens_for(Session, "after_begin")
def _reapply_tenant_on_new_transaction(session, transaction, connection):  # type: ignore[no-untyped-def]
    """After each commit a new transaction starts; re-apply the tenant to it."""
    if "tenant_id" in session.info or "bypass" in session.info:
        connection.execute(_SET_CTX, _ctx_params(session.info))


async def apply_tenant(session: AsyncSession, company_id: UUID | None, bypass: bool = False) -> None:
    session.info["tenant_id"] = company_id
    session.info["bypass"] = bypass
    await session.execute(_SET_CTX, _ctx_params(session.info))


@asynccontextmanager
async def open_tenant_session(
    company_id: UUID | None, *, bypass: bool = False
) -> AsyncIterator[AsyncSession]:
    """A session pinned to one tenant. Uncommitted work is rolled back on exit."""
    async with SessionLocal() as session:
        await apply_tenant(session, company_id, bypass)
        yield session


async def resolve_company_id(slug: str) -> UUID | None:
    """Slug -> company id via a SECURITY DEFINER function (RLS would hide the row
    before we know which tenant we are)."""
    async with SessionLocal() as session:
        return await session.scalar(text("SELECT app_resolve_company(:s)"), {"s": slug.lower()})