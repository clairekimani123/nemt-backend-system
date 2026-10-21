"""Fleet service: CRUD + dashboard aggregation."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base import new_id
from app.core.crud import get_or_404
from app.modules.audit import service as audit
from app.modules.fleet.models import (
    InspectionResult, VehicleInspection, VehicleMaintenance, VehicleMileage,
)
from app.modules.fleet.schemas import (
    VehicleFleetStatus,
)
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle

DASHBOARD_ALERT_WINDOW_DAYS = 30
DASHBOARD_MAINTENANCE_STALE_DAYS = 180


def _http(code: int, detail: object) -> HTTPException:
    return HTTPException(code, detail)


# ------------------------------------------------------------------ maintenance
async def record_maintenance(db: AsyncSession, actor: User, vehicle_id: UUID,
                             body, request: Request) -> VehicleMaintenance:
    await get_or_404(db, Vehicle, vehicle_id, actor.company_id, "Vehicle")
    record = VehicleMaintenance(
        id=new_id(), company_id=actor.company_id, vehicle_id=vehicle_id,
        created_by=actor.id, **body.model_dump(),
    )
    db.add(record)
    await db.flush()
    await audit.record(db, action="fleet.maintenance_recorded", actor=actor,
                       resource_type="vehicle", resource_id=vehicle_id, request=request,
                       meta={"kind": body.kind.value, "service_date": body.service_date.isoformat()})
    await db.commit()
    await db.refresh(record)
    return record


# ------------------------------------------------------------------ inspections
async def record_inspection(db: AsyncSession, actor: User, vehicle_id: UUID,
                            body, request: Request) -> VehicleInspection:
    await get_or_404(db, Vehicle, vehicle_id, actor.company_id, "Vehicle")
    record = VehicleInspection(
        id=new_id(), company_id=actor.company_id, vehicle_id=vehicle_id,
        created_by=actor.id, **body.model_dump(),
    )
    db.add(record)
    await db.flush()
    await audit.record(db, action="fleet.inspection_recorded", actor=actor,
                       resource_type="vehicle", resource_id=vehicle_id, request=request,
                       meta={"kind": body.kind.value, "result": body.result.value})
    await db.commit()
    await db.refresh(record)
    return record


# ------------------------------------------------------------------ mileage
async def record_mileage(db: AsyncSession, actor: User, vehicle_id: UUID,
                         body, request: Request) -> VehicleMileage:
    await get_or_404(db, Vehicle, vehicle_id, actor.company_id, "Vehicle")
    record = VehicleMileage(
        company_id=actor.company_id, vehicle_id=vehicle_id, reading_km=body.reading_km,
        recorded_at=body.recorded_at or datetime.now(UTC), note=body.note,
        created_by=actor.id, source="manual",
    )
    db.add(record)
    await db.flush()
    await audit.record(db, action="fleet.mileage_recorded", actor=actor,
                       resource_type="vehicle", resource_id=vehicle_id, request=request,
                       meta={"reading_km": body.reading_km})
    await db.commit()
    await db.refresh(record)
    return record


# ------------------------------------------------------------------ dashboard
async def fleet_dashboard(db: AsyncSession, company_id: UUID, *,
                          include_retired: bool = False) -> list[VehicleFleetStatus]:
    """One object per vehicle, with everything a fleet manager needs to see.

    This is deliberately a wide read: one SQL query for vehicles, then bulk-load
    the latest maintenance / inspection / mileage per vehicle. Avoids N+1 while
    keeping each vehicle's status independently reconstructible.
    """
    vehicle_stmt = select(Vehicle).where(Vehicle.company_id == company_id)
    if not include_retired:
        vehicle_stmt = vehicle_stmt.where(Vehicle.archived_at.is_(None))
    vehicles = list((await db.scalars(vehicle_stmt.order_by(Vehicle.unit_number))).all())
    if not vehicles:
        return []

    ids = [v.id for v in vehicles]

    # Latest maintenance per vehicle
    latest_maint_rows = (await db.execute(
        select(VehicleMaintenance)
        .where(VehicleMaintenance.company_id == company_id,
               VehicleMaintenance.vehicle_id.in_(ids))
        .order_by(VehicleMaintenance.vehicle_id, desc(VehicleMaintenance.service_date),
                  desc(VehicleMaintenance.created_at))
    )).scalars().all()
    latest_maint: dict[UUID, VehicleMaintenance] = {}
    for row in latest_maint_rows:
        latest_maint.setdefault(row.vehicle_id, row)

    # Latest inspection per vehicle
    latest_insp_rows = (await db.execute(
        select(VehicleInspection)
        .where(VehicleInspection.company_id == company_id,
               VehicleInspection.vehicle_id.in_(ids))
        .order_by(VehicleInspection.vehicle_id, desc(VehicleInspection.inspected_on),
                  desc(VehicleInspection.created_at))
    )).scalars().all()
    latest_insp: dict[UUID, VehicleInspection] = {}
    for row in latest_insp_rows:
        latest_insp.setdefault(row.vehicle_id, row)

    # Latest mileage per vehicle
    latest_mileage_rows = (await db.execute(
        select(VehicleMileage)
        .where(VehicleMileage.company_id == company_id,
               VehicleMileage.vehicle_id.in_(ids))
        .order_by(VehicleMileage.vehicle_id, desc(VehicleMileage.recorded_at))
    )).scalars().all()
    latest_mileage: dict[UUID, VehicleMileage] = {}
    for row in latest_mileage_rows:
        latest_mileage.setdefault(row.vehicle_id, row)

    today = date.today()
    alert_cutoff = today + timedelta(days=DASHBOARD_ALERT_WINDOW_DAYS)

    out: list[VehicleFleetStatus] = []
    for v in vehicles:
        m = latest_maint.get(v.id)
        i = latest_insp.get(v.id)
        mm = latest_mileage.get(v.id)
        alerts: list[str] = []
        days_since_maint = (today - m.service_date).days if m else None

        if m and days_since_maint is not None and days_since_maint > DASHBOARD_MAINTENANCE_STALE_DAYS:
            alerts.append("maintenance_overdue")
        if m and m.next_due_date and m.next_due_date <= alert_cutoff:
            alerts.append("maintenance_due_soon")
        if m and m.next_due_km and mm and mm.reading_km and mm.reading_km >= m.next_due_km:
            alerts.append("maintenance_km_exceeded")
        if v.registration_expires_on and v.registration_expires_on <= alert_cutoff:
            alerts.append("registration_expiring")
        if v.insurance_expires_on and v.insurance_expires_on <= alert_cutoff:
            alerts.append("insurance_expiring")
        if i and i.expires_on <= alert_cutoff:
            alerts.append("inspection_expiring")
        if not i:
            alerts.append("no_inspection_on_file")
        if i and i.result == InspectionResult.FAIL:
            alerts.append("last_inspection_failed")

        out.append(VehicleFleetStatus(
            vehicle_id=v.id,
            unit_number=v.unit_number,
            status=v.status.value,
            make=v.make, model=v.model, year=v.year, plate=v.plate,
            current_mileage_km=mm.reading_km if mm else None,
            mileage_updated_at=mm.recorded_at if mm else None,
            last_maintenance_date=m.service_date if m else None,
            last_maintenance_kind=m.kind if m else None,
            next_maintenance_due_date=m.next_due_date if m else None,
            next_maintenance_due_km=m.next_due_km if m else None,
            days_since_last_maintenance=days_since_maint,
            last_inspection_date=i.inspected_on if i else None,
            last_inspection_result=i.result if i else None,
            next_inspection_expires_on=i.expires_on if i else None,
            registration_expires_on=v.registration_expires_on,
            insurance_expires_on=v.insurance_expires_on,
            days_until_registration_expiry=(v.registration_expires_on - today).days
                if v.registration_expires_on else None,
            days_until_insurance_expiry=(v.insurance_expires_on - today).days
                if v.insurance_expires_on else None,
            days_until_inspection_expiry=(i.expires_on - today).days if i else None,
            alerts=alerts,
        ))
    return out