from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.address import apply_address
from app.core.crud import Page, flush_or_409, get_or_404
from app.core.deps import get_db, require_permission
from app.core.geo import point
from app.core.permissions import Permission
from app.modules.audit import service as audit
from app.modules.facilities.models import Broker, Facility, FacilityKind
from app.modules.facilities.schemas import (
    BrokerCreate,
    BrokerOut,
    BrokerUpdate,
    FacilityCreate,
    FacilityNearbyOut,
    FacilityOut,
    FacilityUpdate,
)
from app.modules.users.models import User

router = APIRouter(prefix="/facilities", tags=["facilities"])
brokers_router = APIRouter(prefix="/brokers", tags=["brokers"])
F_READ = Depends(require_permission(Permission.FACILITIES_READ))
F_WRITE = Depends(require_permission(Permission.FACILITIES_WRITE))


@router.post("", response_model=FacilityOut, status_code=status.HTTP_201_CREATED)
async def create_facility(body: FacilityCreate, request: Request, actor: User = F_WRITE, db: AsyncSession = Depends(get_db)):
    fac = Facility(company_id=actor.company_id,
                   **body.model_dump(exclude={"address"}))
    await apply_address(fac, body.address)
    db.add(fac)
    await flush_or_409(db, "Could not save facility")
    await audit.record(db, action="facility.create", actor=actor, resource_type="facility",
                       resource_id=fac.id, request=request)
    await db.commit()
    await db.refresh(fac)
    return fac


@router.get("", response_model=list[FacilityOut])
async def list_facilities(
    q: str | None = Query(default=None, min_length=2, max_length=100),
    kind: FacilityKind | None = None,
    include_archived: bool = False,
    page: Page = Depends(),
    actor: User = F_READ,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Facility).where(Facility.company_id == actor.company_id)
    if not include_archived:
        stmt = stmt.where(Facility.archived_at.is_(None))
    if q:
        stmt = stmt.where(func.lower(Facility.name).contains(q.lower(), autoescape=True))
    if kind:
        stmt = stmt.where(Facility.kind == kind)
    return (await db.scalars(stmt.order_by(Facility.name, Facility.id).limit(page.limit).offset(page.offset))).all()


@router.get("/nearby", response_model=list[FacilityNearbyOut])
async def nearby_facilities(
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius_km: float = Query(default=10, gt=0, le=200),
    kind: FacilityKind | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    actor: User = F_READ,
    db: AsyncSession = Depends(get_db),
):
    """PostGIS: ST_DWithin on `geography` uses the GiST index and measures real-world METRES."""
    here = point(lat, lng)
    dist = func.ST_Distance(Facility.location, here).label("distance_m")
    stmt = (select(Facility, dist)
            .where(Facility.company_id == actor.company_id, Facility.archived_at.is_(None),
                   Facility.location.is_not(None), func.ST_DWithin(Facility.location, here, radius_km * 1000)))
    if kind:
        stmt = stmt.where(Facility.kind == kind)
    rows = (await db.execute(stmt.order_by(dist).limit(limit))).all()
    return [FacilityNearbyOut(**FacilityOut.model_validate(f).model_dump(), distance_m=round(d, 1)) for f, d in rows]


@router.get("/{facility_id}", response_model=FacilityOut)
async def get_facility(facility_id: UUID, actor: User = F_READ, db: AsyncSession = Depends(get_db)):
    return await get_or_404(db, Facility, facility_id, actor.company_id, "Facility")


@router.patch("/{facility_id}", response_model=FacilityOut)
async def update_facility(facility_id: UUID, body: FacilityUpdate, request: Request, actor: User = F_WRITE,
                          db: AsyncSession = Depends(get_db)):
    fac = await get_or_404(db, Facility, facility_id, actor.company_id, "Facility")
    changes = body.model_dump(exclude_unset=True, exclude={"address"})
    for k, v in changes.items():
        setattr(fac, k, v)
    if body.address:
        await apply_address(fac, body.address)
    await flush_or_409(db, "Could not save facility")
    await audit.record(db, action="facility.update", actor=actor, resource_type="facility", resource_id=fac.id,
                       request=request, meta={"fields": sorted(body.model_dump(exclude_unset=True))})
    await db.commit()
    await db.refresh(fac)
    return fac


@router.delete("/{facility_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_facility(facility_id: UUID, request: Request, actor: User = F_WRITE, db: AsyncSession = Depends(get_db)):
    fac = await get_or_404(db, Facility, facility_id, actor.company_id, "Facility")
    fac.archived_at = datetime.now(UTC)
    await audit.record(db, action="facility.archive", actor=actor, resource_type="facility",
                       resource_id=fac.id, request=request)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------- brokers ----------------
@brokers_router.post("", response_model=BrokerOut, status_code=status.HTTP_201_CREATED)
async def create_broker(body: BrokerCreate, request: Request, actor: User = F_WRITE, db: AsyncSession = Depends(get_db)):
    broker = Broker(company_id=actor.company_id, **body.model_dump())
    db.add(broker)
    await flush_or_409(db, "A broker with this code already exists")
    await audit.record(db, action="broker.create", actor=actor, resource_type="broker",
                       resource_id=broker.id, request=request)
    await db.commit()
    await db.refresh(broker)
    return broker


@brokers_router.get("", response_model=list[BrokerOut])
async def list_brokers(include_archived: bool = False, page: Page = Depends(), actor: User = F_READ,
                       db: AsyncSession = Depends(get_db)):
    stmt = select(Broker).where(Broker.company_id == actor.company_id)
    if not include_archived:
        stmt = stmt.where(Broker.archived_at.is_(None))
    return (await db.scalars(stmt.order_by(Broker.name).limit(page.limit).offset(page.offset))).all()


@brokers_router.get("/{broker_id}", response_model=BrokerOut)
async def get_broker(broker_id: UUID, actor: User = F_READ, db: AsyncSession = Depends(get_db)):
    return await get_or_404(db, Broker, broker_id, actor.company_id, "Broker")


@brokers_router.patch("/{broker_id}", response_model=BrokerOut)
async def update_broker(broker_id: UUID, body: BrokerUpdate, request: Request, actor: User = F_WRITE,
                        db: AsyncSession = Depends(get_db)):
    broker = await get_or_404(db, Broker, broker_id, actor.company_id, "Broker")
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        changes.pop("name")
    for k, v in changes.items():
        setattr(broker, k, v)
    await audit.record(db, action="broker.update", actor=actor, resource_type="broker", resource_id=broker.id,
                       request=request, meta={"fields": sorted(changes)})
    await db.commit()
    await db.refresh(broker)
    return broker


@brokers_router.delete("/{broker_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_broker(broker_id: UUID, request: Request, actor: User = F_WRITE, db: AsyncSession = Depends(get_db)):
    broker = await get_or_404(db, Broker, broker_id, actor.company_id, "Broker")
    broker.archived_at = datetime.now(UTC)
    await audit.record(db, action="broker.archive", actor=actor, resource_type="broker",
                       resource_id=broker.id, request=request)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)