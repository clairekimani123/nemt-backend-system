"""Nightly job: make sure every active recurring series has real trips for the next few weeks."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from app.core.config import settings
from app.core.db import engine, open_tenant_session
from app.modules.audit import service as audit
from app.modules.tenants.models import Company
from app.modules.trips import service
from app.modules.trips.models import RecurringTrip


async def materialize_all() -> dict[str, int]:
    """Visits every company, each in its OWN tenant-scoped session (so RLS still protects us)."""
    today = datetime.now(UTC).date()
    horizon = today + timedelta(days=settings.recurring_horizon_days)
    totals = {"companies": 0, "series": 0, "created": 0, "skipped": 0}
    async with open_tenant_session(None, bypass=True) as db:
        company_ids = (await db.scalars(select(Company.id).where(
            Company.is_active.is_(True), Company.is_platform.is_(False)))).all()
    for cid in company_ids:
        async with open_tenant_session(cid) as db:
            active = (await db.scalars(select(RecurringTrip).where(
                RecurringTrip.company_id == cid, RecurringTrip.is_active.is_(True),
                or_(RecurringTrip.end_date.is_(None), RecurringTrip.end_date >= today)))).all()
            for series in active:
                created, skipped = await service.materialize(db, series, horizon, None)
                totals["series"] += 1
                totals["created"] += created
                totals["skipped"] += len(skipped)
                if created or skipped:
                    await audit.record(db, action="recurring.materialize", company_id=cid, resource_type="recurring_trip",
                                       resource_id=series.id, meta={"created": created, "skipped": len(skipped), "by": "nightly_job"})
            await db.commit()
        totals["companies"] += 1
    return totals


def run_materialize_all() -> dict[str, int]:
    """Entry point for Celery (which is synchronous)."""

    async def _go() -> dict[str, int]:
        try:
            return await materialize_all()
        finally:
            await engine.dispose()  # a worker loop is short-lived; don't leave pooled connections behind

    return asyncio.run(_go())