from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud import Page, flush_or_409, get_or_404
from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.modules.audit import service as audit
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle, VehicleStatus, VehicleType
from app.modules.vehicles.schemas import VehicleCreate, VehicleOut, VehicleUpdate

router = APIRouter(prefix="/vehicles", tags=["vehicles / fleet"])
READ = Depends(require_permission(Permission.VEHICLES_READ))
WRITE = Depends(require_permission(Permission.VEHICLES_WRITE))
CONFLICT = "Unit number, VIN, or driver reference conflicts with existing data"


@router.post("", response_model=VehicleOut, status_code=status.HTTP_201_CREATED)
async def create_vehicle(body: VehicleCreate, request: Request, actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    vehicle = Vehicle(company_id=actor.company_id, **body.model_dump())
    db.add(vehicle)
    await flush_or_409(db, CONFLICT)
    await audit.record(db, action="vehicle.create", actor=actor, resource_type="vehicle",
                       resource_id=vehicle.id, request=request)
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


@router.get("", response_model=list[VehicleOut])
async def list_vehicles(
    status_: VehicleStatus | None = Query(default=None, alias="status"),
    vehicle_type: VehicleType | None = None,
    wheelchair_capable: bool | None = None,
    include_archived: bool = False,
    page: Page = Depends(),
    actor: User = READ,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Vehicle).where(Vehicle.company_id == actor.company_id)
    if not include_archived:
        stmt = stmt.where(Vehicle.archived_at.is_(None))
    if status_:
        stmt = stmt.where(Vehicle.status == status_)
    if vehicle_type:
        stmt = stmt.where(Vehicle.vehicle_type == vehicle_type)
    if wheelchair_capable is not None:
        stmt = stmt.where((Vehicle.wheelchair_capacity > 0) if wheelchair_capable else (Vehicle.wheelchair_capacity == 0))
    return (await db.scalars(stmt.order_by(Vehicle.unit_number).limit(page.limit).offset(page.offset))).all()


@router.get("/{vehicle_id}", response_model=VehicleOut)
async def get_vehicle(vehicle_id: UUID, actor: User = READ, db: AsyncSession = Depends(get_db)):
    return await get_or_404(db, Vehicle, vehicle_id, actor.company_id, "Vehicle")


@router.patch("/{vehicle_id}", response_model=VehicleOut)
async def update_vehicle(vehicle_id: UUID, body: VehicleUpdate, request: Request, actor: User = WRITE,
                         db: AsyncSession = Depends(get_db)):
    vehicle = await get_or_404(db, Vehicle, vehicle_id, actor.company_id, "Vehicle")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(vehicle, k, v)
    if vehicle.wheelchair_capacity and vehicle.equipment.value == "none":  # same rule as create
        raise HTTPException(422, "wheelchair capacity needs ramp or lift equipment")
    await flush_or_409(db, CONFLICT)
    await audit.record(db, action="vehicle.update", actor=actor, resource_type="vehicle", resource_id=vehicle.id,
                       request=request, meta={"fields": sorted(changes)})
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


@router.delete("/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
async def retire_vehicle(vehicle_id: UUID, request: Request, actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    vehicle = await get_or_404(db, Vehicle, vehicle_id, actor.company_id, "Vehicle")
    vehicle.status, vehicle.archived_at = VehicleStatus.RETIRED, datetime.now(UTC)
    await audit.record(db, action="vehicle.retire", actor=actor, resource_type="vehicle",
                       resource_id=vehicle.id, request=request)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)