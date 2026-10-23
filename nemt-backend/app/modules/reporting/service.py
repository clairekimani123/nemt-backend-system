"""Operational reporting.

All read-only. Every query is tenant-scoped by RLS, and every date boundary is computed
in the company's local timezone so a "day" means the operator's day, not UTC's.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

# from app.core.crud import get_or_404
from app.modules.billing.models import Claim
from app.modules.drivers.models import Driver
from app.modules.fleet.models import VehicleMaintenance

# from app.modules.patients.models import MobilityType
from app.modules.reporting.schemas import (
    BreakdownRow,
    CancellationReport,
    CancellationRow,
    DriverUtilizationReport,
    LeaderboardReport,
    LeaderboardRow,
    NoShowReport,
    OnTimeReport,
    OnTimeRow,
    PeriodOut,
    RevenueProductivityOut,
    UtilizationRow,
    VehicleUtilizationReport,
    VehicleUtilizationRow,
    VolumeBucket,
    VolumeReport,
)
from app.modules.trips.models import Trip, TripEvent
from app.modules.trips.service import company_tz, day_bounds
from app.modules.trips.state import TripStatus
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle

S = TripStatus
TERMINAL = {S.COMPLETED, S.CANCELLED, S.NO_SHOW}
PRE_PICKUP = {S.SCHEDULED, S.ASSIGNED, S.EN_ROUTE_TO_PICKUP}


def _http(code: int, detail: object) -> Exception:
    from fastapi import HTTPException
    return HTTPException(code, detail)


def _default_range(from_date: date | None, to_date: date | None) -> tuple[date, date]:
    """Last 7 days inclusive when either bound is missing."""
    today = date.today()
    if to_date is None:
        to_date = today
    if from_date is None:
        from_date = to_date - timedelta(days=6)
    if to_date < from_date:
        raise _http(422, "'to' must be on or after 'from'")
    return from_date, to_date


async def _bounds(db: AsyncSession, company_id: UUID, from_date: date, to_date: date) -> tuple[datetime, datetime]:
    """[day_start_from, day_start_to+1) in UTC — a range that covers whole local days."""
    tz = await company_tz(db, company_id)
    start, _ = day_bounds(from_date, tz)
    _, end = day_bounds(to_date, tz)
    return start, end


# ================================================================== on-time
async def on_time_report(db: AsyncSession, company_id: UUID,
                         from_date: date | None = None,
                         to_date: date | None = None) -> OnTimeReport:
    from_date, to_date = _default_range(from_date, to_date)
    start, end = await _bounds(db, company_id, from_date, to_date)

    # For pickups, "on time" means: the driver's status went to EN_ROUTE_TO_PICKUP, then
    # ARRIVED_AT_PICKUP, at or before scheduled_pickup_at + late_grace_minutes.
    # The event stream has the exact arrival time; trips has only current state.
    grace = _late_grace(db)  # we'll define below
    # Simpler and more honest: use trip_events.arrived_pickup event time, joined to trip.
    stmt = (
        select(
            Trip.id,
            Trip.driver_id,
            Trip.scheduled_pickup_at,
            TripEvent.occurred_at.label("arrived_at"),
        )
        .join(
            TripEvent,
            and_(
                TripEvent.trip_id == Trip.id,
                TripEvent.company_id == Trip.company_id,
                TripEvent.to_status == S.ARRIVED_AT_PICKUP.value,
            ),
        )
        .where(
            Trip.company_id == company_id,
            Trip.scheduled_pickup_at >= start,
            Trip.scheduled_pickup_at < end,
        )
    )
    rows = (await db.execute(stmt)).all()

    # Aggregate by driver (and overall)
    by_driver: dict[UUID | None, dict] = {}
    for _trip_id, driver_id, scheduled, arrived in rows:
        if arrived is None:
            continue
        delta = (arrived - scheduled).total_seconds() / 60
        agg = by_driver.setdefault(driver_id, {"on": 0, "late": 0, "late_minutes": [], "total": 0})
        agg["total"] += 1
        if delta <= grace:
            agg["on"] += 1
        else:
            agg["late"] += 1
            agg["late_minutes"].append(delta)

    driver_names = {d.id: u.full_name for d, u in (await db.execute(
        select(Driver, User).join(User, and_(User.id == Driver.user_id,
                                            User.company_id == Driver.company_id))
        .where(Driver.company_id == company_id))).all()}

    def _row_for(driver_id, agg) -> OnTimeRow:
        total = agg["total"] or 1
        on = agg["on"]
        return OnTimeRow(
            driver_id=driver_id,
            driver_name=driver_names.get(driver_id) if driver_id else None,
            total_pickups=agg["total"],
            on_time_count=on,
            late_count=agg["late"],
            avg_late_minutes=round(sum(agg["late_minutes"]) / len(agg["late_minutes"]), 1)
                if agg["late_minutes"] else None,
            on_time_rate_bp=(on * 10_000) // total,
        )

    overall_agg = {
        "on": sum(a["on"] for a in by_driver.values()),
        "late": sum(a["late"] for a in by_driver.values()),
        "late_minutes": [m for a in by_driver.values() for m in a["late_minutes"]],
        "total": sum(a["total"] for a in by_driver.values()),
    }
    per_driver = [_row_for(did, a) for did, a in by_driver.items()]
    per_driver.sort(key=lambda r: (r.on_time_rate_bp, r.driver_name or ""))
    return OnTimeReport(
        period=PeriodOut(from_date=from_date, to_date=to_date),
        overall=_row_for(None, overall_agg),
        by_driver=per_driver,
    )


def _late_grace(db: AsyncSession) -> int:
    from app.core.config import settings
    return settings.late_grace_minutes


# ================================================================== cancellations
async def cancellation_report(db: AsyncSession, company_id: UUID,
                              from_date: date | None = None,
                              to_date: date | None = None) -> CancellationReport:
    from_date, to_date = _default_range(from_date, to_date)
    start, end = await _bounds(db, company_id, from_date, to_date)
    rows = (await db.execute(
        select(Trip.cancel_reason_code, Trip.late_cancel)
        .where(Trip.company_id == company_id,
               Trip.status == S.CANCELLED,
               Trip.scheduled_pickup_at >= start,
               Trip.scheduled_pickup_at < end)
    )).all()
    total = len(rows)
    by_reason: dict[str | None, dict] = {}
    late_count = 0
    for code, late in rows:
        entry = by_reason.setdefault(code, {"count": 0, "late": 0})
        entry["count"] += 1
        if late:
            entry["late"] += 1
            late_count += 1
    return CancellationReport(
        period=PeriodOut(from_date=from_date, to_date=to_date),
        total=total,
        by_reason=[
            CancellationRow(reason_code=code.value if code else None,
                            count=v["count"], late_cancellations=v["late"])
            for code, v in sorted(by_reason.items(), key=lambda kv: -kv[1]["count"])
        ],
        late_rate_bp=(late_count * 10_000) // total if total else 0,
    )


# ================================================================== no-shows
async def no_show_report(db: AsyncSession, company_id: UUID,
                          from_date: date | None = None,
                          to_date: date | None = None) -> NoShowReport:
    from_date, to_date = _default_range(from_date, to_date)
    start, end = await _bounds(db, company_id, from_date, to_date)
    rows = (await db.execute(
        select(Trip.driver_id, func.count())
        .where(Trip.company_id == company_id,
               Trip.status == S.NO_SHOW,
               Trip.scheduled_pickup_at >= start,
               Trip.scheduled_pickup_at < end)
        .group_by(Trip.driver_id)
    )).all()
    driver_names = {d.id: u.full_name for d, u in (await db.execute(
        select(Driver, User).join(User, and_(User.id == Driver.user_id,
                                            User.company_id == Driver.company_id))
        .where(Driver.company_id == company_id))).all()}
    total = sum(n for _, n in rows)
    return NoShowReport(
        period=PeriodOut(from_date=from_date, to_date=to_date),
        total=total,
        by_driver=[
            BreakdownRow(key=str(did) if did else "", label=driver_names.get(did, "(unassigned)"),
                         trip_count=n, completed_count=0, cancelled_count=0, no_show_count=n)
            for did, n in sorted(rows, key=lambda r: -r[1])
        ],
    )


# ================================================================== volume
async def volume_report(db: AsyncSession, company_id: UUID,
                        from_date: date | None = None,
                        to_date: date | None = None) -> VolumeReport:
    from_date, to_date = _default_range(from_date, to_date)
    start, end = await _bounds(db, company_id, from_date, to_date)
    tz = await company_tz(db, company_id)

    trips = list((await db.scalars(
        select(Trip).where(Trip.company_id == company_id,
                           Trip.scheduled_pickup_at >= start,
                           Trip.scheduled_pickup_at < end))).all())
    by_day: dict[date, dict] = {}
    for t in trips:
        d = t.scheduled_pickup_at.astimezone(tz).date()
        agg = by_day.setdefault(d, {"total": 0, "completed": 0, "cancelled": 0, "no_show": 0})
        agg["total"] += 1
        if t.status == S.COMPLETED:
            agg["completed"] += 1
        elif t.status == S.CANCELLED:
            agg["cancelled"] += 1
        elif t.status == S.NO_SHOW:
            agg["no_show"] += 1

    by_mobility: dict[str, int] = {}
    for t in trips:
        by_mobility[t.mobility.value] = by_mobility.get(t.mobility.value, 0) + 1
    by_source: dict[str, int] = {}
    for t in trips:
        by_source[t.source.value] = by_source.get(t.source.value, 0) + 1

    return VolumeReport(
        period=PeriodOut(from_date=from_date, to_date=to_date),
        total_trips=len(trips),
        by_day=[
            VolumeBucket(bucket=d, total=v["total"], completed=v["completed"],
                         cancelled=v["cancelled"], no_show=v["no_show"])
            for d, v in sorted(by_day.items())
        ],
        by_mobility=[
            BreakdownRow(key=k, label=k, trip_count=v, completed_count=v,
                         cancelled_count=0, no_show_count=0)
            for k, v in sorted(by_mobility.items(), key=lambda kv: -kv[1])
        ],
        by_source=[
            BreakdownRow(key=k, label=k, trip_count=v, completed_count=v,
                         cancelled_count=0, no_show_count=0)
            for k, v in sorted(by_source.items(), key=lambda kv: -kv[1])
        ],
    )


# ================================================================== driver utilization
async def driver_utilization(db: AsyncSession, company_id: UUID,
                              from_date: date | None = None,
                              to_date: date | None = None) -> DriverUtilizationReport:
    """For each driver, hours driving vs active.

    "Driving" comes from the trip event stream (en_route → completed). If a trip has
    no such events (very old records), falls back to `estimated_duration_min`.
    """
    from_date, to_date = _default_range(from_date, to_date)
    start, end = await _bounds(db, company_id, from_date, to_date)

    trips = list((await db.scalars(
        select(Trip).where(Trip.company_id == company_id,
                           Trip.driver_id.is_not(None),
                           Trip.scheduled_pickup_at >= start,
                           Trip.scheduled_pickup_at < end,
                           Trip.status.in_({S.COMPLETED, S.ASSIGNED, S.EN_ROUTE_TO_PICKUP,
                                            S.ARRIVED_AT_PICKUP, S.PASSENGER_ON_BOARD,
                                            S.EN_ROUTE_TO_DROPOFF, S.ARRIVED_AT_DROPOFF})))).all())

    # driver names
    driver_rows = (await db.execute(
        select(Driver, User).join(User, and_(User.id == Driver.user_id,
                                            User.company_id == Driver.company_id))
        .where(Driver.company_id == company_id))).all()
    driver_names = {d.id: u.full_name for d, u in driver_rows}

    by_driver: dict[UUID, dict] = {}
    for t in trips:
        agg = by_driver.setdefault(t.driver_id, {
            "trips": 0, "days": set(), "scheduled_minutes": 0, "active_span_minutes": 0,
        })
        agg["trips"] += 1
        agg["days"].add(t.scheduled_pickup_at.date())
        agg["scheduled_minutes"] += t.estimated_duration_min

    # actual drive minutes from trip events
    trip_ids = [t.id for t in trips if t.driver_id is not None]
    actual_by_trip: dict[UUID, int] = {}
    if trip_ids:
        event_rows = (await db.execute(
            select(TripEvent.trip_id, TripEvent.event_type, TripEvent.occurred_at)
            .where(TripEvent.company_id == company_id,
                   TripEvent.trip_id.in_(trip_ids),
                   TripEvent.event_type == "status_changed")
        )).all()
        by_trip: dict[UUID, list] = {}
        for tid, _, occurred in event_rows:
            by_trip.setdefault(tid, []).append(occurred)
        for tid, times in by_trip.items():
            if times:
                span = (max(times) - min(times)).total_seconds() / 60
                actual_by_trip[tid] = int(span)

    for t in trips:
        if t.driver_id is None:
            continue
        actual = actual_by_trip.get(t.id, t.estimated_duration_min)
        by_driver[t.driver_id]["active_span_minutes"] += actual

    rows = []
    for did, agg in by_driver.items():
        span = agg["active_span_minutes"] or 1
        util = (agg["scheduled_minutes"] * 10_000) // span if span else 0
        rows.append(UtilizationRow(
            driver_id=did,
            driver_name=driver_names.get(did, "(unknown)"),
            trips_driven=agg["trips"],
            active_days=len(agg["days"]),
            scheduled_hours=round(agg["scheduled_minutes"] / 60, 1),
            driving_minutes=agg["scheduled_minutes"],
            idle_minutes=max(0, agg["active_span_minutes"] - agg["scheduled_minutes"]),
            utilization_bp=min(10_000, util),
        ))
    rows.sort(key=lambda r: -r.utilization_bp)
    return DriverUtilizationReport(
        period=PeriodOut(from_date=from_date, to_date=to_date), rows=rows)


# ================================================================== vehicle utilization
async def vehicle_utilization(db: AsyncSession, company_id: UUID,
                               from_date: date | None = None,
                               to_date: date | None = None) -> VehicleUtilizationReport:
    from_date, to_date = _default_range(from_date, to_date)
    start, end = await _bounds(db, company_id, from_date, to_date)
    trips = list((await db.scalars(
        select(Trip).where(Trip.company_id == company_id,
                           Trip.vehicle_id.is_not(None),
                           Trip.scheduled_pickup_at >= start,
                           Trip.scheduled_pickup_at < end))).all())
    vehicles = list((await db.scalars(
        select(Vehicle).where(Vehicle.company_id == company_id))).all())
    vehicle_units = {v.id: v.unit_number for v in vehicles}
    # maintenance cost in range
    cost_by_vehicle: dict[UUID, int] = {}
    maint_rows = (await db.execute(
        select(VehicleMaintenance.vehicle_id, func.sum(VehicleMaintenance.cost_cents))
        .where(VehicleMaintenance.company_id == company_id,
               VehicleMaintenance.service_date >= from_date,
               VehicleMaintenance.service_date <= to_date)
        .group_by(VehicleMaintenance.vehicle_id)
    )).all()
    for vid, total in maint_rows:
        cost_by_vehicle[vid] = total or 0

    by_vehicle: dict[UUID, dict] = {}
    for t in trips:
        agg = by_vehicle.setdefault(t.vehicle_id, {"trips": 0, "distance_km": 0.0})
        agg["trips"] += 1
        if t.pickup_latitude and t.dropoff_latitude:
            from math import asin, cos, radians, sin, sqrt
            r = 6371.0
            p1, p2 = radians(t.pickup_latitude), radians(t.dropoff_latitude)
            dp = p2 - p1
            dl = radians(t.dropoff_longitude - t.pickup_longitude)
            h = sin(dp/2)**2 + cos(p1) * cos(p2) * sin(dl/2)**2
            agg["distance_km"] += 2 * r * asin(sqrt(h))

    return VehicleUtilizationReport(
        period=PeriodOut(from_date=from_date, to_date=to_date),
        rows=[
            VehicleUtilizationRow(
                vehicle_id=vid,
                unit_number=vehicle_units.get(vid, "(unknown)"),
                trips_driven=agg["trips"],
                distance_km=round(agg["distance_km"], 2),
                maintenance_cost_cents=cost_by_vehicle.get(vid, 0),
            )
            for vid, agg in sorted(by_vehicle.items(), key=lambda kv: -kv[1]["trips"])
        ],
    )


# ================================================================== revenue productivity
async def revenue_productivity(db: AsyncSession, company_id: UUID,
                               from_date: date | None = None,
                               to_date: date | None = None) -> RevenueProductivityOut:
    from_date, to_date = _default_range(from_date, to_date)
    # service_date is a local date on claims
    claims = list((await db.scalars(
        select(Claim).where(Claim.company_id == company_id,
                            Claim.service_date >= from_date,
                            Claim.service_date <= to_date))).all())
    billed = sum(c.billed_cents for c in claims)
    paid = sum(c.paid_cents for c in claims)
    trips_completed = len(claims)             # one claim == one trip (today)
    # distance from trips
    trip_ids = [c.trip_id for c in claims if c.trip_id]
    dist_km = 0.0
    if trip_ids:
        trip_rows = (await db.scalars(
            select(Trip).where(Trip.company_id == company_id,
                              Trip.id.in_(trip_ids)))).all()
        for t in trip_rows:
            if t.pickup_latitude and t.dropoff_latitude:
                from math import asin, cos, radians, sin, sqrt
                r = 6371.0
                p1, p2 = radians(t.pickup_latitude), radians(t.dropoff_latitude)
                dp = p2 - p1
                dl = radians(t.dropoff_longitude - t.pickup_longitude)
                h = sin(dp/2)**2 + cos(p1) * cos(p2) * sin(dl/2)**2
                dist_km += 2 * r * asin(sqrt(h))
    return RevenueProductivityOut(
        period=PeriodOut(from_date=from_date, to_date=to_date),
        trips_completed=trips_completed,
        total_distance_km=round(dist_km, 2),
        billed_cents=billed,
        paid_cents=paid,
        revenue_per_trip_cents=(billed // trips_completed) if trips_completed else 0,
        revenue_per_km_cents=int(billed / dist_km) if dist_km > 0 else 0,
        paid_rate_bp=(paid * 10_000) // billed if billed else 0,
    )


# ================================================================== leaderboard
async def leaderboard(db: AsyncSession, company_id: UUID,
                      from_date: date | None = None,
                      to_date: date | None = None,
                      limit: int = 20) -> LeaderboardReport:
    from_date, to_date = _default_range(from_date, to_date)
    start, end = await _bounds(db, company_id, from_date, to_date)
    driver_rows = (await db.execute(
        select(Driver, User).join(User, and_(User.id == Driver.user_id,
                                            User.company_id == Driver.company_id))
        .where(Driver.company_id == company_id))).all()
    driver_names = {d.id: u.full_name for d, u in driver_rows}

    # per driver: completed count, cancelled count, no_show count, distance
    trips = list((await db.scalars(
        select(Trip).where(Trip.company_id == company_id,
                          Trip.driver_id.is_not(None),
                          Trip.scheduled_pickup_at >= start,
                          Trip.scheduled_pickup_at < end))).all())
    by_driver: dict[UUID, dict] = {}
    for t in trips:
        agg = by_driver.setdefault(t.driver_id, {
            "completed": 0, "cancelled": 0, "no_show": 0, "dist": 0.0})
        if t.status == S.COMPLETED:
            agg["completed"] += 1
            if t.pickup_latitude and t.dropoff_latitude:
                from math import asin, cos, radians, sin, sqrt
                r = 6371.0
                p1, p2 = radians(t.pickup_latitude), radians(t.dropoff_latitude)
                dp = p2 - p1
                dl = radians(t.dropoff_longitude - t.pickup_longitude)
                h = sin(dp/2)**2 + cos(p1) * cos(p2) * sin(dl/2)**2
                agg["dist"] += 2 * r * asin(sqrt(h))
        elif t.status == S.CANCELLED:
            agg["cancelled"] += 1
        elif t.status == S.NO_SHOW:
            agg["no_show"] += 1

    # on-time rate reused from on_time_report
    ontime = await on_time_report(db, company_id, from_date, to_date)
    on_time_by_driver = {r.driver_id: r.on_time_rate_bp for r in ontime.by_driver if r.driver_id}

    ranked = sorted(
        by_driver.items(),
        key=lambda kv: (-kv[1]["completed"], -on_time_by_driver.get(kv[0], 0)),
    )[:limit]
    return LeaderboardReport(
        period=PeriodOut(from_date=from_date, to_date=to_date),
        rows=[
            LeaderboardRow(
                rank=i + 1,
                driver_id=did,
                driver_name=driver_names.get(did, "(unknown)"),
                completed_trips=agg["completed"],
                on_time_rate_bp=on_time_by_driver.get(did, 0),
                cancelled_trips=agg["cancelled"],
                no_show_trips=agg["no_show"],
                distance_km=round(agg["dist"], 2),
            )
            for i, (did, agg) in enumerate(ranked)
        ],
    )