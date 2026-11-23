"""Nightly job: driver positions are kept for `location_retention_days`, then purged."""

import asyncio

from sqlalchemy import text

from app.core.config import settings
from app.core.db import engine, open_tenant_session


async def purge_positions() -> int:
    """The runtime role has no DELETE on driver_positions; app_purge_driver_positions (migration
    0012) runs as the table owner and only ever removes rows older than the retention window."""
    async with open_tenant_session(None, bypass=True) as db:
        n = await db.scalar(text("SELECT app_purge_driver_positions(:d)"), {"d": settings.location_retention_days})
        await db.commit()
    return int(n or 0)


def run_purge_positions() -> dict[str, int]:
    """Entry point for Celery (which is synchronous)."""

    async def _go() -> dict[str, int]:
        try:
            return {"deleted": await purge_positions()}
        finally:
            await engine.dispose()

    return asyncio.run(_go())
