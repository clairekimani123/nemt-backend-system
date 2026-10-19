"""What the PASSENGER PORTAL calls. A passenger only ever sees and touches their own record:
every query is pinned to the patient row linked to the caller's login (patients.user_id)."""

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.address import AddressIn, apply_address
from app.core.config import settings
from app.core.crud import Page, flush_or_409
from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.core.security import decrypt_field
from app.core.storage import Storage, get_storage
from app.modules.audit import service as audit
from app.modules.auth.service import split_name
from app.modules.drivers.models import Driver
from app.modules.facilities.models import Facility
from app.modules.passenger.schemas import (
    AnswerIn,
    AssignedDriverOut,
    AssignedVehicleOut,
    AssignmentOut,
    DestinationOut,
    DriverLocationOut,
    PassengerAddressOut,
    PassengerProfileOut,
    PassengerProfileUpdate,
    PassengerTripOut,
    RideRequestIn,
)
from app.modules.patients import service as patient_service
from app.modules.patients.models import Patient, PatientAddress
from app.modules.patients.schemas import PatientAddressCreate
from app.modules.realtime import publisher as rt
from app.modules.realtime.models import DriverPosition
from app.modules.routing.providers import Point, RoutingError, get_router
from app.modules.trips import service as trips
from app.modules.trips.models import Trip
from app.modules.trips.schemas import StatusIn, TripCreate, TripOut
from app.modules.trips.state import (
    CONTACT_VISIBLE,
    LOCATION_VISIBLE,
    PASSENGER_ANSWERABLE,
    TERMINAL,
    CancelReason,
    PassengerResponse,
    TripSource,
    TripStatus,
)
from app.modules.users import avatar
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle

log = logging.getLogger(__name__)

router = APIRouter(prefix="/passenger", tags=["passenger portal"])
PASSENGER = Depends(require_permission(Permission.PASSENGER_PORTAL))
CANCELLABLE = {TripStatus.SCHEDULED, TripStatus.ASSIGNED}


async def _me(db: AsyncSession, user: User) -> Patient:
    patient = await db.scalar(select(Patient).where(Patient.user_id == user.id, Patient.company_id == user.company_id))
    if patient is None or patient.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No passenger profile is linked to this account. Please contact your transport company.")
    return patient


def _profile(user: User, p: Patient) -> PassengerProfileOut:
    return PassengerProfileOut(
        user_id=user.id, patient_id=p.id, email=user.email, full_name=user.full_name, first_name=p.first_name,
        last_name=p.last_name, phone=p.phone, date_of_birth=date.fromisoformat(decrypt_field(p.date_of_birth_enc)),
        preferred_language=p.preferred_language, medicaid_last4=p.medicaid_last4, mobility=p.mobility,
        assistance_level=p.assistance_level, requires_escort=p.requires_escort, uses_oxygen=p.uses_oxygen,
        has_service_animal=p.has_service_animal, companion_name=p.companion_name, companion_phone=p.companion_phone,
        companion_relationship=p.companion_relationship, emergency_contact_name=p.emergency_contact_name,
        emergency_contact_phone=p.emergency_contact_phone, created_at=p.created_at,
    )


def _trip_out(t: TripOut) -> PassengerTripOut:
    return PassengerTripOut(
        **t.model_dump(include=set(PassengerTripOut.model_fields) - {"driver_name", "vehicle_unit"}),
        driver_name=t.driver.full_name if t.driver else None,
        vehicle_unit=t.vehicle.unit_number if t.vehicle else None,
    )


# ------------------------------------------------------------------ profile
@router.get("/profile", response_model=PassengerProfileOut)
async def get_profile(user: User = PASSENGER, db: AsyncSession = Depends(get_db)):
    return _profile(user, await _me(db, user))


@router.patch("/profile", response_model=PassengerProfileOut)
async def update_profile(body: PassengerProfileUpdate, request: Request, user: User = PASSENGER,
                         db: AsyncSession = Depends(get_db)):
    patient = await _me(db, user)
    changes = body.model_dump(exclude_unset=True)
    if "full_name" in changes:
        user.full_name = " ".join(changes.pop("full_name").split())
        first, last = split_name(user.full_name)
        changes |= {"first_name": first[:100], "last_name": last[:100]}
    if "phone" in changes:
        user.phone = changes["phone"]
    patient_service.apply_fields(patient, changes)
    await audit.record(db, action="passenger.profile_update", actor=user, resource_type="patient",
                       resource_id=patient.id, request=request, meta={"fields": sorted(body.model_fields_set)})
    await db.commit()
    await db.refresh(patient)
    return _profile(user, patient)


# ------------------------------------------------------------------ saved places
@router.get("/addresses", response_model=list[PassengerAddressOut])
async def list_addresses(user: User = PASSENGER, db: AsyncSession = Depends(get_db)):
    patient = await _me(db, user)
    return (await db.scalars(select(PatientAddress).where(
        PatientAddress.patient_id == patient.id, PatientAddress.company_id == user.company_id)
        .order_by(PatientAddress.is_primary.desc(), PatientAddress.label))).all()


@router.post("/addresses", response_model=PassengerAddressOut, status_code=status.HTTP_201_CREATED)
async def add_address(body: PatientAddressCreate, request: Request, user: User = PASSENGER,
                      db: AsyncSession = Depends(get_db)):
    patient = await _me(db, user)
    if body.is_primary:
        await db.execute(PatientAddress.__table__.update()
                         .where(PatientAddress.patient_id == patient.id, PatientAddress.is_primary.is_(True))
                         .values(is_primary=False))
    addr = PatientAddress(company_id=user.company_id, patient_id=patient.id, label=body.label,
                          is_primary=body.is_primary, access_notes=body.access_notes)
    await apply_address(addr, AddressIn(**body.model_dump(include=set(AddressIn.model_fields))))
    db.add(addr)
    await flush_or_409(db, "Could not save address")
    await audit.record(db, action="passenger.address_add", actor=user, resource_type="patient",
                       resource_id=patient.id, request=request, meta={"address_id": str(addr.id)})
    await db.commit()
    await db.refresh(addr)
    return addr


@router.delete("/addresses/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_address(address_id: UUID, request: Request, user: User = PASSENGER,
                         db: AsyncSession = Depends(get_db)):
    patient = await _me(db, user)
    addr = await db.scalar(select(PatientAddress).where(
        PatientAddress.id == address_id, PatientAddress.patient_id == patient.id,
        PatientAddress.company_id == user.company_id))
    if addr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Address not found")
    await db.delete(addr)
    await audit.record(db, action="passenger.address_delete", actor=user, resource_type="patient",
                       resource_id=patient.id, request=request, meta={"address_id": str(address_id)})
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/destinations", response_model=list[DestinationOut])
async def destinations(user: User = PASSENGER, db: AsyncSession = Depends(get_db)):
    """The company's facilities a passenger can be driven to (hospitals, clinics, dialysis...)."""
    rows = (await db.scalars(select(Facility).where(Facility.company_id == user.company_id,
                                                    Facility.archived_at.is_(None))
                             .order_by(Facility.name).limit(500))).all()
    return [DestinationOut.model_validate(f, from_attributes=True) for f in rows]


# ------------------------------------------------------------------ trips
@router.get("/trips", response_model=list[PassengerTripOut])
async def my_trips(
    scope: Literal["upcoming", "past", "all"] = "all",
    page: Page = Depends(),
    user: User = PASSENGER,
    db: AsyncSession = Depends(get_db),
):
    patient = await _me(db, user)
    stmt = trips.trip_select().where(Trip.company_id == user.company_id, Trip.patient_id == patient.id)
    if scope == "upcoming":
        stmt = stmt.where(Trip.status.not_in(TERMINAL)).order_by(Trip.scheduled_pickup_at)
    elif scope == "past":
        stmt = stmt.where(Trip.status.in_(TERMINAL)).order_by(Trip.scheduled_pickup_at.desc())
    else:
        stmt = stmt.order_by(Trip.scheduled_pickup_at.desc())
    rows = (await db.execute(stmt.limit(page.limit).offset(page.offset))).all()
    now = datetime.now(UTC)
    return [_trip_out(trips.to_out(*r, now)) for r in rows]


async def _own_trip(db: AsyncSession, user: User, patient: Patient, trip_id: UUID) -> PassengerTripOut:
    row = (await db.execute(trips.trip_select().where(
        Trip.id == trip_id, Trip.company_id == user.company_id, Trip.patient_id == patient.id))).first()
    if row is None:  # someone else's trip is indistinguishable from a missing one
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip not found")
    return _trip_out(trips.to_out(*row, datetime.now(UTC)))


@router.get("/trips/{trip_id}", response_model=PassengerTripOut)
async def my_trip(trip_id: UUID, user: User = PASSENGER, db: AsyncSession = Depends(get_db)):
    return await _own_trip(db, user, await _me(db, user), trip_id)


@router.post("/trips", response_model=list[PassengerTripOut], status_code=status.HTTP_201_CREATED)
async def request_ride(body: RideRequestIn, request: Request, user: User = PASSENGER,
                       db: AsyncSession = Depends(get_db)):
    """A ride request. It lands in dispatch's 'unassigned' lane exactly like a phoned-in booking;
    a dispatcher then assigns a driver and vehicle."""
    patient = await _me(db, user)
    create = TripCreate(patient_id=patient.id, **body.model_dump(exclude_unset=True))
    ids = await trips.create_trips(db, user, create, request, source=TripSource.PASSENGER_PORTAL)
    return [_trip_out(t) for t in await trips.load_outs(db, user.company_id, ids)]


@router.post("/trips/{trip_id}/cancel", response_model=PassengerTripOut)
async def cancel_ride(trip_id: UUID, request: Request, user: User = PASSENGER, db: AsyncSession = Depends(get_db)):
    patient = await _me(db, user)
    trip = await trips.lock_trip(db, user.company_id, trip_id)
    if trip.patient_id != patient.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip not found")
    if trip.status not in CANCELLABLE:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This ride can no longer be cancelled online; please call dispatch")
    body = StatusIn(status=TripStatus.CANCELLED, reason_code=CancelReason.PATIENT_CANCELLED,
                    note="Cancelled by the passenger")
    trips.apply_transition(db, trip, TripStatus.CANCELLED, user.id, body, by_driver=False)
    await audit.record(db, action="trip.status", actor=user, resource_type="trip", resource_id=trip.id,
                       request=request, meta={"to": "cancelled", "by": "passenger"})
    await rt.trip_status_changed(trip.company_id, trip.id, TripStatus.CANCELLED, trip.driver_id, trip.vehicle_id)
    await db.commit()
    return await _own_trip(db, user, patient, trip_id)


# ------------------------------------------------------------------ assigned driver
async def _own_trip_row(db: AsyncSession, user: User, patient: Patient, trip_id: UUID) -> Trip:
    trip = await db.scalar(select(Trip).where(
        Trip.id == trip_id, Trip.company_id == user.company_id, Trip.patient_id == patient.id))
    if trip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip not found")
    return trip


async def _driver_location(db: AsyncSession, trip: Trip) -> tuple[DriverLocationOut | None, str | None]:
    """The assigned driver's newest position, ONLY while they are heading to this passenger's
    pickup, and only positions reported since they set off for it."""
    if trip.status not in LOCATION_VISIBLE or trip.driver_id is None:
        return None, ("Your driver's location appears once they are on the way to your pickup."
                      if trip.status == TripStatus.ASSIGNED else None)
    since = max(trip.en_route_at or trip.assigned_at or trip.created_at, datetime.now(UTC) - timedelta(minutes=30))
    row = (await db.execute(
        select(DriverPosition.latitude, DriverPosition.longitude, DriverPosition.recorded_at,
               func.ST_Distance(DriverPosition.location, Trip.pickup_location))
        .join(Trip, (Trip.id == trip.id) & (Trip.company_id == DriverPosition.company_id))
        .where(DriverPosition.company_id == trip.company_id, DriverPosition.driver_id == trip.driver_id,
               DriverPosition.recorded_at >= since)
        .order_by(DriverPosition.recorded_at.desc()).limit(1))).first()
    if row is None:
        return None, "Your driver hasn't shared a location yet."
    lat, lng, at, straight = row
    age = max(0, int((datetime.now(UTC) - at).total_seconds()))
    road_m = eta_s = source = None
    router = get_router()
    if router.name == "osrm":  # a real road router; the offline model is not presented as an ETA
        try:
            path = await router.path([Point(lat, lng), Point(trip.pickup_latitude, trip.pickup_longitude)])
            road_m, eta_s, source = path.distance_m, int(path.duration_s), "osrm"
        except (RoutingError, OSError) as exc:
            log.warning("eta unavailable for trip %s: %s", trip.id, type(exc).__name__)
    return DriverLocationOut(
        latitude=lat, longitude=lng, recorded_at=at, age_seconds=age,
        stale=age > settings.location_stale_seconds, straight_line_m=round(float(straight), 1),
        road_distance_m=road_m, eta_seconds=eta_s, eta_source=source,
    ), None


@router.get("/trips/{trip_id}/assignment", response_model=AssignmentOut)
async def my_assignment(trip_id: UUID, user: User = PASSENGER, db: AsyncSession = Depends(get_db)):
    """The "driver found" card: who is coming, in what vehicle, and (while they are on the way)
    where they are. Bound to ONE trip of the caller; nothing about any other trip or driver."""
    patient = await _me(db, user)
    trip = await _own_trip_row(db, user, patient, trip_id)
    driver_out = vehicle_out = None
    if trip.driver_id is not None and trip.status not in TERMINAL:
        driver = await db.get(Driver, trip.driver_id)
        du = await db.get(User, driver.user_id) if driver else None
        vehicle = await db.get(Vehicle, trip.vehicle_id) if trip.vehicle_id else None
        if driver and du:
            driver_out = AssignedDriverOut(
                id=driver.id, display_name=du.full_name, has_photo=du.has_avatar,
                photo_updated_at=du.avatar_updated_at,
                phone=du.phone if trip.status in CONTACT_VISIBLE else None,
                wheelchair_certified=driver.wheelchair_certified,
            )
        if vehicle:
            vehicle_out = AssignedVehicleOut.model_validate(vehicle, from_attributes=True)
    # Not audit-logged per read (like the passenger's other own-trip reads): the portal polls this.
    location, note = await _driver_location(db, trip) if driver_out else (None, None)
    return AssignmentOut(
        trip_id=trip.id, trip_ref=f"T-{trip.trip_number:06d}", status=trip.status,
        passenger_response=trip.passenger_response, passenger_responded_at=trip.passenger_responded_at,
        assigned_at=trip.assigned_at,
        can_respond=(driver_out is not None and trip.status in PASSENGER_ANSWERABLE
                     and trip.passenger_response == PassengerResponse.PENDING),
        driver=driver_out, vehicle=vehicle_out,
        pickup_latitude=trip.pickup_latitude, pickup_longitude=trip.pickup_longitude,
        location=location, location_note=note,
    )


@router.post("/trips/{trip_id}/assignment/accept", response_model=PassengerTripOut)
async def accept_driver(trip_id: UUID, body: AnswerIn, request: Request, user: User = PASSENGER,
                        db: AsyncSession = Depends(get_db)):
    patient = await _me(db, user)
    await trips.answer_assignment(db, user, patient, trip_id, accept=True, expected_driver_id=body.driver_id,
                                  request=request)
    return await _own_trip(db, user, patient, trip_id)


@router.post("/trips/{trip_id}/assignment/decline", response_model=PassengerTripOut)
async def decline_driver(trip_id: UUID, body: AnswerIn, request: Request, user: User = PASSENGER,
                         db: AsyncSession = Depends(get_db)):
    """Releases the driver; the ride goes back to dispatch, who choose another driver."""
    patient = await _me(db, user)
    await trips.answer_assignment(db, user, patient, trip_id, accept=False, expected_driver_id=body.driver_id,
                                  request=request)
    return await _own_trip(db, user, patient, trip_id)


@router.get("/trips/{trip_id}/driver-photo")
async def driver_photo(trip_id: UUID, user: User = PASSENGER, db: AsyncSession = Depends(get_db),
                       storage: Storage = Depends(get_storage)):
    """The photo of the driver currently assigned to this (live) trip of the caller, nobody else's."""
    patient = await _me(db, user)
    trip = await _own_trip_row(db, user, patient, trip_id)
    if trip.driver_id is None or trip.status in TERMINAL:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No profile photo")
    driver = await db.get(Driver, trip.driver_id)
    du = await db.get(User, driver.user_id) if driver else None
    if du is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No profile photo")
    return await avatar.serve(storage, du)
