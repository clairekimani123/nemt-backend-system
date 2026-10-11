"""What the DRIVER APP calls. A driver can only ever see and touch trips assigned to them."""

from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.modules.audit import service as audit
from app.modules.drivers.models import Driver
from app.modules.patients.models import Patient
from app.modules.trips import service
from app.modules.trips.models import Trip
from app.modules.trips.schemas import DriverTripOut, StatusIn
from app.modules.trips.state import TripStatus
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle

router = APIRouter(prefix="/driver", tags=["driver app"])
DRIVER = Depends(require_permission(Permission.DRIVER_APP))


async def _me(db: AsyncSession, user: User) -> Driver:
    driver = await db.scalar(select(Driver).where(Driver.user_id == user.id, Driver.company_id == user.company_id))
    if driver is None or driver.archived_at is not None:
        raise HTTPException(404, "No driver profile for this account")
    return driver


def _mine(driver: Driver):  # type: ignore[no-untyped-def]
    return (select(Trip, Patient, Vehicle)
            .join(Patient, and_(Patient.id == Trip.patient_id, Patient.company_id == Trip.company_id))
            .outerjoin(Vehicle, and_(Vehicle.id == Trip.vehicle_id, Vehicle.company_id == Trip.company_id))
            .where(Trip.company_id == driver.company_id, Trip.driver_id == driver.id,
                   Trip.status != TripStatus.SCHEDULED))


@router.get("/trips", response_model=list[DriverTripOut])
async def my_trips(request: Request, on_date: date | None = None, user: User = DRIVER, db: AsyncSession = Depends(get_db)):
    """Today's run (or `on_date`), in pickup order. 'Today' is the COMPANY's local day."""
    driver = await _me(db, user)
    tz = await service.company_tz(db, user.company_id)
    day = on_date or datetime.now(tz).date()
    start, end = service.day_bounds(day, tz)
    rows = (await db.execute(_mine(driver).where(Trip.scheduled_pickup_at >= start, Trip.scheduled_pickup_at < end)
                             .order_by(Trip.scheduled_pickup_at))).all()
    await audit.record(db, action="driver.trips_read", actor=user, resource_type="trip", request=request,
                       meta={"count": len(rows)})
    await db.commit()
    now = datetime.now(UTC)
    return [service.to_driver_out(*r, now) for r in rows]


@router.get("/trips/{trip_id}", response_model=DriverTripOut)
async def my_trip(trip_id: UUID, request: Request, user: User = DRIVER, db: AsyncSession = Depends(get_db)):
    driver = await _me(db, user)
    row = (await db.execute(_mine(driver).where(Trip.id == trip_id))).first()
    if row is None:
        raise HTTPException(404, "Trip not found")
    await audit.record(db, action="driver.trip_read", actor=user, resource_type="trip", resource_id=trip_id, request=request)
    await db.commit()
    return service.to_driver_out(*row, datetime.now(UTC))


@router.post("/trips/{trip_id}/status", response_model=DriverTripOut)
async def my_trip_status(trip_id: UUID, body: StatusIn, request: Request, user: User = DRIVER,
                         db: AsyncSession = Depends(get_db)):
    """The driver taps the next button: en route -> arrived -> picked up -> ... -> completed."""
    driver = await _me(db, user)
    await service.change_status(db, user, trip_id, body, request, driver=driver)
    row = (await db.execute(_mine(driver).where(Trip.id == trip_id))).first()
    return service.to_driver_out(*row, datetime.now(UTC))  # type: ignore[misc]