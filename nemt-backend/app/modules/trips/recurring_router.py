from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crud import Page, get_or_404
from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.modules.audit import service as audit
from app.modules.trips import service
from app.modules.trips.models import RecurringTrip
from app.modules.trips.schemas import (
    MaterializeOut,
    RecurringCreate,
    RecurringEndIn,
    RecurringUpdate,
    SeriesCreatedOut,
    SeriesOut,
    SkippedOut,
)
from app.modules.users.models import User

router = APIRouter(prefix="/recurring-trips", tags=["recurring trips"])
READ = Depends(require_permission(Permission.TRIPS_READ))
WRITE = Depends(require_permission(Permission.TRIPS_WRITE))
MAX_DAYS_AHEAD = 400


def _result(created: int, skipped: list[service.Skipped]) -> MaterializeOut:
    return MaterializeOut(created=created, skipped=[SkippedOut(date=s.date, leg=s.leg, reason=s.reason) for s in skipped])


@router.post("", response_model=SeriesCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_series(body: RecurringCreate, request: Request, actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    series = await service.create_series(db, actor, body, request)
    created, skipped = 0, []
    if body.materialize_now:
        horizon = datetime.now(UTC).date() + timedelta(days=settings.recurring_horizon_days)
        created, skipped = await service.materialize(db, series, horizon, actor.id)
    await db.commit()
    await db.refresh(series)
    return SeriesCreatedOut(series=service.series_out(series), generated=_result(created, skipped))


@router.get("", response_model=list[SeriesOut])
async def list_series(patient_id: UUID | None = None, active: bool | None = None, page: Page = Depends(),
                      actor: User = READ, db: AsyncSession = Depends(get_db)):
    stmt = select(RecurringTrip).where(RecurringTrip.company_id == actor.company_id)
    if patient_id:
        stmt = stmt.where(RecurringTrip.patient_id == patient_id)
    if active is not None:
        stmt = stmt.where(RecurringTrip.is_active.is_(active))
    rows = (await db.scalars(stmt.order_by(RecurringTrip.created_at.desc()).limit(page.limit).offset(page.offset))).all()
    return [service.series_out(s) for s in rows]


@router.get("/{series_id}", response_model=SeriesOut)
async def get_series(series_id: UUID, actor: User = READ, db: AsyncSession = Depends(get_db)):
    return service.series_out(await get_or_404(db, RecurringTrip, series_id, actor.company_id, "Recurring trip"))


@router.patch("/{series_id}", response_model=SeriesOut)
async def update_series(series_id: UUID, body: RecurringUpdate, request: Request, actor: User = WRITE,
                        db: AsyncSession = Depends(get_db)):
    """Only the end date and on/off switch can change. To change TIMES or PLACES: end this series
    and create a new one (so history stays truthful about what ran under which schedule)."""
    series = await get_or_404(db, RecurringTrip, series_id, actor.company_id, "Recurring trip")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("is_active", True) is None:
        raise HTTPException(422, "is_active cannot be null")
    for k, v in changes.items():
        setattr(series, k, v)
    if series.end_date and series.end_date < series.start_date:
        raise HTTPException(422, "end_date is before start_date")
    await audit.record(db, action="recurring.update", actor=actor, resource_type="recurring_trip",
                       resource_id=series.id, request=request, meta={"fields": sorted(changes)})
    await db.commit()
    await db.refresh(series)
    return service.series_out(series)


@router.post("/{series_id}/materialize", response_model=MaterializeOut)
async def materialize(series_id: UUID, request: Request, until: date | None = None, actor: User = WRITE,
                      db: AsyncSession = Depends(get_db)):
    """Create real trips up to `until` (default: the normal look-ahead). Idempotent."""
    series = await get_or_404(db, RecurringTrip, series_id, actor.company_id, "Recurring trip")
    today = datetime.now(UTC).date()
    until = until or today + timedelta(days=settings.recurring_horizon_days)
    if until > today + timedelta(days=MAX_DAYS_AHEAD):
        raise HTTPException(422, f"until may be at most {MAX_DAYS_AHEAD} days ahead")
    created, skipped = await service.materialize(db, series, until, actor.id)
    await audit.record(db, action="recurring.materialize", actor=actor, resource_type="recurring_trip",
                       resource_id=series.id, request=request, meta={"created": created, "skipped": len(skipped)})
    await db.commit()
    return _result(created, skipped)


@router.post("/{series_id}/end")
async def end_series(series_id: UUID, body: RecurringEndIn, request: Request, actor: User = WRITE,
                     db: AsyncSession = Depends(get_db)):
    """Stop the series from `from_date` and cancel its not-yet-started trips from that day on."""
    cancelled = await service.end_series(db, actor, series_id, body.from_date, body.reason_code, request)
    return {"cancelled_trips": cancelled}