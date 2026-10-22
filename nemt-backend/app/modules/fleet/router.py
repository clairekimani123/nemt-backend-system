"""Fleet endpoints: maintenance, inspections, mileage, and the dashboard."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.modules.fleet import service
from app.modules.fleet.models import VehicleInspection, VehicleMaintenance, VehicleMileage
from app.modules.fleet.schemas import (
    InspectionIn,
    InspectionOut,
    MaintenanceIn,
    MaintenanceOut,
    MileageIn,
    MileageOut,
    VehicleFleetStatus,
)
from app.modules.users.models import User

router = APIRouter(prefix="/fleet", tags=["fleet"])
READ = Depends(require_permission(Permission.VEHICLES_READ))
WRITE = Depends(require_permission(Permission.VEHICLES_WRITE))


# ------------------------------------------------------------------ dashboard
@router.get("/dashboard", response_model=list[VehicleFleetStatus])
async def dashboard(include_retired: bool = False, actor: User = READ,
                    db: AsyncSession = Depends(get_db)):
    """One row per vehicle: mileage, next maintenance, expiring documents, alerts."""
    return await service.fleet_dashboard(db, actor.company_id, include_retired=include_retired)


# ------------------------------------------------------------------ maintenance
@router.get("/vehicles/{vehicle_id}/maintenance", response_model=list[MaintenanceOut])
async def list_maintenance(vehicle_id: UUID, limit: int = Query(50, ge=1, le=200),
                           actor: User = READ, db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(VehicleMaintenance).where(
        VehicleMaintenance.company_id == actor.company_id,
        VehicleMaintenance.vehicle_id == vehicle_id,
    ).order_by(VehicleMaintenance.service_date.desc()).limit(limit))).all()


@router.post("/vehicles/{vehicle_id}/maintenance", response_model=MaintenanceOut,
             status_code=status.HTTP_201_CREATED)
async def record_maintenance(vehicle_id: UUID, body: MaintenanceIn, request: Request,
                             actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    return await service.record_maintenance(db, actor, vehicle_id, body, request)


# ------------------------------------------------------------------ inspections
@router.get("/vehicles/{vehicle_id}/inspections", response_model=list[InspectionOut])
async def list_inspections(vehicle_id: UUID, limit: int = Query(50, ge=1, le=200),
                           actor: User = READ, db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(VehicleInspection).where(
        VehicleInspection.company_id == actor.company_id,
        VehicleInspection.vehicle_id == vehicle_id,
    ).order_by(VehicleInspection.inspected_on.desc()).limit(limit))).all()


@router.post("/vehicles/{vehicle_id}/inspections", response_model=InspectionOut,
             status_code=status.HTTP_201_CREATED)
async def record_inspection(vehicle_id: UUID, body: InspectionIn, request: Request,
                            actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    return await service.record_inspection(db, actor, vehicle_id, body, request)


# ------------------------------------------------------------------ mileage
@router.get("/vehicles/{vehicle_id}/mileage", response_model=list[MileageOut])
async def list_mileage(vehicle_id: UUID, limit: int = Query(100, ge=1, le=500),
                       actor: User = READ, db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(VehicleMileage).where(
        VehicleMileage.company_id == actor.company_id,
        VehicleMileage.vehicle_id == vehicle_id,
    ).order_by(VehicleMileage.recorded_at.desc()).limit(limit))).all()


@router.post("/vehicles/{vehicle_id}/mileage", response_model=MileageOut,
             status_code=status.HTTP_201_CREATED)
async def record_mileage(vehicle_id: UUID, body: MileageIn, request: Request,
                         actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    return await service.record_mileage(db, actor, vehicle_id, body, request)