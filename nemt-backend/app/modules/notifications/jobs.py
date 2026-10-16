"""Celery-driven batch jobs: reminders, manifests.

Each job opens one tenant session per company, so RLS still applies. This is the same
pattern Phase 3's `materialize_all` uses.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, select

from app.core.config import settings
from app.core.db import engine, open_tenant_session
from app.modules.notifications import service as notifier
from app.modules.notifications.models import Channel
from app.modules.patients.models import Patient
from app.modules.tenants.models import Company
from app.modules.trips.models import Trip
from app.modules.trips.state import TripStatus
from app.modules.users.models import User
from app.modules.drivers.models import Driver


async def _companies():
    async with open_tenant_session(None, bypass=True) as db:
        return (await db.scalars(select(Company.id).where(
            Company.is_active.is_(True), Company.is_platform.is_(False)))).all()


async def trip_reminders_once() -> dict[str, int]:
    """Two-hour reminder for every scheduled trip whose pickup is inside the window."""
    now = datetime.now(UTC)
    window_end = now + timedelta(hours=2)
    totals = {"companies": 0, "sent": 0}
    for cid in await _companies():
        async with open_tenant_session(cid) as db:
            rows = (await db.execute(select(Trip, Patient).join(
                Patient, and_(Patient.id == Trip.patient_id, Patient.company_id == Trip.company_id))
                .where(Trip.company_id == cid,
                       Trip.status == TripStatus.SCHEDULED,
                       Trip.scheduled_pickup_at > now,
                       Trip.scheduled_pickup_at <= window_end))).all()
            for trip, patient in rows:
                if not patient.email:
                    continue
                await notifier.notify(
                    db, company_id=cid, event="trip.reminder.patient",
                    channel=Channel.EMAIL, to=patient.email, recipient_user_id=None,
                    context={
                        "patient_first_name": patient.first_name,
                        "trip_ref": f"T-{trip.trip_number:06d}",
                        "pickup_line1": trip.pickup_line1,
                        "pickup_at": trip.scheduled_pickup_at.isoformat(),
                    },
                )
                totals["sent"] += 1
            await db.commit()
        totals["companies"] += 1
    return totals


async def daily_manifests_once() -> dict[str, int]:
    """Email every driver with today's trips. Runs at 05:00 UTC, i.e. before the early shift."""
    from app.modules.trips.service import company_tz, day_bounds
    today_utc = datetime.now(UTC).date()
    totals = {"companies": 0, "drivers": 0, "trips": 0}
    for cid in await _companies():
        async with open_tenant_session(cid) as db:
            tz = await company_tz(db, cid)
            start, end = day_bounds(today_utc, tz)
            rows = (await db.execute(
                select(Trip, Driver, User)
                .join(Driver, and_(Driver.id == Trip.driver_id, Driver.company_id == Trip.company_id))
                .join(User, and_(User.id == Driver.user_id, User.company_id == Driver.company_id))
                .where(Trip.company_id == cid,
                       Trip.status.in_({TripStatus.ASSIGNED, TripStatus.EN_ROUTE_TO_PICKUP}),
                       Trip.scheduled_pickup_at >= start, Trip.scheduled_pickup_at < end)
                .order_by(Trip.driver_id, Trip.scheduled_pickup_at)
            )).all()
            by_driver: dict[tuple, list[Trip]] = {}
            names: dict = {}
            for trip, driver, user in rows:
                by_driver.setdefault(driver.id, []).append(trip)
                names[driver.id] = (user.email, user.full_name)
            for driver_id, trips in by_driver.items():
                email, full_name = names[driver_id]
                body_lines = [f"- T-{t.trip_number:06d} at {t.scheduled_pickup_at.isoformat()}: "
                              f"{t.pickup_line1} -> {t.dropoff_line1}" for t in trips]
                await notifier.notify(
                    db, company_id=cid, event="driver.manifest.daily",
                    channel=Channel.EMAIL, to=email, recipient_user_id=None,
                    context={
                        "driver_name": full_name,
                        "trip_count": len(trips),
                        "manifest": "\n".join(body_lines),
                    },
                )
                totals["drivers"] += 1
                totals["trips"] += len(trips)
            await db.commit()
        totals["companies"] += 1
    return totals


def _run(coro):
    async def _go():
        try:
            return await coro
        finally:
            await engine.dispose()
    return asyncio.run(_go())


def run_trip_reminders() -> dict[str, int]:
    return _run(trip_reminders_once())


def run_daily_manifests() -> dict[str, int]:
    return _run(daily_manifests_once())