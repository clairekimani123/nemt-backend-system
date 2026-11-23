"""What the DRIVER APP calls. A driver can only ever see and touch trips assigned to them."""

from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.modules.audit import service as audit
from app.modules.drivers.models import Driver, DutyStatus
from app.modules.patients.models import Patient
from app.modules.realtime import positions
from app.modules.realtime import publisher as rt
from app.modules.realtime.schemas import LocationPing
from app.modules.tenants.models import Company
from app.modules.trips import service
from app.modules.trips.models import Trip
from app.modules.trips.schemas import (
    DriverDeclineIn,
    DriverMeOut,
    DriverStatsOut,
    DriverTripOut,
    DutyIn,
    StatusIn,
)
from app.modules.trips.state import OCCUPYING, TERMINAL, TripStatus
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


UPCOMING_DAYS = 30


@router.get("/me", response_model=DriverMeOut)
async def my_profile(user: User = DRIVER, db: AsyncSession = Depends(get_db)):
    """The dashboard header: the signed-in driver's own account, company and availability."""
    driver = await _me(db, user)
    company = await db.get(Company, user.company_id)
    tz = await service.company_tz(db, user.company_id)
    return DriverMeOut(
        driver_id=driver.id, user_id=user.id, full_name=user.full_name, email=user.email, phone=user.phone,
        has_avatar=user.has_avatar, avatar_updated_at=user.avatar_updated_at,
        company_name=company.name if company else "", company_timezone=str(tz),
        employee_number=driver.employee_number, wheelchair_certified=driver.wheelchair_certified,
        duty_status=driver.duty_status, duty_changed_at=driver.duty_changed_at,
        on_trip=await positions.is_driving(db, driver), today=datetime.now(tz).date(),
    )


@router.put("/me/duty", response_model=DriverMeOut)
async def set_my_duty(body: DutyIn, request: Request, user: User = DRIVER, db: AsyncSession = Depends(get_db)):
    """Available / unavailable (break) / off duty. Dispatch sees the change live. A driver can't go
    off duty in the middle of a trip they have started: finish it, or ask dispatch to reassign it."""
    driver = await _me(db, user)
    if body.duty_status == DutyStatus.OFF_DUTY and await positions.is_driving(db, driver):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "You have a trip under way. Finish it (or ask dispatch to reassign it) before going off duty.")
    if driver.duty_status != body.duty_status:
        before = driver.duty_status
        driver.duty_status, driver.duty_changed_at = body.duty_status, datetime.now(UTC)
        await audit.record(db, action="driver.duty_change", actor=user, resource_type="driver",
                           resource_id=driver.id, request=request,
                           meta={"from": before.value, "to": body.duty_status.value})
        await db.commit()
        await rt.driver_duty_changed(user.company_id, driver.id, body.duty_status.value)
    return await my_profile(user, db)


@router.get("/stats", response_model=DriverStatsOut)
async def my_stats(user: User = DRIVER, db: AsyncSession = Depends(get_db)):
    driver = await _me(db, user)
    tz = await service.company_tz(db, user.company_id)
    today = datetime.now(tz).date()
    start, end = service.day_bounds(today, tz)
    week_start, _ = service.day_bounds(today - timedelta(days=7), tz)

    def n(cond):  # type: ignore[no-untyped-def]
        return func.count(case((cond, 1)))

    in_today = and_(Trip.scheduled_pickup_at >= start, Trip.scheduled_pickup_at < end)
    in_week = and_(Trip.scheduled_pickup_at >= week_start, Trip.scheduled_pickup_at < start)
    row = (await db.execute(select(
        n(in_today), n(and_(in_today, Trip.status.not_in(TERMINAL))),
        n(and_(in_today, Trip.status == TripStatus.COMPLETED)), n(and_(in_today, Trip.status == TripStatus.CANCELLED)),
        n(and_(in_today, Trip.status == TripStatus.NO_SHOW)),
        n(and_(in_week, Trip.status == TripStatus.COMPLETED)), n(and_(in_week, Trip.status == TripStatus.CANCELLED)),
        n(and_(in_week, Trip.status == TripStatus.NO_SHOW)),
        n(and_(Trip.scheduled_pickup_at >= end, Trip.status.in_(OCCUPYING))),
    ).where(Trip.company_id == driver.company_id, Trip.driver_id == driver.id,
            Trip.status != TripStatus.SCHEDULED))).one()
    return DriverStatsOut(today=today, today_total=row[0], today_remaining=row[1], today_completed=row[2],
                          today_cancelled=row[3], today_no_show=row[4], week_completed=row[5],
                          week_cancelled=row[6], week_no_show=row[7], upcoming=row[8])


@router.get("/trips", response_model=list[DriverTripOut])
async def my_trips(request: Request, on_date: date | None = None,
                   scope: Literal["day", "upcoming"] = "day",
                   user: User = DRIVER, db: AsyncSession = Depends(get_db)):
    """scope=day: one day's run (`on_date`, default today), every status, in pickup order.
    scope=upcoming: everything assigned to me that isn't finished, from today on (next 30 days),
    plus any trip of mine still under way from an earlier day.
    'Today' is the COMPANY's local day, not the device's."""
    driver = await _me(db, user)
    tz = await service.company_tz(db, user.company_id)
    if scope == "upcoming":
        start, _ = service.day_bounds(datetime.now(tz).date(), tz)
        horizon = start + timedelta(days=UPCOMING_DAYS + 1)
        stmt = _mine(driver).where(Trip.status.in_(OCCUPYING), Trip.scheduled_pickup_at < horizon,
                                   (Trip.scheduled_pickup_at >= start) | (Trip.status != TripStatus.ASSIGNED))
    else:
        day = on_date or datetime.now(tz).date()
        start, end = service.day_bounds(day, tz)
        stmt = _mine(driver).where(Trip.scheduled_pickup_at >= start, Trip.scheduled_pickup_at < end)
    rows = (await db.execute(stmt.order_by(Trip.scheduled_pickup_at).limit(200))).all()
    await audit.record(db, action="driver.trips_read", actor=user, resource_type="trip", request=request,
                       meta={"count": len(rows), "scope": scope})
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

@router.post("/trips/{trip_id}/decline", response_model=DriverTripOut | None, status_code=status.HTTP_200_OK)
async def decline_trip(trip_id: UUID, body: DriverDeclineIn, request: Request, user: User = DRIVER,
                       db: AsyncSession = Depends(get_db)):
    """Hand an assignment back to dispatch before starting it. Returns null: it is no longer yours."""
    driver = await _me(db, user)
    await service.driver_decline(db, user, driver, trip_id, body.reason, request)
    return None


@router.post("/location", status_code=status.HTTP_204_NO_CONTENT)
async def report_location(body: LocationPing, user: User = DRIVER, db: AsyncSession = Depends(get_db)):
    """One GPS fix from the driver's device (the REST twin of the WebSocket 'location' message,
    for when the socket is down). Positions are not audit-logged one by one: they are their own
    append-only record, and logging every fix would bury the audit trail."""
    driver = await _me(db, user)
    stored = await positions.record(db, driver, body)
    await db.commit()
    if stored is not None:  # a fix that came too soon after the last one is accepted but not kept
        await rt.driver_position(user.company_id, driver.id, body.lat, body.lng, body.trip_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
