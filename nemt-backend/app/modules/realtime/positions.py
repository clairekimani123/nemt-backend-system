"""Recording a driver's position: the ONE path both the WebSocket and REST endpoint use."""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.geo import point
from app.modules.drivers.models import Driver
from app.modules.realtime.models import DriverPosition
from app.modules.realtime.schemas import LocationPing
from app.modules.trips.models import Trip
from app.modules.trips.state import OCCUPYING

MAX_AGE = timedelta(minutes=5)      # a fix older than this is not "where the driver is"
MAX_SKEW = timedelta(minutes=1)     # tolerate a device clock slightly ahead of ours


async def record(db: AsyncSession, driver: Driver, ping: LocationPing) -> DriverPosition:
    """Validate and store one fix. The caller commits. A trip tag must be one of THIS driver's
    live trips, so a driver can't attach positions to (or appear on) someone else's ride."""
    now = datetime.now(UTC)
    at = ping.recorded_at or now
    if at < now - MAX_AGE or at > now + MAX_SKEW:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "recorded_at is too far from the current time")
    if ping.trip_id is not None:
        trip = await db.scalar(select(Trip.id).where(
            Trip.id == ping.trip_id, Trip.company_id == driver.company_id, Trip.driver_id == driver.id,
            Trip.status.in_(OCCUPYING)))
        if trip is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip not found")
    pos = DriverPosition(
        company_id=driver.company_id, driver_id=driver.id, trip_id=ping.trip_id,
        location=point(ping.lat, ping.lng), speed_mps=ping.speed_mps, heading_deg=ping.heading_deg,
        accuracy_m=ping.accuracy_m, recorded_at=min(at, now),
    )
    db.add(pos)
    return pos
