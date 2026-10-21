"""Operational reporting endpoints."""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.modules.reporting import service
from app.modules.reporting.schemas import (
    CancellationReport, DriverUtilizationReport, LeaderboardReport,
    NoShowReport, OnTimeReport, RevenueProductivityOut,
    VehicleUtilizationReport, VolumeReport,
)
from app.modules.users.models import User

router = APIRouter(prefix="/reports", tags=["reports"])
READ = Depends(require_permission(Permission.REPORTS_READ))


@router.get("/on-time", response_model=OnTimeReport)
async def on_time(from_date: date | None = Query(default=None, alias="from"),
                  to_date: date | None = Query(default=None, alias="to"),
                  actor: User = READ, db: AsyncSession = Depends(get_db)):
    """On-time pickup performance overall and by driver. Defaults to the last 7 days."""
    return await service.on_time_report(db, actor.company_id, from_date, to_date)


@router.get("/cancellations", response_model=CancellationReport)
async def cancellations(from_date: date | None = Query(default=None, alias="from"),
                        to_date: date | None = Query(default=None, alias="to"),
                        actor: User = READ, db: AsyncSession = Depends(get_db)):
    return await service.cancellation_report(db, actor.company_id, from_date, to_date)


@router.get("/no-shows", response_model=NoShowReport)
async def no_shows(from_date: date | None = Query(default=None, alias="from"),
                   to_date: date | None = Query(default=None, alias="to"),
                   actor: User = READ, db: AsyncSession = Depends(get_db)):
    return await service.no_show_report(db, actor.company_id, from_date, to_date)


@router.get("/volume", response_model=VolumeReport)
async def volume(from_date: date | None = Query(default=None, alias="from"),
                 to_date: date | None = Query(default=None, alias="to"),
                 actor: User = READ, db: AsyncSession = Depends(get_db)):
    return await service.volume_report(db, actor.company_id, from_date, to_date)


@router.get("/utilization/drivers", response_model=DriverUtilizationReport)
async def driver_util(from_date: date | None = Query(default=None, alias="from"),
                      to_date: date | None = Query(default=None, alias="to"),
                      actor: User = READ, db: AsyncSession = Depends(get_db)):
    return await service.driver_utilization(db, actor.company_id, from_date, to_date)


@router.get("/utilization/vehicles", response_model=VehicleUtilizationReport)
async def vehicle_util(from_date: date | None = Query(default=None, alias="from"),
                       to_date: date | None = Query(default=None, alias="to"),
                       actor: User = READ, db: AsyncSession = Depends(get_db)):
    return await service.vehicle_utilization(db, actor.company_id, from_date, to_date)


@router.get("/revenue-productivity", response_model=RevenueProductivityOut)
async def revenue_productivity(from_date: date | None = Query(default=None, alias="from"),
                               to_date: date | None = Query(default=None, alias="to"),
                               actor: User = READ, db: AsyncSession = Depends(get_db)):
    return await service.revenue_productivity(db, actor.company_id, from_date, to_date)


@router.get("/leaderboard", response_model=LeaderboardReport)
async def leaderboard(from_date: date | None = Query(default=None, alias="from"),
                      to_date: date | None = Query(default=None, alias="to"),
                      limit: int = Query(20, ge=1, le=100),
                      actor: User = READ, db: AsyncSession = Depends(get_db)):
    return await service.leaderboard(db, actor.company_id, from_date, to_date, limit)