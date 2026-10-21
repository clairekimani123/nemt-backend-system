"""Nightly jobs for the fleet module."""

import asyncio
from datetime import date, timedelta

from sqlalchemy import and_, select

from app.core.db import engine, open_tenant_session
from app.modules.fleet import service as fleet
from app.modules.notifications import service as notifier
from app.modules.notifications.models import Channel
from app.modules.tenants.models import Company
from app.modules.users.models import User
from app.core.permissions import Role


async def _companies_with_fleet_managers():
    """Yield (company_id, fleet_manager_email, fleet_manager_name) for each tenant."""
    async with open_tenant_session(None, bypass=True) as db:
        companies = (await db.scalars(select(Company).where(
            Company.is_active.is_(True), Company.is_platform.is_(False)))).all()
    for c in companies:
        async with open_tenant_session(c.id) as db:
            fm = await db.scalar(select(User).where(
                User.company_id == c.id, User.role == Role.FLEET_MANAGER,
                User.is_active.is_(True)).limit(1))
            if fm is not None:
                yield c.id, fm.email, fm.full_name


async def fleet_expiry_digest_once() -> dict[str, int]:
    totals = {"companies": 0, "emails": 0, "vehicles_flagged": 0}
    for company_id, email, name in _companies_with_fleet_managers():
        async with open_tenant_session(company_id) as db:
            dashboard = await fleet.fleet_dashboard(db, company_id)
        flagged = [v for v in dashboard if v.alerts]
        if not flagged:
            continue
        lines = []
        for v in flagged:
            lines.append(f"- {v.unit_number} ({v.make} {v.model}): {', '.join(v.alerts)}")
        body = "\n".join(lines)
        async with open_tenant_session(company_id) as db:
            await notifier.notify(
                db, company_id=company_id, event="fleet.expiry_digest",
                channel=Channel.EMAIL, to=email, recipient_user_id=None,
                context={"fleet_manager_name": name, "count": len(flagged),
                         "vehicles": body},
            )
            await db.commit()
        totals["companies"] += 1
        totals["emails"] += 1
        totals["vehicles_flagged"] += len(flagged)
    return totals


def run_fleet_expiry_digest() -> dict[str, int]:
    async def _go():
        try:
            return await fleet_expiry_digest_once()
        finally:
            await engine.dispose()
    return asyncio.run(_go())