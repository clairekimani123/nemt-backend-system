"""Dashboard numbers for one company. Counts only: no names, no PHI."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.modules.drivers.models import Driver
from app.modules.facilities.models import Facility
from app.modules.patients.models import Patient
from app.modules.trips import service as trips
from app.modules.trips.models import Trip
from app.modules.trips.state import TripSource, TripStatus
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle

router = APIRouter(prefix="/stats", tags=["dashboard"])


class SummaryOut(BaseModel):
    users_by_role: dict[str, int]
    patients: int
    self_registered_patients: int
    drivers: int
    vehicles_by_status: dict[str, int]
    facilities: int
    trips_today_by_status: dict[str, int]
    unassigned_upcoming: int
    passenger_requests_pending: int


@router.get("/summary", response_model=SummaryOut)
async def summary(actor: User = Depends(require_permission(Permission.REPORTS_READ)),
                  db: AsyncSession = Depends(get_db)):
    cid = actor.company_id
    now = datetime.now(UTC)
    tz = await trips.company_tz(db, cid)
    start, end = trips.day_bounds(now.astimezone(tz).date(), tz)

    async def count(stmt) -> int:  # type: ignore[no-untyped-def]
        return await db.scalar(stmt) or 0

    roles = (await db.execute(select(User.role, func.count()).where(User.company_id == cid, User.is_active.is_(True))
                              .group_by(User.role))).all()
    vehicles = (await db.execute(select(Vehicle.status, func.count()).where(Vehicle.company_id == cid)
                                 .group_by(Vehicle.status))).all()
    today = (await db.execute(select(Trip.status, func.count()).where(
        Trip.company_id == cid, Trip.scheduled_pickup_at >= start, Trip.scheduled_pickup_at < end)
        .group_by(Trip.status))).all()
    active_patients = select(func.count()).select_from(Patient).where(Patient.company_id == cid,
                                                                      Patient.archived_at.is_(None))
    unassigned = select(func.count()).select_from(Trip).where(
        Trip.company_id == cid, Trip.status == TripStatus.SCHEDULED, Trip.scheduled_pickup_at >= now)
    return SummaryOut(
        users_by_role={r.value: n for r, n in roles},
        patients=await count(active_patients),
        self_registered_patients=await count(active_patients.where(Patient.user_id.is_not(None))),
        drivers=await count(select(func.count()).select_from(Driver).where(Driver.company_id == cid,
                                                                           Driver.archived_at.is_(None))),
        vehicles_by_status={s.value: n for s, n in vehicles},
        facilities=await count(select(func.count()).select_from(Facility).where(Facility.company_id == cid,
                                                                                Facility.archived_at.is_(None))),
        trips_today_by_status={s.value: n for s, n in today},
        unassigned_upcoming=await count(unassigned),
        passenger_requests_pending=await count(unassigned.where(Trip.source == TripSource.PASSENGER_PORTAL)),
    )
