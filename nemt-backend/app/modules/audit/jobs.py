"""Nightly retention job."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from app.core.config import settings
from app.core.db import engine, open_tenant_session
from app.modules.audit.models import AuditLog


async def purge_expired_audit_logs_once() -> dict[str, int]:
    """Delete audit_logs older than `audit_retention_days` (default 6 years).

    Runs as owner (bypass), so RLS doesn't hide rows from other tenants.
    """
    cutoff = datetime.now(UTC) - timedelta(days=settings.audit_retention_days)
    total = 0
    async with open_tenant_session(None, bypass=True) as db:
        result = await db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
        total = result.rowcount or 0
        await db.commit()
    return {"deleted_audit_rows": total}


def run_purge_expired_audit_logs() -> dict[str, int]:
    async def _go():
        try:
            return await purge_expired_audit_logs_once()
        finally:
            await engine.dispose()
    return asyncio.run(_go())