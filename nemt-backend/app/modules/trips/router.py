from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crud import Page, get_or_404
from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.modules.audit import service as audit
from app.modules.trips import service
from app.modules.trips.models import Trip, TripEvent
from app.modules.trips.schemas import (
    AssignIn,
    AssignmentReportOut,
    BoardOut,
    StatusIn,
    TripCreate,
    TripEventOut,
    TripOut,
    TripUpdate,
)
from app.modules.trips.state import PRE_PICKUP, TripStatus
from app.modules.users.models import User

router = APIRouter(prefix="/trips", tags=["trips"])
board_router = APIRouter(prefix="/dispatch", tags=["dispatch"])
READ = Depends(require_permission(Permission.TRIPS_READ))
WRITE = Depends(require_permission(Permission.TRIPS_WRITE))
DISPATCH = Depends(require_permission(Permission.DISPATCH))


async def _one(db: AsyncSession, actor: User, trip_id: UUID) -> TripOut:
    return (await service.load_outs(db, actor.company_id, [trip_id]))[0]


@router.post("", response_model=list[TripOut], status_code=status.HTTP_201_CREATED)
async def create_trip(body: TripCreate, request: Request, actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    """Creates one trip, or two (outbound + return) when `round_trip` is given."""
    ids = await service.create_trips(db, actor, body, request)
    return await service.load_outs(db, actor.company_id, ids)


@router.get("", response_model=list[TripOut])
async def list_trips(
    request: Request,
    on_date: date | None = Query(default=None, description="a calendar day in the COMPANY's timezone"),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    status_: TripStatus | None = Query(default=None, alias="status"),
    driver_id: UUID | None = None,
    vehicle_id: UUID | None = None,
    patient_id: UUID | None = None,
    unassigned: bool | None = None,
    late: bool | None = None,
    page: Page = Depends(),
    actor: User = READ,
    db: AsyncSession = Depends(get_db),
):
    stmt = service.trip_select().where(Trip.company_id == actor.company_id)
    if on_date:
        start, end = service.day_bounds(on_date, await service.company_tz(db, actor.company_id))
        stmt = stmt.where(Trip.scheduled_pickup_at >= start, Trip.scheduled_pickup_at < end)
    if from_:
        stmt = stmt.where(Trip.scheduled_pickup_at >= from_)
    if to:
        stmt = stmt.where(Trip.scheduled_pickup_at < to)
    if status_:
        stmt = stmt.where(Trip.status == status_)
    if driver_id:
        stmt = stmt.where(Trip.driver_id == driver_id)
    if vehicle_id:
        stmt = stmt.where(Trip.vehicle_id == vehicle_id)
    if patient_id:
        stmt = stmt.where(Trip.patient_id == patient_id)
    if unassigned is not None:
        # 'unassigned' = still WAITING for a driver (a cancelled trip never needed one)
        stmt = stmt.where(Trip.status == TripStatus.SCHEDULED if unassigned else Trip.driver_id.is_not(None))
    if late:
        cutoff = datetime.now(UTC) - timedelta(minutes=settings.late_grace_minutes)
        stmt = stmt.where(Trip.status.in_(PRE_PICKUP), Trip.scheduled_pickup_at < cutoff)
    rows = (await db.execute(stmt.order_by(Trip.scheduled_pickup_at, Trip.trip_number)
                             .limit(page.limit).offset(page.offset))).all()
    await audit.record(db, action="trip.list", actor=actor, resource_type="trip", request=request,
                       meta={"count": len(rows)})
    await db.commit()
    now = datetime.now(UTC)
    return [service.to_out(*r, now) for r in rows]


@router.get("/{trip_id}", response_model=TripOut)
async def get_trip(trip_id: UUID, request: Request, actor: User = READ, db: AsyncSession = Depends(get_db)):
    await get_or_404(db, Trip, trip_id, actor.company_id, "Trip")
    await audit.record(db, action="trip.read", actor=actor, resource_type="trip", resource_id=trip_id, request=request)
    await db.commit()
    return await _one(db, actor, trip_id)


@router.get("/{trip_id}/events", response_model=list[TripEventOut])
async def trip_history(trip_id: UUID, actor: User = READ, db: AsyncSession = Depends(get_db)):
    await get_or_404(db, Trip, trip_id, actor.company_id, "Trip")
    return (await db.scalars(select(TripEvent).where(TripEvent.trip_id == trip_id, TripEvent.company_id == actor.company_id)
                             .order_by(TripEvent.id))).all()


@router.patch("/{trip_id}", response_model=TripOut)
async def update_trip(trip_id: UUID, body: TripUpdate, request: Request, actor: User = WRITE,
                      db: AsyncSession = Depends(get_db)):
    await service.update_trip(db, actor, trip_id, body, request)
    return await _one(db, actor, trip_id)


@router.get("/{trip_id}/assignment-check", response_model=AssignmentReportOut)
async def assignment_check(trip_id: UUID, driver_id: UUID, vehicle_id: UUID, actor: User = DISPATCH,
                           db: AsyncSession = Depends(get_db)):
    """A dry run for the dispatch board: 'what would happen if I dropped this driver on this trip?'"""
    return await service.check_for(db, actor, trip_id, driver_id, vehicle_id)


@router.post("/{trip_id}/assign", response_model=TripOut)
async def assign(trip_id: UUID, body: AssignIn, request: Request, actor: User = DISPATCH, db: AsyncSession = Depends(get_db)):
    await service.assign_trip(db, actor, trip_id, body.driver_id, body.vehicle_id, request)
    return await _one(db, actor, trip_id)


@router.post("/{trip_id}/unassign", response_model=TripOut)
async def unassign(trip_id: UUID, request: Request, actor: User = DISPATCH, db: AsyncSession = Depends(get_db)):
    await service.unassign_trip(db, actor, trip_id, request)
    return await _one(db, actor, trip_id)


@router.post("/{trip_id}/status", response_model=TripOut)
async def set_status(trip_id: UUID, body: StatusIn, request: Request, actor: User = WRITE,
                     db: AsyncSession = Depends(get_db)):
    """Dispatcher moves a trip along (or cancels / marks no-show). Drivers use /driver/trips/..."""
    await service.change_status(db, actor, trip_id, body, request)
    return await _one(db, actor, trip_id)


# ------------------------------------------------------------------ dispatch board
LANES = {
    "unassigned": {TripStatus.SCHEDULED},
    "assigned": {TripStatus.ASSIGNED, TripStatus.EN_ROUTE_TO_PICKUP, TripStatus.ARRIVED_AT_PICKUP},
    "in_progress": {TripStatus.PASSENGER_ON_BOARD, TripStatus.EN_ROUTE_TO_DROPOFF, TripStatus.ARRIVED_AT_DROPOFF},
    "completed": {TripStatus.COMPLETED},
    "cancelled": {TripStatus.CANCELLED},
    "no_show": {TripStatus.NO_SHOW},
}


@board_router.get("/board", response_model=BoardOut)
async def board(on_date: date, actor: User = READ, db: AsyncSession = Depends(get_db)):
    """One screen for the day: lanes of trips + counts + how many are running late."""
    start, end = service.day_bounds(on_date, await service.company_tz(db, actor.company_id))
    rows = (await db.execute(service.trip_select().where(
        and_(Trip.company_id == actor.company_id, Trip.scheduled_pickup_at >= start, Trip.scheduled_pickup_at < end))
        .order_by(Trip.scheduled_pickup_at, Trip.trip_number).limit(500))).all()
    now = datetime.now(UTC)
    outs = [service.to_out(*r, now) for r in rows]
    lanes = {name: [o for o in outs if o.status in members] for name, members in LANES.items()}
    return BoardOut(on_date=on_date, counts={k: len(v) for k, v in lanes.items()},
                    late=sum(o.is_late for o in outs), lanes=lanes)