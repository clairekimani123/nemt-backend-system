"""Trip engine: create, edit, assign, move through the life-cycle, and generate recurring trips."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.address import AddressIn
from app.core.base import new_id
from app.core.config import settings
from app.core.crud import flush_or_409, get_or_404
from app.core.geo import get_geocoder, point
from app.core.security import decrypt_field, encrypt_field
from app.modules.audit import service as audit
from app.modules.drivers.models import Driver
from app.modules.facilities.models import Broker, Facility
from app.modules.patients.models import Patient, PatientAddress
from app.modules.tenants.models import Company
from app.modules.trips import conflicts
from app.modules.trips.models import RecurringTrip, Trip, TripEvent
from app.modules.trips.recurrence import occurrences
from app.modules.trips.schemas import (
    AssignmentReportOut,
    DriverBrief,
    DriverTripOut,
    IssueOut,
    PatientBrief,
    PlaceOut,
    PlaceRef,
    RecurringCreate,
    SeriesOut,
    StatusIn,
    TripCreate,
    TripOut,
    TripUpdate,
    VehicleBrief,
)
from app.modules.trips.state import (
    DRIVER_TARGETS,
    EDITABLE,
    NON_TERMINAL,
    PRE_PICKUP,
    STATUS_ENDPOINT_TARGETS,
    Leg,
    TripSource,
    TripStatus,
    can_transition,
)
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle
from app.modules.realtime import publisher as rt

S = TripStatus


def _err(code: int, detail: Any) -> HTTPException:
    return HTTPException(code, detail)


# ======================================================================= small helpers
async def company_tz(db: AsyncSession, company_id: UUID) -> ZoneInfo:
    name = await db.scalar(select(Company.timezone).where(Company.id == company_id))
    return ZoneInfo(name or "UTC")


def local_to_utc(day: date, at: time, tz: ZoneInfo) -> datetime:
    """07:30 on a given local day -> the right UTC instant, whatever DST is doing that day."""
    return datetime.combine(day, at, tzinfo=tz).astimezone(UTC)


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
    return start, datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)


async def next_number(db: AsyncSession, company_id: UUID, name: str) -> int:
    """Atomic per-company counter. The UPDATE locks the row, so two dispatchers booking at the
    same instant get different numbers; and a rolled-back booking gives its number back."""
    return await db.scalar(
        text("""INSERT INTO counters (company_id, name, value) VALUES (:c, :n, 1)
                ON CONFLICT (company_id, name) DO UPDATE SET value = counters.value + 1
                RETURNING value"""), {"c": company_id, "n": name})


def _age(dob: date) -> int:
    t = date.today()
    return t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))


def _decrypt(enc: str | None) -> str | None:
    return decrypt_field(enc) if enc else None


def flags(trip: Trip, now: datetime) -> tuple[bool, bool]:
    """(is_late, at_risk). Derived from the clock, never stored: stored flags go stale."""
    late = trip.status in PRE_PICKUP and now > trip.scheduled_pickup_at + timedelta(minutes=settings.late_grace_minutes)
    risk = (trip.status == S.SCHEDULED and not late
            and now > trip.scheduled_pickup_at - timedelta(hours=settings.at_risk_hours))
    return late, risk


# ======================================================================= places
async def locate(addr: AddressIn) -> AddressIn:
    """Guarantee coordinates: use what we have, else geocode, else refuse."""
    if addr.latitude is not None:
        return addr
    found = await get_geocoder().geocode(f"{addr.line1}, {addr.city}, {addr.state} {addr.postal_code}")
    if not found:
        raise _err(422, f"Could not locate '{addr.line1}, {addr.city}' on the map; send latitude and longitude")
    return addr.model_copy(update={"latitude": found[0], "longitude": found[1]})


async def resolve_place(db: AsyncSession, company_id: UUID, patient_id: UUID, ref: PlaceRef) -> AddressIn:
    if ref.address is not None:
        return await locate(ref.address)
    if ref.patient_address_id is not None:
        pa = await get_or_404(db, PatientAddress, ref.patient_address_id, company_id, "Patient address")
        if pa.patient_id != patient_id:
            raise _err(422, "That saved address belongs to a different patient")
        src: Any = pa
    else:
        src = await get_or_404(db, Facility, ref.facility_id, company_id, "Facility")  # type: ignore[arg-type]
        if src.archived_at is not None:
            raise _err(422, "That facility is archived")
    return await locate(AddressIn(line1=src.line1, line2=src.line2, city=src.city, state=src.state,
                                  postal_code=src.postal_code, latitude=src.latitude, longitude=src.longitude))


def set_place(obj: Any, prefix: str, a: AddressIn) -> None:
    for f in ("line1", "line2", "city", "state", "postal_code"):
        setattr(obj, f"{prefix}_{f}", getattr(a, f))
    setattr(obj, f"{prefix}_location", point(a.latitude, a.longitude))  # type: ignore[arg-type]


def place_out(obj: Any, prefix: str) -> PlaceOut:
    return PlaceOut(**{f: getattr(obj, f"{prefix}_{f}") for f in
                       ("line1", "line2", "city", "state", "postal_code", "latitude", "longitude")})


def place_as_address(obj: Any, prefix: str) -> AddressIn:
    p = place_out(obj, prefix)
    return AddressIn(**p.model_dump())


# ======================================================================= events
def add_event(db: AsyncSession, trip: Trip, event_type: str, actor_id: UUID | None, *,
              from_status: TripStatus | None = None, to_status: TripStatus | None = None,
              lat: float | None = None, lng: float | None = None, reason: str | None = None,
              note: str | None = None, meta: dict | None = None) -> None:
    db.add(TripEvent(
        company_id=trip.company_id, trip_id=trip.id, event_type=event_type, actor_user_id=actor_id,
        from_status=from_status.value if from_status else None, to_status=to_status.value if to_status else None,
        location=point(lat, lng) if lat is not None and lng is not None else None,
        reason_code=reason, note_enc=encrypt_field(note) if note else None, meta=meta or {},
    ))


# ======================================================================= reading
def trip_select():  # type: ignore[no-untyped-def]
    du = aliased(User)
    return (select(Trip, Patient, Driver, du, Vehicle)
            .join(Patient, and_(Patient.id == Trip.patient_id, Patient.company_id == Trip.company_id))
            .outerjoin(Driver, and_(Driver.id == Trip.driver_id, Driver.company_id == Trip.company_id))
            .outerjoin(du, and_(du.id == Driver.user_id, du.company_id == Driver.company_id))
            .outerjoin(Vehicle, and_(Vehicle.id == Trip.vehicle_id, Vehicle.company_id == Trip.company_id)))


def to_out(trip: Trip, patient: Patient, driver: Driver | None, driver_user: User | None,
           vehicle: Vehicle | None, now: datetime) -> TripOut:
    late, risk = flags(trip, now)
    return TripOut(
        id=trip.id, trip_number=trip.trip_number, trip_ref=f"T-{trip.trip_number:06d}", status=trip.status,
        leg=trip.leg, source=trip.source,
        patient=PatientBrief(id=patient.id, first_name=patient.first_name, last_name=patient.last_name, phone=patient.phone),
        driver=DriverBrief(id=driver.id, full_name=driver_user.full_name) if driver and driver_user else None,
        vehicle=VehicleBrief(id=vehicle.id, unit_number=vehicle.unit_number) if vehicle else None,
        pickup=place_out(trip, "pickup"), dropoff=place_out(trip, "dropoff"),
        scheduled_pickup_at=trip.scheduled_pickup_at, appointment_at=trip.appointment_at,
        estimated_duration_min=trip.estimated_duration_min, ends_at=trip.ends_at, mobility=trip.mobility,
        escort_count=trip.escort_count, uses_oxygen=trip.uses_oxygen, has_service_animal=trip.has_service_animal,
        special_instructions=_decrypt(trip.special_instructions_enc), broker_id=trip.broker_id,
        authorization_number=trip.authorization_number, return_of_trip_id=trip.return_of_trip_id,
        series_id=trip.series_id, is_late=late, at_risk=risk, en_route_at=trip.en_route_at,
        arrived_pickup_at=trip.arrived_pickup_at, picked_up_at=trip.picked_up_at,
        arrived_dropoff_at=trip.arrived_dropoff_at, completed_at=trip.completed_at,
        cancelled_at=trip.cancelled_at, no_show_at=trip.no_show_at, cancel_reason_code=trip.cancel_reason_code,
        late_cancel=trip.late_cancel, created_at=trip.created_at,
    )


async def load_outs(db: AsyncSession, company_id: UUID, ids: list[UUID]) -> list[TripOut]:
    rows = (await db.execute(trip_select().where(Trip.company_id == company_id, Trip.id.in_(ids))
                             .order_by(Trip.scheduled_pickup_at))).all()
    now = datetime.now(UTC)
    return [to_out(*r, now) for r in rows]


def to_driver_out(trip: Trip, patient: Patient, vehicle: Vehicle | None, now: datetime) -> DriverTripOut:
    return DriverTripOut(
        id=trip.id, trip_ref=f"T-{trip.trip_number:06d}", status=trip.status,
        patient_first_name=patient.first_name, patient_last_name=patient.last_name, patient_phone=patient.phone,
        mobility=trip.mobility, escort_count=trip.escort_count, uses_oxygen=trip.uses_oxygen,
        has_service_animal=trip.has_service_animal, special_instructions=_decrypt(trip.special_instructions_enc),
        pickup=place_out(trip, "pickup"), dropoff=place_out(trip, "dropoff"),
        scheduled_pickup_at=trip.scheduled_pickup_at, appointment_at=trip.appointment_at,
        vehicle_unit=vehicle.unit_number if vehicle else None, is_late=flags(trip, now)[0],
    )


async def lock_trip(db: AsyncSession, company_id: UUID, trip_id: UUID) -> Trip:
    """Fetch AND lock the row: two people changing one trip at once are made to queue."""
    trip = await db.scalar(select(Trip).where(Trip.id == trip_id, Trip.company_id == company_id).with_for_update())
    if trip is None:
        raise _err(404, "Trip not found")
    return trip


# ======================================================================= creating
async def patient_overlaps(db: AsyncSession, company_id: UUID, patient_id: UUID, start: datetime,
                           end: datetime, exclude: UUID | None = None) -> list[Trip]:
    q = select(Trip).where(Trip.company_id == company_id, Trip.patient_id == patient_id,
                           Trip.status.in_(NON_TERMINAL), Trip.scheduled_pickup_at < end, Trip.ends_at > start)
    if exclude:
        q = q.where(Trip.id != exclude)
    return list((await db.scalars(q)).all())


def _overlap_error(clashes: list[Trip]) -> HTTPException:
    return _err(409, {"code": "patient_overlap", "message": "The patient already has a trip at that time",
                      "trips": [f"T-{t.trip_number:06d}" for t in clashes]})


def _check_times(pickup: datetime, appointment: datetime | None, *, must_be_future: bool) -> None:
    if must_be_future and pickup <= datetime.now(UTC):
        raise _err(422, "scheduled_pickup_at must be in the future")
    if appointment is not None and appointment <= pickup:
        raise _err(422, "appointment_at must be after scheduled_pickup_at")


def _defaults_from_patient(patient: Patient, mobility, escorts, oxygen, animal) -> tuple[Any, int, bool, bool]:
    dob = date.fromisoformat(decrypt_field(patient.date_of_birth_enc))
    minor = _age(dob) < 18
    if escorts is None:
        escorts = 1 if (patient.requires_escort or minor) else 0
    if minor and escorts == 0:
        raise _err(422, "A minor must travel with an escort (escort_count >= 1)")
    return (mobility or patient.mobility, escorts,
            patient.uses_oxygen if oxygen is None else oxygen,
            patient.has_service_animal if animal is None else animal)


def build_trip(*, company_id: UUID, actor_id: UUID | None, patient: Patient, pickup: AddressIn, dropoff: AddressIn,
               number: int, pickup_at: datetime, appointment_at: datetime | None, duration: int, mobility: Any,
               escorts: int, oxygen: bool, animal: bool, instructions: str | None, broker_id: UUID | None,
               auth_no: str | None, leg: Leg, source: TripSource, return_of: UUID | None = None,
               series_id: UUID | None = None, occurrence_date: date | None = None) -> Trip:
    trip = Trip(
        id=new_id(), company_id=company_id, trip_number=number, status=S.SCHEDULED, leg=leg, source=source,
        patient_id=patient.id, broker_id=broker_id, authorization_number=auth_no, scheduled_pickup_at=pickup_at,
        appointment_at=appointment_at, estimated_duration_min=duration,
        ends_at=pickup_at + timedelta(minutes=duration), mobility=mobility, escort_count=escorts,
        uses_oxygen=oxygen, has_service_animal=animal,
        special_instructions_enc=encrypt_field(instructions) if instructions else None,
        return_of_trip_id=return_of, series_id=series_id, occurrence_date=occurrence_date, created_by=actor_id,
    )
    set_place(trip, "pickup", pickup)
    set_place(trip, "dropoff", dropoff)
    return trip


async def create_trips(db: AsyncSession, actor: User, body: TripCreate, request: Request) -> list[UUID]:
    cid = actor.company_id
    patient = await get_or_404(db, Patient, body.patient_id, cid, "Patient")
    if patient.archived_at is not None:
        raise _err(422, "Patient is archived")
    if body.broker_id:
        await get_or_404(db, Broker, body.broker_id, cid, "Broker")
    _check_times(body.scheduled_pickup_at, body.appointment_at, must_be_future=True)
    duration = body.estimated_duration_min or settings.default_trip_minutes
    mobility, escorts, oxygen, animal = _defaults_from_patient(
        patient, body.mobility, body.escort_count, body.uses_oxygen, body.has_service_animal)
    pickup = await resolve_place(db, cid, patient.id, body.pickup)
    dropoff = await resolve_place(db, cid, patient.id, body.dropoff)

    start = body.scheduled_pickup_at
    clashes = await patient_overlaps(db, cid, patient.id, start, start + timedelta(minutes=duration))
    if clashes:
        raise _overlap_error(clashes)

    common = dict(company_id=cid, actor_id=actor.id, patient=patient, duration=duration, mobility=mobility,
                  escorts=escorts, oxygen=oxygen, animal=animal, instructions=body.special_instructions,
                  broker_id=body.broker_id, auth_no=body.authorization_number, source=TripSource.DISPATCHER)
    outbound = build_trip(**common, pickup=pickup, dropoff=dropoff, number=await next_number(db, cid, "trip"),
                          pickup_at=start, appointment_at=body.appointment_at, leg=Leg.OUTBOUND)
    trips = [outbound]
    if body.round_trip:
        back_at = body.round_trip.return_pickup_at
        if back_at < outbound.ends_at:
            raise _err(422, "The return pickup must be after the outbound trip ends")
        clashes = await patient_overlaps(db, cid, patient.id, back_at, back_at + timedelta(minutes=duration))
        if clashes:
            raise _overlap_error(clashes)
        trips.append(build_trip(**common, pickup=dropoff, dropoff=pickup, number=await next_number(db, cid, "trip"),
                                pickup_at=back_at, appointment_at=None, leg=Leg.RETURN, return_of=outbound.id))
    for t in trips:
        db.add(t)
    await flush_or_409(db, "Could not save the trip")
    for t in trips:
        add_event(db, t, "created", actor.id, to_status=S.SCHEDULED, meta={"leg": t.leg.value})
        await audit.record(db, action="trip.create", actor=actor, resource_type="trip", resource_id=t.id, request=request)
    await db.commit()
    return [t.id for t in trips]


# ======================================================================= editing
def _trip_date(trip: Trip, tz: ZoneInfo) -> date:
    return trip.scheduled_pickup_at.astimezone(tz).date()


async def _driver_and_vehicle(db: AsyncSession, company_id: UUID, driver_id: UUID, vehicle_id: UUID):
    row = (await db.execute(select(Driver, User).join(User, and_(User.id == Driver.user_id, User.company_id == Driver.company_id))
                            .where(Driver.id == driver_id, Driver.company_id == company_id))).first()
    if row is None:
        raise _err(404, "Driver not found")
    vehicle = await get_or_404(db, Vehicle, vehicle_id, company_id, "Vehicle")
    return row[0], row[1], vehicle


async def _run_check(db: AsyncSession, trip: Trip, driver: Driver, driver_user: User, vehicle: Vehicle):
    tz = await company_tz(db, trip.company_id)
    return await conflicts.check_assignment(db, trip, driver, driver_user.is_active, vehicle, _trip_date(trip, tz))


def report_out(r: conflicts.Report) -> AssignmentReportOut:
    conv = lambda xs: [IssueOut(code=i.code, message=i.message, trip_id=i.trip_id) for i in xs]  # noqa: E731
    return AssignmentReportOut(ok=r.ok, errors=conv(r.errors), warnings=conv(r.warnings))


async def update_trip(db: AsyncSession, actor: User, trip_id: UUID, body: TripUpdate, request: Request) -> Trip:
    trip = await lock_trip(db, actor.company_id, trip_id)
    if trip.status not in EDITABLE:
        raise _err(409, f"A {trip.status.value} trip can no longer be edited")
    changes = body.model_dump(exclude_unset=True, exclude={"pickup", "dropoff", "special_instructions"})
    patient = await get_or_404(db, Patient, trip.patient_id, actor.company_id, "Patient")

    for key, value in changes.items():
        setattr(trip, key, value)
    if "special_instructions" in body.model_fields_set:
        trip.special_instructions_enc = encrypt_field(body.special_instructions) if body.special_instructions else None
    if body.pickup:
        set_place(trip, "pickup", await resolve_place(db, actor.company_id, patient.id, body.pickup))
    if body.dropoff:
        set_place(trip, "dropoff", await resolve_place(db, actor.company_id, patient.id, body.dropoff))

    timing = {"scheduled_pickup_at", "estimated_duration_min", "appointment_at"} & changes.keys()
    _check_times(trip.scheduled_pickup_at, trip.appointment_at, must_be_future="scheduled_pickup_at" in changes)
    trip.ends_at = trip.scheduled_pickup_at + timedelta(minutes=trip.estimated_duration_min)
    if timing:
        clashes = await patient_overlaps(db, actor.company_id, trip.patient_id, trip.scheduled_pickup_at, trip.ends_at, exclude=trip.id)
        if clashes:
            raise _overlap_error(clashes)
    if trip.status == S.ASSIGNED and ({"scheduled_pickup_at", "estimated_duration_min", "mobility", "escort_count"} & changes.keys()):
        driver, du, vehicle = await _driver_and_vehicle(db, actor.company_id, trip.driver_id, trip.vehicle_id)  # type: ignore[arg-type]
        await lock_keys(db, f"driver:{driver.id}", f"vehicle:{vehicle.id}")
        rep = await _run_check(db, trip, driver, du, vehicle)
        if not rep.ok:
            raise _err(409, {"message": "That change conflicts with the current driver/vehicle; unassign first",
                             **report_out(rep).model_dump(mode="json")})
    await flush_or_409(db, "Could not save the trip")
    add_event(db, trip, "updated", actor.id, meta={"fields": sorted(body.model_fields_set)})
    await audit.record(db, action="trip.update", actor=actor, resource_type="trip", resource_id=trip.id,
                       request=request, meta={"fields": sorted(body.model_fields_set)})
    await db.commit()
    return trip


# ======================================================================= assigning
async def lock_keys(db: AsyncSession, *keys: str) -> None:
    """Advisory locks: 'only one request may be deciding about driver X / vehicle Y at a time'.
    Taken in sorted order so two requests can never deadlock each other."""
    for key in sorted(keys):
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})


async def check_for(db: AsyncSession, actor: User, trip_id: UUID, driver_id: UUID, vehicle_id: UUID) -> AssignmentReportOut:
    trip = await get_or_404(db, Trip, trip_id, actor.company_id, "Trip")
    driver, du, vehicle = await _driver_and_vehicle(db, actor.company_id, driver_id, vehicle_id)
    return report_out(await _run_check(db, trip, driver, du, vehicle))


async def assign_trip(db: AsyncSession, actor: User, trip_id: UUID, driver_id: UUID, vehicle_id: UUID,
                      request: Request) -> Trip:
    trip = await lock_trip(db, actor.company_id, trip_id)
    if trip.status not in {S.SCHEDULED, S.ASSIGNED, S.EN_ROUTE_TO_PICKUP}:
        raise _err(409, f"A {trip.status.value} trip cannot be (re)assigned")
    driver, du, vehicle = await _driver_and_vehicle(db, actor.company_id, driver_id, vehicle_id)
    await lock_keys(db, f"driver:{driver.id}", f"vehicle:{vehicle.id}")  # serialise competing assignments
    rep = await _run_check(db, trip, driver, du, vehicle)
    if not rep.ok:
        raise _err(409, {"message": "Assignment refused", **report_out(rep).model_dump(mode="json")})
    previous = (trip.driver_id, trip.vehicle_id)
    before = trip.status
    trip.driver_id, trip.vehicle_id = driver.id, vehicle.id
    trip.status, trip.assigned_at, trip.assigned_by = S.ASSIGNED, datetime.now(UTC), actor.id
    trip.en_route_at = None
    await flush_or_409(db, "Could not assign the trip")
    reassigned = previous[0] is not None
    meta = {"driver_id": str(driver.id), "vehicle_id": str(vehicle.id),
            "previous_driver_id": str(previous[0]) if previous[0] else None,
            "warnings": [w.code for w in rep.warnings]}
    add_event(db, trip, "reassigned" if reassigned else "assigned", actor.id, from_status=before, to_status=S.ASSIGNED, meta=meta)
        # real-time: tell dispatchers and the driver
    driver_user_id = du.id
    await rt.trip_assigned(trip.company_id, trip.id, driver.id, vehicle.id, driver_user_id)
    await audit.record(db, action="trip.assign", actor=actor, resource_type="trip", resource_id=trip.id, request=request, meta=meta)
    await db.commit()
    return trip


async def unassign_trip(db: AsyncSession, actor: User, trip_id: UUID, request: Request) -> Trip:
    trip = await lock_trip(db, actor.company_id, trip_id)
    if trip.status not in {S.ASSIGNED, S.EN_ROUTE_TO_PICKUP}:
        raise _err(409, f"A {trip.status.value} trip cannot be unassigned")
    before, old_driver = trip.status, trip.driver_id
    trip.driver_id = trip.vehicle_id = trip.assigned_at = trip.assigned_by = trip.en_route_at = None  # type: ignore[assignment]
    trip.status = S.SCHEDULED
    await flush_or_409(db, "Could not unassign the trip")
    add_event(db, trip, "unassigned", actor.id, from_status=before, to_status=S.SCHEDULED, meta={"previous_driver_id": str(old_driver)})
    await audit.record(db, action="trip.unassign", actor=actor, resource_type="trip", resource_id=trip.id, request=request)
    await db.commit()
    return trip


# ======================================================================= life-cycle
_STAMPS = {S.EN_ROUTE_TO_PICKUP: "en_route_at", S.ARRIVED_AT_PICKUP: "arrived_pickup_at",
           S.PASSENGER_ON_BOARD: "picked_up_at", S.ARRIVED_AT_DROPOFF: "arrived_dropoff_at",
           S.COMPLETED: "completed_at", S.CANCELLED: "cancelled_at", S.NO_SHOW: "no_show_at"}


def apply_transition(db: AsyncSession, trip: Trip, target: TripStatus, actor_id: UUID | None, body: StatusIn,
                     *, by_driver: bool) -> None:
    """The ONE place a trip changes status. Both the dispatcher and the driver app come through here."""
    if not can_transition(trip.status, target):
        raise _err(409, f"A trip cannot go from '{trip.status.value}' to '{target.value}'")
    now = datetime.now(UTC)
    if target == S.NO_SHOW and by_driver:
        waited = now - (trip.arrived_pickup_at or now)
        if waited < timedelta(minutes=settings.no_show_wait_minutes):
            raise _err(409, f"Wait at least {settings.no_show_wait_minutes} minutes after arriving before marking a no-show")
    if target == S.CANCELLED:
        trip.cancel_reason_code = body.reason_code
        trip.late_cancel = now > trip.scheduled_pickup_at - timedelta(hours=settings.late_cancel_hours)
    before = trip.status
    trip.status = target
    if (column := _STAMPS.get(target)):          # 'en route to drop-off' has no column; its time lives in trip_events
        setattr(trip, column, now)
    add_event(db, trip, "status_changed", actor_id, from_status=before, to_status=target, lat=body.latitude,
              lng=body.longitude, reason=body.reason_code.value if body.reason_code else None, note=body.note)


async def change_status(db: AsyncSession, actor: User, trip_id: UUID, body: StatusIn, request: Request, *,
                        driver: Driver | None = None) -> Trip:
    """`driver` is set when the call comes from the driver app (stricter rules apply)."""
    trip = await lock_trip(db, actor.company_id, trip_id)
    if driver is not None and trip.driver_id != driver.id:
        raise _err(404, "Trip not found")           # not your trip = it doesn't exist for you
    if driver is not None and body.status not in DRIVER_TARGETS:
        raise _err(403, "Drivers cannot set that status; ask dispatch")
    if body.status not in STATUS_ENDPOINT_TARGETS:
        raise _err(422, "Use the assign / unassign endpoints for 'scheduled' and 'assigned'")
    apply_transition(db, trip, body.status, actor.id, body, by_driver=driver is not None)
    await audit.record(db, action="trip.status", actor=actor, resource_type="trip", resource_id=trip.id,
    request=request, meta={"to": body.status.value})
    await rt.trip_status_changed(trip.company_id, trip.id, body.status, trip.driver_id, trip.vehicle_id)
    await db.commit()
    return trip


# ======================================================================= recurring
@dataclass
class Skipped:
    date: date
    leg: str
    reason: str


def series_out(s: RecurringTrip) -> SeriesOut:
    return SeriesOut(
        id=s.id, patient_id=s.patient_id, broker_id=s.broker_id, authorization_number=s.authorization_number,
        rrule=s.rrule, start_date=s.start_date, end_date=s.end_date, timezone=s.timezone, pickup_time=s.pickup_time,
        appointment_time=s.appointment_time, return_pickup_time=s.return_pickup_time,
        estimated_duration_min=s.estimated_duration_min, mobility=s.mobility, escort_count=s.escort_count,
        uses_oxygen=s.uses_oxygen, has_service_animal=s.has_service_animal,
        special_instructions=_decrypt(s.special_instructions_enc), pickup=place_out(s, "pickup"),
        dropoff=place_out(s, "dropoff"), is_active=s.is_active, materialized_through=s.materialized_through,
        created_at=s.created_at,
    )


async def create_series(db: AsyncSession, actor: User, body: RecurringCreate, request: Request) -> RecurringTrip:
    cid = actor.company_id
    patient = await get_or_404(db, Patient, body.patient_id, cid, "Patient")
    if patient.archived_at is not None:
        raise _err(422, "Patient is archived")
    if body.broker_id:
        await get_or_404(db, Broker, body.broker_id, cid, "Broker")
    mobility, escorts, oxygen, animal = _defaults_from_patient(
        patient, body.mobility, body.escort_count, body.uses_oxygen, body.has_service_animal)
    duration = body.estimated_duration_min or settings.default_trip_minutes
    start_min = body.pickup_time.hour * 60 + body.pickup_time.minute
    if body.return_pickup_time and body.return_pickup_time.hour * 60 + body.return_pickup_time.minute < start_min + duration:
        raise _err(422, "return_pickup_time must be after the outbound trip ends")
    tz = await company_tz(db, cid)
    series = RecurringTrip(
        id=new_id(), company_id=cid, patient_id=patient.id, broker_id=body.broker_id,
        authorization_number=body.authorization_number, rrule=body.rrule, start_date=body.start_date,
        end_date=body.end_date, timezone=tz.key, pickup_time=body.pickup_time, appointment_time=body.appointment_time,
        return_pickup_time=body.return_pickup_time, estimated_duration_min=duration, mobility=mobility,
        escort_count=escorts, uses_oxygen=oxygen, has_service_animal=animal,
        special_instructions_enc=encrypt_field(body.special_instructions) if body.special_instructions else None,
        created_by=actor.id,
    )
    set_place(series, "pickup", await resolve_place(db, cid, patient.id, body.pickup))
    set_place(series, "dropoff", await resolve_place(db, cid, patient.id, body.dropoff))
    db.add(series)
    await flush_or_409(db, "Could not save the series")
    await audit.record(db, action="recurring.create", actor=actor, resource_type="recurring_trip",
                       resource_id=series.id, request=request)
    return series


async def materialize(db: AsyncSession, series: RecurringTrip, through: date, actor_id: UUID | None) -> tuple[int, list[Skipped]]:
    """Turn the recipe into real trips for every day up to `through`. Safe to run any number of times:
    a unique index on (series, day, leg) means the same day can never be created twice, and a trip a
    dispatcher cancelled stays cancelled instead of being re-created."""
    tz = ZoneInfo(series.timezone)
    lo = series.start_date if series.materialized_through is None else max(series.start_date, series.materialized_through + timedelta(days=1))
    hi = min(through, series.end_date) if series.end_date else through
    created, skipped = 0, []
    if not series.is_active or hi < lo:
        return created, skipped
    patient = await db.get(Patient, series.patient_id)
    existing = {(t.occurrence_date, t.leg): t.id for t in (await db.scalars(
        select(Trip).where(Trip.series_id == series.id, Trip.occurrence_date.between(lo, hi)))).all()}
    pickup, dropoff = place_as_address(series, "pickup"), place_as_address(series, "dropoff")
    now = datetime.now(UTC)
    for day in occurrences(series.rrule, series.start_date, lo, hi):
        out_id = existing.get((day, Leg.OUTBOUND))
        legs = [(Leg.OUTBOUND, series.pickup_time, series.appointment_time, pickup, dropoff)]
        if series.return_pickup_time:
            legs.append((Leg.RETURN, series.return_pickup_time, None, dropoff, pickup))
        for leg, at, appt, frm, to in legs:
            if (day, leg) in existing:
                continue
            pickup_at = local_to_utc(day, at, tz)
            if pickup_at <= now:
                skipped.append(Skipped(day, leg.value, "in_the_past"))
                continue
            end_at = pickup_at + timedelta(minutes=series.estimated_duration_min)
            if await patient_overlaps(db, series.company_id, series.patient_id, pickup_at, end_at):
                skipped.append(Skipped(day, leg.value, "patient_overlap"))
                continue
            trip = build_trip(
                company_id=series.company_id, actor_id=actor_id, patient=patient, pickup=frm, dropoff=to,  # type: ignore[arg-type]
                number=await next_number(db, series.company_id, "trip"), pickup_at=pickup_at,
                appointment_at=local_to_utc(day, appt, tz) if appt else None, duration=series.estimated_duration_min,
                mobility=series.mobility, escorts=series.escort_count, oxygen=series.uses_oxygen,
                animal=series.has_service_animal, instructions=_decrypt(series.special_instructions_enc),
                broker_id=series.broker_id, auth_no=series.authorization_number, leg=leg,
                source=TripSource.RECURRING, return_of=out_id if leg == Leg.RETURN else None,
                series_id=series.id, occurrence_date=day,
            )
            db.add(trip)
            await db.flush()
            if leg == Leg.OUTBOUND:
                out_id = trip.id
            add_event(db, trip, "created", actor_id, to_status=S.SCHEDULED, meta={"series_id": str(series.id)})
            created += 1
    series.materialized_through = hi if series.materialized_through is None else max(series.materialized_through, hi)
    return created, skipped


async def end_series(db: AsyncSession, actor: User, series_id: UUID, from_date: date, reason: Any, request: Request) -> int:
    series = await get_or_404(db, RecurringTrip, series_id, actor.company_id, "Recurring trip")
    trips = (await db.scalars(select(Trip).where(
        Trip.series_id == series.id, Trip.occurrence_date >= from_date,
        Trip.status.in_({S.SCHEDULED, S.ASSIGNED})).with_for_update())).all()
    body = StatusIn(status=S.CANCELLED, reason_code=reason)
    for t in trips:
        apply_transition(db, t, S.CANCELLED, actor.id, body, by_driver=False)
    if from_date <= series.start_date:
        series.is_active = False
    else:
        series.end_date = from_date - timedelta(days=1)
    await audit.record(db, action="recurring.end", actor=actor, resource_type="recurring_trip", resource_id=series.id,
                       request=request, meta={"cancelled": len(trips), "from": from_date.isoformat()})
    await db.commit()
    return len(trips)