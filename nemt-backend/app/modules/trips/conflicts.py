"""Can THIS driver + THIS vehicle take THIS trip?  Returns errors (block) and warnings (allow).

Analogy: an airline check-in desk. Before a seat is confirmed it checks: is the plane able to
carry this passenger (wheelchair spot? stretcher?), are the crew's and the plane's papers valid
on the day of the flight, is the crew already flying another plane at that hour, and (for a
shared ride) will everyone actually fit at the busiest moment.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.drivers.models import Driver
from app.modules.patients.models import MobilityType
from app.modules.trips.models import Trip
from app.modules.trips.state import OCCUPYING
from app.modules.vehicles.models import Vehicle, VehicleStatus

WHEELCHAIR = {MobilityType.WHEELCHAIR_MANUAL, MobilityType.WHEELCHAIR_POWER}
SOON_DAYS = 14  # a document expiring within this many days of the trip gets a warning


@dataclass
class Issue:
    code: str
    message: str
    trip_id: UUID | None = None


@dataclass
class Report:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class Load:
    seats: int
    wheelchair: int
    stretcher: int


def load_of(mobility: MobilityType, escorts: int) -> Load:
    """What a trip occupies in a vehicle. Escorts always need ordinary seats."""
    if mobility in WHEELCHAIR:
        return Load(escorts, 1, 0)
    if mobility == MobilityType.STRETCHER:
        return Load(escorts, 0, 1)
    return Load(1 + escorts, 0, 0)


def _over(total: Load, vehicle: Vehicle) -> bool:
    return (total.seats > vehicle.seat_capacity or total.wheelchair > vehicle.wheelchair_capacity
            or total.stretcher > vehicle.stretcher_capacity)


def _sum(loads: Iterable[Load]) -> Load:
    ls = list(loads)
    return Load(sum(x.seats for x in ls), sum(x.wheelchair for x in ls), sum(x.stretcher for x in ls))


def _peak_overload(trip: Trip, others: list[Trip], vehicle: Vehicle) -> bool:
    """Does the vehicle overflow at ANY moment? Checked at every pickup time inside the window.

    Adding up everyone who overlaps `trip` would be too pessimistic: A (10:00-10:45) and
    B (11:00-11:45) never share the van, so a trip spanning both only ever sits with one of them.
    """
    everyone = [trip, *others]
    for moment in {t.scheduled_pickup_at for t in everyone}:
        present = [t for t in everyone if t.scheduled_pickup_at <= moment < t.ends_at]
        if trip in present and _over(_sum(load_of(t.mobility, t.escort_count) for t in present), vehicle):
            return True
    return False


def _expiry(issues: Report, label: str, code: str, expires: date | None, trip_date: date) -> None:
    if expires is None:
        return
    if expires < trip_date:
        issues.errors.append(Issue(f"{code}_expired", f"{label} expires {expires}, before the trip on {trip_date}"))
    elif expires <= trip_date + timedelta(days=SOON_DAYS):
        issues.warnings.append(Issue(f"{code}_expiring", f"{label} expires {expires}, within {SOON_DAYS} days of the trip"))


async def check_assignment(
    db: AsyncSession, trip: Trip, driver: Driver, driver_active: bool, vehicle: Vehicle, trip_date: date
) -> Report:
    r = Report()
    need = load_of(trip.mobility, trip.escort_count)

    # --- 1. is each resource usable at all? -------------------------------------------------
    if driver.archived_at is not None or not driver_active:
        r.errors.append(Issue("driver_inactive", "Driver is not active"))
    if vehicle.archived_at is not None or vehicle.status != VehicleStatus.ACTIVE:
        r.errors.append(Issue("vehicle_not_active", f"Vehicle {vehicle.unit_number} is {vehicle.status.value}"))

    # --- 2. can they carry THIS passenger? -------------------------------------------------
    if _over(need, vehicle):
        r.errors.append(Issue("vehicle_cannot_carry",
                              f"Vehicle {vehicle.unit_number} cannot carry this trip (needs {need.seats} seat(s), "
                              f"{need.wheelchair} wheelchair spot(s), {need.stretcher} stretcher spot(s))"))
    if (trip.mobility in WHEELCHAIR or trip.mobility == MobilityType.STRETCHER) and not driver.wheelchair_certified:
        r.errors.append(Issue("driver_not_certified", "Driver is not certified for wheelchair/stretcher passengers"))

    # --- 3. are the papers valid ON THE DAY OF THE TRIP? -------------------------------------
    _expiry(r, "Driver's licence", "driver_license", driver.license_expires_on, trip_date)
    _expiry(r, "Driver's medical card", "driver_medical_cert", driver.medical_cert_expires_on, trip_date)
    _expiry(r, f"Vehicle {vehicle.unit_number} registration", "vehicle_registration", vehicle.registration_expires_on, trip_date)
    _expiry(r, f"Vehicle {vehicle.unit_number} insurance", "vehicle_insurance", vehicle.insurance_expires_on, trip_date)

    # --- 4. is anyone already busy? -----------------------------------------------------------
    buf = timedelta(minutes=settings.trip_buffer_minutes)
    busy = (await db.scalars(
        select(Trip).where(
            Trip.company_id == trip.company_id, Trip.id != trip.id, Trip.status.in_(OCCUPYING),
            or_(Trip.driver_id == driver.id, Trip.vehicle_id == vehicle.id),
            Trip.scheduled_pickup_at < trip.ends_at + buf, Trip.ends_at > trip.scheduled_pickup_at - buf,
        )
    )).all()
    shared: list[Trip] = []
    for other in busy:
        same_driver, same_vehicle = other.driver_id == driver.id, other.vehicle_id == vehicle.id
        truly_overlaps = other.scheduled_pickup_at < trip.ends_at and other.ends_at > trip.scheduled_pickup_at
        if same_driver and same_vehicle:
            if truly_overlaps:
                shared.append(other)
            else:
                r.warnings.append(Issue("tight_turnaround", f"Only a short gap after/before {_ref(other)}", other.id))
        elif same_driver:
            r.errors.append(Issue("driver_busy_other_vehicle",
                                  f"Driver is already on {_ref(other)} in a different vehicle at that time", other.id))
        else:
            r.errors.append(Issue("vehicle_busy_other_driver",
                                  f"Vehicle {vehicle.unit_number} is already on {_ref(other)} with a different driver", other.id))
    if shared:
        if _peak_overload(trip, shared, vehicle):
            r.errors.append(Issue("vehicle_over_capacity", "Sharing this ride would exceed the vehicle's capacity"))
        else:
            r.warnings.append(Issue("shared_ride", f"Shares the vehicle with {', '.join(_ref(t) for t in shared)}"))
    return r


def _ref(t: Trip) -> str:
    return f"T-{t.trip_number:06d}"