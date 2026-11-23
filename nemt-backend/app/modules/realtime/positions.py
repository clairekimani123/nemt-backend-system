"""Recording a driver's position: the ONE path both the WebSocket and REST endpoint use."""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.geo import point
from app.modules.drivers.models import Driver, DutyStatus
from app.modules.realtime.models import DriverPosition
from app.modules.realtime.schemas import LocationPing
from app.modules.trips.models import Trip
from app.modules.trips.state import OCCUPYING, TripStatus

DRIVING = OCCUPYING - {TripStatus.ASSIGNED}  # actually under way: location is part of the job

MAX_AGE = timedelta(minutes=5)      # a fix older than this is not "where the driver is"
MAX_SKEW = timedelta(minutes=1)     # tolerate a device clock slightly ahead of ours


async def is_driving(db: AsyncSession, driver: Driver) -> bool:
    return await db.scalar(select(select(Trip.id).where(
        Trip.company_id == driver.company_id, Trip.driver_id == driver.id, Trip.status.in_(DRIVING)).exists())) or False


async def record(db: AsyncSession, driver: Driver, ping: LocationPing) -> DriverPosition | None:
    """Validate and store one fix. The caller commits. A trip tag must be one of THIS driver's
    live trips, so a driver can't attach positions to (or appear on) someone else's ride.

    Returns None (nothing stored, nothing to publish) for a fix that arrives sooner than
    `location_min_interval_seconds` after the last stored one: a chatty device can't flood the table."""
    now = datetime.now(UTC)
    at = ping.recorded_at or now
    if at < now - MAX_AGE or at > now + MAX_SKEW:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "recorded_at is too far from the current time")
    if driver.duty_status == DutyStatus.OFF_DUTY and not await is_driving(db, driver):
        raise HTTPException(status.HTTP_409_CONFLICT, "You are off duty. Go on duty to share your location.")
    last = await db.scalar(select(DriverPosition.recorded_at).where(
        DriverPosition.company_id == driver.company_id, DriverPosition.driver_id == driver.id)
        .order_by(DriverPosition.recorded_at.desc()).limit(1))
    if last is not None and min(at, now) - last < timedelta(seconds=settings.location_min_interval_seconds):
        return None
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
