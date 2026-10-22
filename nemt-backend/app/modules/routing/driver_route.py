"""The driver's manifest: the ordered stops of today's published route."""

from datetime import date, datetime

# from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.core.security import decrypt_field
from app.modules.audit import service as audit
from app.modules.drivers.models import Driver
from app.modules.patients.models import Patient
from app.modules.routing.models import Route, RouteStatus, RouteStop
from app.modules.routing.schemas import DriverRouteOut, DriverStopOut
from app.modules.trips.models import Trip
from app.modules.trips.service import company_tz, place_out
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle

router = APIRouter(prefix="/driver", tags=["driver app"])
DRIVER = Depends(require_permission(Permission.DRIVER_APP))


@router.get("/route", response_model=DriverRouteOut)
async def my_route(request: Request, on_date: date | None = None, user: User = DRIVER,
                   db: AsyncSession = Depends(get_db)):
    """Today's stops in the order the optimiser chose."""
    driver = await db.scalar(select(Driver).where(Driver.user_id == user.id,
                                                  Driver.company_id == user.company_id))
    if driver is None or driver.archived_at is not None:
        raise HTTPException(404, "No driver profile for this account")
    day = on_date or datetime.now(await company_tz(db, user.company_id)).date()
    route = await db.scalar(select(Route).where(
        Route.company_id == user.company_id, Route.driver_id == driver.id,
        Route.route_date == day, Route.status == RouteStatus.PUBLISHED))
    if route is None:
        raise HTTPException(404, "No published route for that day")
    rows = (await db.execute(select(RouteStop, Trip, Patient).join(
        Trip, and_(Trip.id == RouteStop.trip_id, Trip.company_id == RouteStop.company_id)).join(
        Patient, and_(Patient.id == Trip.patient_id, Patient.company_id == Trip.company_id)).where(
        RouteStop.route_id == route.id).order_by(RouteStop.sequence))).all()
    vehicle = await db.get(Vehicle, route.vehicle_id)
    stops = []
    for st, trip, pat in rows:
        pickup = st.kind == "pickup"
        stops.append(DriverStopOut(
            sequence=st.sequence, kind=st.kind, trip_id=trip.id,
            trip_ref=f"T-{trip.trip_number:06d}", trip_status=trip.status,
            patient_first_name=pat.first_name, patient_last_name=pat.last_name,
            patient_phone=pat.phone if pickup else None,
            mobility=trip.mobility, escort_count=trip.escort_count,
            uses_oxygen=trip.uses_oxygen, has_service_animal=trip.has_service_animal,
            special_instructions=decrypt_field(trip.special_instructions_enc)
            if pickup and trip.special_instructions_enc else None,
            place=place_out(trip, "pickup" if pickup else "dropoff"),
            planned_arrival_at=st.planned_arrival_at,
            booked_time=trip.scheduled_pickup_at if pickup else trip.appointment_at,
        ))
    await audit.record(db, action="driver.route_read", actor=user, resource_type="route",
                       resource_id=route.id, request=request)
    await db.commit()
    return DriverRouteOut(route_id=route.id, route_date=route.route_date,
                          vehicle_unit=vehicle.unit_number, stops=stops)