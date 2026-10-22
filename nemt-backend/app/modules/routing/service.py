"""Plan routes for a day, save the plan, apply it, and estimate single trips."""

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base import new_id
from app.core.config import settings
from app.core.crud import get_or_404
from app.core.security import decrypt_field
from app.modules.audit import service as audit
from app.modules.drivers.models import Driver
from app.modules.patients.models import MobilityType, Patient
from app.modules.routing import solver
from app.modules.routing.models import PlanStatus, Route, RoutePlan, RouteStatus, RouteStop
from app.modules.routing.providers import Matrix, Point, RoutingError, get_router, traffic_factor
from app.modules.routing.schemas import (
    AppointmentOut,
    EstimateOut,
    GeometryOut,
    LoadOut,
    PlanOut,
    PlanRequest,
    RouteOut,
    SkippedResourceOut,
    StopOut,
    UnassignedOut,
)
from app.modules.trips import conflicts
from app.modules.trips.models import Trip
from app.modules.trips.schemas import DriverBrief, VehicleBrief
from app.modules.trips.service import (
    add_event,
    company_tz,
    day_bounds,
    lock_keys,
    lock_trip,
    patient_overlaps,
    place_out,
)
from app.modules.trips.state import EDITABLE, OCCUPYING, TripStatus
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle, VehicleStatus

S = TripStatus
MAX_TRIPS, MAX_RESOURCES, UNREACHABLE_MIN = 120, 40, 99_999
STARTED = OCCUPYING - {S.ASSIGNED}


def _http(code: int, detail: Any) -> HTTPException:
    return HTTPException(code, detail)


def dwell_minutes(mobility: MobilityType) -> int:
    return {
        MobilityType.AMBULATORY: settings.dwell_ambulatory_min,
        MobilityType.WALKER: settings.dwell_walker_min,
        MobilityType.WHEELCHAIR_MANUAL: settings.dwell_wheelchair_min,
        MobilityType.WHEELCHAIR_POWER: settings.dwell_wheelchair_min,
        MobilityType.STRETCHER: settings.dwell_stretcher_min,
    }[mobility]


async def _matrix(points: list[Point]) -> Matrix:
    try:
        return await get_router().matrix(points)
    except RoutingError as exc:
        raise _http(503, f"Routing service unavailable: {exc}") from None


@dataclass
class Res:
    driver: Driver
    user: User
    vehicle: Vehicle


async def _resources(db: AsyncSession, cid: UUID, body: PlanRequest) -> tuple[list[Res], list[dict]]:
    join_driver = and_(Driver.id == Vehicle.default_driver_id, Driver.company_id == Vehicle.company_id)
    join_user = and_(User.id == Driver.user_id, User.company_id == Driver.company_id)
    if body.resources:
        found: list[Res] = []
        for ref in body.resources:
            row = (await db.execute(select(Driver, User).join(User, join_user).where(
                Driver.id == ref.driver_id, Driver.company_id == cid))).first()
            if row is None:
                raise _http(404, "Driver not found")
            found.append(Res(row[0], row[1], await get_or_404(db, Vehicle, ref.vehicle_id, cid, "Vehicle")))
        if len({r.driver.id for r in found}) != len(found) or len({r.vehicle.id for r in found}) != len(found):
            raise _http(422, "Each driver and each vehicle may appear only once in `resources`")
    else:
        rows = (await db.execute(select(Vehicle, Driver, User).join(Driver, join_driver).join(User, join_user).where(
            Vehicle.company_id == cid, Vehicle.archived_at.is_(None)).order_by(Vehicle.unit_number))).all()
        found = [Res(d, u, v) for v, d, u in rows]
        seen: set[UUID] = set()
        deduped: list[Res] = []
        for r in found:
            if r.driver.id in seen:
                continue
            seen.add(r.driver.id)
            deduped.append(r)
        found = deduped
    return found, []


async def create_plan(db: AsyncSession, actor: User, body: PlanRequest, request: Request) -> RoutePlan:
    cid = actor.company_id
    tz = await company_tz(db, cid)
    day_start, day_end = day_bounds(body.on_date, tz)
    t0 = day_start

    candidates, skipped = await _resources(db, cid, body)
    started_drivers = set((await db.scalars(select(Trip.driver_id).where(
        Trip.company_id == cid, Trip.status.in_(STARTED),
        Trip.scheduled_pickup_at >= day_start, Trip.scheduled_pickup_at < day_end))).all())
    resources: list[Res] = []
    for r in candidates:
        reason = (
            "driver_inactive" if r.driver.archived_at or not r.user.is_active
            else "vehicle_not_active" if r.vehicle.archived_at or r.vehicle.status != VehicleStatus.ACTIVE
            else "driver_already_on_a_trip" if r.driver.id in started_drivers
            else None
        )
        if reason:
            skipped.append({"driver_id": str(r.driver.id), "vehicle_id": str(r.vehicle.id), "reason": reason})
        else:
            resources.append(r)
    if not resources:
        raise _http(422, "No usable driver+vehicle pairs. Pass `resources`, or set default drivers on your vehicles.")
    if len(resources) > MAX_RESOURCES:
        raise _http(422, f"At most {MAX_RESOURCES} vehicles per plan")
    by_pair = {(r.driver.id, r.vehicle.id): i for i, r in enumerate(resources)}

    stmt = (select(Trip, Patient)
            .join(Patient, and_(Patient.id == Trip.patient_id, Patient.company_id == Trip.company_id))
            .where(Trip.company_id == cid,
                   Trip.scheduled_pickup_at >= day_start, Trip.scheduled_pickup_at < day_end,
                   Trip.status.in_({S.SCHEDULED, S.ASSIGNED}))
            .order_by(Trip.scheduled_pickup_at, Trip.trip_number))
    if body.trip_ids:
        stmt = stmt.where(Trip.id.in_(body.trip_ids))
    trips: list[tuple[Trip, Patient, int | None]] = []
    for trip, patient in (await db.execute(stmt)).all():
        committed: int | None = None
        if trip.status == S.ASSIGNED:
            committed = by_pair.get((trip.driver_id, trip.vehicle_id))
            if committed is None:
                continue
            if body.include_assigned:
                committed = None
        trips.append((trip, patient, committed))
    if len(trips) > MAX_TRIPS:
        raise _http(422, f"Too many trips ({len(trips)}); at most {MAX_TRIPS} per plan. Narrow it with trip_ids.")

    points: list[Point] = []
    index: dict[tuple[float, float], int] = {}

    def loc(lat: float, lng: float) -> int:
        key = (round(lat, 6), round(lng, 6))
        if key not in index:
            index[key] = len(points)
            points.append(Point(*key))
        return index[key]

    ends = [(loc(t.pickup_latitude, t.pickup_longitude), loc(t.dropoff_latitude, t.dropoff_longitude))
            for t, _, _ in trips]
    loc(body.depot.latitude, body.depot.longitude) if body.depot else None
    router_matrix = await _matrix(points) if points else Matrix([], [], get_router().name)

    factor = [1.0] * len(points)
    for (t, _, _), (p, d) in zip(trips, ends, strict=True):
        f = traffic_factor(t.scheduled_pickup_at.astimezone(tz))
        factor[p], factor[d] = max(factor[p], f), max(factor[d], f)

    def minutes(i: int, j: int) -> int:
        sec = router_matrix.durations_s[i][j]
        if sec is None:
            return UNREACHABLE_MIN
        return 0 if i == j else max(1, math.ceil(sec * factor[i] / 60))

    n = len(points)
    travel = [[minutes(i, j) for j in range(n)] for i in range(n)]

    def mins(dt: datetime) -> int:
        return round((dt - t0).total_seconds() / 60)

    jobs: list[solver.Job] = []
    unassigned: list[dict] = []
    for (t, _, committed), (p, d) in zip(trips, ends, strict=True):
        was = t.status == S.ASSIGNED
        if travel[p][d] >= UNREACHABLE_MIN:
            unassigned.append({"trip_id": str(t.id), "reasons": ["unreachable_by_road"], "was_assigned": was})
            continue
        trip_day = t.scheduled_pickup_at.astimezone(tz).date()
        allowed, codes = set(), set()
        for ri, r in enumerate(resources):
            rep = conflicts.static_report(t, r.driver, r.user.is_active, r.vehicle, trip_day)
            if rep.ok:
                allowed.add(ri)
            else:
                codes.update(e.code for e in rep.errors)
        if not allowed and committed is None:
            unassigned.append({"trip_id": str(t.id), "reasons": sorted(codes) or ["no_compatible_resource"],
                               "was_assigned": was})
            continue
        dwell = dwell_minutes(t.mobility)
        direct = travel[p][d]
        jobs.append(solver.Job(
            key=str(t.id),
            pickup=p,
            dropoff=d,
            load=solver.Load(*_load(t)),
            pickup_open=mins(t.scheduled_pickup_at) - settings.pickup_early_min,
            pickup_close=mins(t.scheduled_pickup_at) + settings.pickup_late_min,
            dropoff_close=mins(t.appointment_at) - settings.appointment_buffer_min if t.appointment_at else None,
            pickup_service=dwell,
            dropoff_service=dwell,
            max_ride=math.ceil(settings.max_ride_factor * direct) + settings.max_ride_extra_min,
            allowed=frozenset(allowed),
            fixed_vehicle=committed,
        ))

    shift = (
        body.shift_start.hour * 60 + body.shift_start.minute if body.shift_start else 0,
        body.shift_end.hour * 60 + body.shift_end.minute if body.shift_end else 30 * 60,
    )
    specs = [solver.VehicleSpec(
        key=r.vehicle.unit_number,
        capacity=solver.Load(r.vehicle.seat_capacity, r.vehicle.wheelchair_capacity, r.vehicle.stretcher_capacity),
        shift_start=shift[0],
        shift_end=shift[1],
    ) for r in resources]

    budget = body.time_limit_s or settings.solver_seconds
    sol = await asyncio.to_thread(solver.solve, travel, jobs, specs)

    trip_by_key = {str(t.id): (t, ends_i) for (t, _, _), ends_i in zip(trips, ends, strict=True)}
    was_assigned = {str(t.id): t.status == S.ASSIGNED for t, _, _ in trips}
    for key, why in sol.early_dropped.items():
        unassigned.append({"trip_id": key, "reasons": [why], "was_assigned": was_assigned.get(key, False)})
    for key in sol.dropped:
        unassigned.append({"trip_id": key, "reasons": ["no_capacity_or_time"],
                           "was_assigned": was_assigned.get(key, False)})

    plan = RoutePlan(
        id=new_id(),
        company_id=cid,
        route_date=body.on_date,
        status=PlanStatus.PROPOSED,
        created_by=actor.id,
        skipped_resources=skipped,
        unassigned=unassigned,
        metrics={},
        params={
            "resources": [{"driver_id": str(r.driver.id), "vehicle_id": str(r.vehicle.id)} for r in resources],
            "include_assigned": body.include_assigned,
            "depot": body.depot is not None,
        },
    )
    db.add(plan)
    await db.flush()

    total_drive = 0
    total_m = 0
    shared = 0
    for vr in sol.routes:
        res = resources[vr.vehicle]
        first, last = vr.stops[0], vr.stops[-1]
        metres = 0.0
        prev = None
        route = Route(
            id=new_id(),
            company_id=cid,
            plan_id=plan.id,
            route_date=body.on_date,
            driver_id=res.driver.id,
            vehicle_id=res.vehicle.id,
            status=RouteStatus.PROPOSED,
            planned_start_at=t0 + timedelta(minutes=first.arrival),
            planned_end_at=t0 + timedelta(minutes=last.departure),
            drive_minutes=vr.drive_minutes,
            distance_m=0,
        )
        db.add(route)
        await db.flush()
        spans: dict[str, list[int]] = {}
        for seq, st in enumerate(vr.stops, start=1):
            trip, (p, d) = trip_by_key[st.job_key]
            here = p if st.kind == "pickup" else d
            if prev is not None:
                metres += router_matrix.distances_m[prev][here] or 0
            prev = here
            spans.setdefault(st.job_key, []).append(st.arrival)
            point = points[here]
            db.add(RouteStop(
                id=new_id(),
                company_id=cid,
                route_id=route.id,
                sequence=seq,
                trip_id=trip.id,
                kind=st.kind,
                planned_arrival_at=t0 + timedelta(minutes=st.arrival),
                planned_departure_at=t0 + timedelta(minutes=st.departure),
                wait_minutes=st.wait,
                load_seats=st.load_after.seats,
                load_wheelchair=st.load_after.wheelchair,
                load_stretcher=st.load_after.stretcher,
                latitude=point.lat,
                longitude=point.lng,
            ))
        route.distance_m = round(metres)
        ivs = list(spans.values())
        shared += sum(any(o is not iv and o[0] < iv[1] and iv[0] < o[1] for o in ivs) for iv in ivs)
        total_drive += vr.drive_minutes
        total_m += route.distance_m

    planned = sum(len({s.job_key for s in vr.stops}) for vr in sol.routes)
    plan.metrics = {
        "trips_requested": len(trips),
        "trips_planned": planned,
        "trips_unassigned": len(unassigned),
        "vehicles_available": len(resources),
        "vehicles_used": len(sol.routes),
        "drive_minutes": total_drive,
        "distance_km": round(total_m / 1000, 1),
        "shared_trips": shared,
        "routing_source": router_matrix.source,
        "committed_trips": sum(c is not None for _, _, c in trips),
        "solver_budget_s": budget,
    }
    await audit.record(db, action="routing.plan", actor=actor, resource_type="route_plan",
                       resource_id=plan.id, request=request, meta=plan.metrics)
    await db.commit()
    await db.refresh(plan)
    return plan


def _load(t: Trip) -> tuple[int, int, int]:
    need = conflicts.load_of(t.mobility, t.escort_count)
    return need.seats, need.wheelchair, need.stretcher


async def routes_out(db: AsyncSession, cid: UUID, routes: list[Route]) -> list[RouteOut]:
    if not routes:
        return []
    ids = [r.id for r in routes]
    drivers = {d.id: (d, u) for d, u in (await db.execute(select(Driver, User).join(
        User, and_(User.id == Driver.user_id, User.company_id == Driver.company_id)).where(
        Driver.company_id == cid, Driver.id.in_({r.driver_id for r in routes})))).all()}
    vehicles = {v.id: v for v in (await db.scalars(select(Vehicle).where(
        Vehicle.company_id == cid, Vehicle.id.in_({r.vehicle_id for r in routes})))).all()}
    rows = (await db.execute(select(RouteStop, Trip, Patient).join(
        Trip, and_(Trip.id == RouteStop.trip_id, Trip.company_id == RouteStop.company_id)).join(
        Patient, and_(Patient.id == Trip.patient_id, Patient.company_id == Trip.company_id)).where(
        RouteStop.company_id == cid, RouteStop.route_id.in_(ids)).order_by(RouteStop.route_id, RouteStop.sequence))).all()
    stops: dict[UUID, list[StopOut]] = {}
    for st, trip, pat in rows:
        pickup = st.kind == "pickup"
        booked = trip.scheduled_pickup_at if pickup else trip.appointment_at
        stops.setdefault(st.route_id, []).append(StopOut(
            sequence=st.sequence, kind=st.kind, trip_id=trip.id,
            trip_ref=f"T-{trip.trip_number:06d}", trip_status=trip.status,
            patient_first_name=pat.first_name, patient_last_name=pat.last_name, mobility=trip.mobility,
            place=place_out(trip, "pickup" if pickup else "dropoff"),
            planned_arrival_at=st.planned_arrival_at, planned_departure_at=st.planned_departure_at,
            wait_minutes=st.wait_minutes, booked_time=booked,
            delta_minutes=round((st.planned_arrival_at - booked).total_seconds() / 60) if pickup and booked else None,
            on_board_after=LoadOut(seats=st.load_seats, wheelchair=st.load_wheelchair, stretcher=st.load_stretcher),
        ))
    out: list[RouteOut] = []
    for r in routes:
        d, u = drivers[r.driver_id]
        out.append(RouteOut(
            id=r.id, plan_id=r.plan_id, route_date=r.route_date, status=r.status,
            driver=DriverBrief(id=d.id, full_name=u.full_name),
            vehicle=VehicleBrief(id=r.vehicle_id, unit_number=vehicles[r.vehicle_id].unit_number),
            planned_start_at=r.planned_start_at, planned_end_at=r.planned_end_at,
            drive_minutes=r.drive_minutes, distance_km=round(r.distance_m / 1000, 2),
            stops=stops.get(r.id, []),
        ))
    return out


async def plan_out(db: AsyncSession, cid: UUID, plan: RoutePlan) -> PlanOut:
    routes = (await db.scalars(select(Route).where(Route.company_id == cid, Route.plan_id == plan.id)
                               .order_by(Route.planned_start_at))).all()
    trip_ids = [UUID(u["trip_id"]) for u in plan.unassigned]
    refs: dict[UUID, int] = {}
    if trip_ids:
        refs = {t.id: t.trip_number for t in (await db.scalars(select(Trip).where(
            Trip.company_id == cid, Trip.id.in_(trip_ids)))).all()}
    return PlanOut(
        id=plan.id, route_date=plan.route_date, status=plan.status, metrics=plan.metrics,
        routes=await routes_out(db, cid, list(routes)),
        unassigned=[UnassignedOut(trip_id=UUID(u["trip_id"]),
                                  trip_ref=f"T-{refs.get(UUID(u['trip_id']), 0):06d}",
                                  reasons=u["reasons"], was_assigned=u["was_assigned"])
                    for u in plan.unassigned],
        skipped_resources=[SkippedResourceOut(**s) for s in plan.skipped_resources],
        created_at=plan.created_at,
    )


async def apply_plan(db: AsyncSession, actor: User, plan_id: UUID, request: Request) -> dict[str, Any]:
    cid = actor.company_id
    plan = await db.scalar(select(RoutePlan).where(RoutePlan.id == plan_id, RoutePlan.company_id == cid)
                           .with_for_update())
    if plan is None:
        raise _http(404, "Plan not found")
    if plan.status != PlanStatus.PROPOSED:
        raise _http(409, f"This plan is already {plan.status.value}")
    routes = (await db.scalars(select(Route).where(Route.plan_id == plan.id,
                                                   Route.status == RouteStatus.PROPOSED))).all()
    stops = (await db.scalars(select(RouteStop).where(
        RouteStop.company_id == cid, RouteStop.route_id.in_([r.id for r in routes])))).all() if routes else []
    planned_ids = {s.trip_id for s in stops}
    drop_ids = {UUID(u["trip_id"]) for u in plan.unassigned if u["was_assigned"]}
    trips = {t.id: t for t in (await db.scalars(select(Trip).where(
        Trip.company_id == cid, Trip.id.in_(planned_ids | drop_ids))
        .order_by(Trip.id).with_for_update())).all()}

    res: dict[UUID, tuple[Driver, User, Vehicle]] = {}
    for r in routes:
        row = (await db.execute(select(Driver, User).join(
            User, and_(User.id == Driver.user_id, User.company_id == Driver.company_id))
            .where(Driver.id == r.driver_id, Driver.company_id == cid))).first()
        res[r.id] = (row[0], row[1], await db.get(Vehicle, r.vehicle_id))
    await lock_keys(db, *[f"driver:{r.driver_id}" for r in routes],
                    *[f"vehicle:{r.vehicle_id}" for r in routes])

    tz = await company_tz(db, cid)
    problems: list[dict] = []
    trip_route: dict[UUID, Route] = {}
    for s in stops:
        trip_route[s.trip_id] = next(r for r in routes if r.id == s.route_id)
    plan_pairs = {(r.driver_id, r.vehicle_id) for r in routes}
    for tid in sorted(planned_ids | drop_ids, key=str):
        t = trips[tid]
        ref = f"T-{t.trip_number:06d}"
        if t.status not in EDITABLE:
            problems.append({"trip": ref, "problem": f"is now {t.status.value}"})
        elif t.updated_at > plan.created_at:
            problems.append({"trip": ref, "problem": "was changed after this plan was made"})
        elif t.status == S.ASSIGNED and (t.driver_id, t.vehicle_id) not in plan_pairs:
            problems.append({"trip": ref, "problem": "was assigned to someone else since the plan was made"})
        elif tid in trip_route:
            route = trip_route[tid]
            driver, user, vehicle = res[route.id]
            rep = conflicts.static_report(t, driver, user.is_active, vehicle,
                                          t.scheduled_pickup_at.astimezone(tz).date())
            problems += [{"trip": ref, "problem": e.message} for e in rep.errors]
    if problems:
        raise _http(409, {"message": "This plan is out of date. Nothing was changed; make a new plan.",
                          "problems": problems})

    now = datetime.now(UTC)
    assigned = 0
    for tid, route in trip_route.items():
        t = trips[tid]
        driver, _, vehicle = res[route.id]
        if (t.driver_id, t.vehicle_id, t.status) != (driver.id, vehicle.id, S.ASSIGNED):
            before, prev = t.status, t.driver_id
            t.driver_id, t.vehicle_id, t.status = driver.id, vehicle.id, S.ASSIGNED
            t.assigned_at, t.assigned_by, t.en_route_at = now, actor.id, None
            add_event(db, t, "reassigned" if prev else "assigned", actor.id,
                      from_status=before, to_status=S.ASSIGNED,
                      meta={"driver_id": str(driver.id), "vehicle_id": str(vehicle.id), "plan_id": str(plan.id)})
        assigned += 1
    freed = 0
    for tid in drop_ids:
        t = trips[tid]
        before = t.status
        t.driver_id = t.vehicle_id = t.assigned_at = t.assigned_by = t.en_route_at = None  # type: ignore[assignment]
        t.status = S.SCHEDULED
        add_event(db, t, "unassigned", actor.id, from_status=before, to_status=S.SCHEDULED,
                  meta={"plan_id": str(plan.id)})
        freed += 1
    await db.flush()
    await db.execute(update(Route).where(
        Route.company_id == cid, Route.route_date == plan.route_date,
        Route.status == RouteStatus.PUBLISHED,
        Route.driver_id.in_([r.driver_id for r in routes]) | Route.vehicle_id.in_([r.vehicle_id for r in routes]),
    ).values(status=RouteStatus.SUPERSEDED))
    for r in routes:
        r.status = RouteStatus.PUBLISHED
    plan.status, plan.applied_at, plan.applied_by = PlanStatus.APPLIED, now, actor.id
    await audit.record(db, action="routing.apply", actor=actor, resource_type="route_plan",
                       resource_id=plan.id, request=request,
                       meta={"routes": len(routes), "assigned": assigned, "unassigned": freed})
    await db.commit()
    return {"plan_id": plan.id, "routes_published": len(routes),
            "trips_assigned": assigned, "trips_unassigned": freed}


async def discard_plan(db: AsyncSession, actor: User, plan_id: UUID, request: Request) -> None:
    plan = await db.scalar(select(RoutePlan).where(RoutePlan.id == plan_id,
                                                   RoutePlan.company_id == actor.company_id)
                           .with_for_update())
    if plan is None:
        raise _http(404, "Plan not found")
    if plan.status != PlanStatus.PROPOSED:
        raise _http(409, f"A {plan.status.value} plan cannot be discarded")
    plan.status = PlanStatus.DISCARDED
    await db.execute(update(Route).where(Route.plan_id == plan.id).values(status=RouteStatus.DISCARDED))
    await audit.record(db, action="routing.discard", actor=actor, resource_type="route_plan",
                       resource_id=plan.id, request=request)
    await db.commit()


async def estimate_trip(db: AsyncSession, actor: User, trip_id: UUID, apply: bool, request: Request) -> EstimateOut:
    trip = await lock_trip(db, actor.company_id, trip_id)
    m = await _matrix([Point(trip.pickup_latitude, trip.pickup_longitude),
                       Point(trip.dropoff_latitude, trip.dropoff_longitude)])
    seconds, metres = m.durations_s[0][1], m.distances_m[0][1]
    if seconds is None:
        raise _http(422, "No road route between pickup and drop-off")
    tz = await company_tz(db, actor.company_id)
    factor = traffic_factor(trip.scheduled_pickup_at.astimezone(tz))
    drive = max(1, math.ceil(seconds * factor / 60))
    dwell = dwell_minutes(trip.mobility)
    duration = min(480, max(10, drive + 2 * dwell))

    appointment = None
    if trip.appointment_at:
        latest = trip.appointment_at - timedelta(minutes=settings.appointment_buffer_min + drive + dwell)
        slack = round((latest - trip.scheduled_pickup_at).total_seconds() / 60)
        appointment = AppointmentOut(appointment_at=trip.appointment_at, latest_pickup_at=latest,
                                     slack_minutes=slack, ok=slack >= 0)

    applied = False
    if apply:
        if trip.status not in EDITABLE:
            raise _http(409, f"A {trip.status.value} trip can no longer be changed")
        start, end = trip.scheduled_pickup_at, trip.scheduled_pickup_at + timedelta(minutes=duration)
        clashes = await patient_overlaps(db, actor.company_id, trip.patient_id, start, end, exclude=trip.id)
        if clashes:
            raise _http(409, {"code": "patient_overlap",
                              "message": "The new duration overlaps another trip of this patient",
                              "trips": [f"T-{c.trip_number:06d}" for c in clashes]})
        if trip.status == S.ASSIGNED:
            row = (await db.execute(select(Driver, User).join(
                User, and_(User.id == Driver.user_id, User.company_id == Driver.company_id))
                .where(Driver.id == trip.driver_id))).first()
            vehicle = await db.get(Vehicle, trip.vehicle_id)
            await lock_keys(db, f"driver:{trip.driver_id}", f"vehicle:{trip.vehicle_id}")
            trip.estimated_duration_min, trip.ends_at = duration, end
            rep = await conflicts.check_assignment(db, trip, row[0], row[1].is_active, vehicle,
                                                   start.astimezone(tz).date())
            if not rep.ok:
                await db.rollback()
                raise _http(409, {"message": "The longer trip would clash with the assigned driver/vehicle",
                                  "errors": [{"code": e.code, "message": e.message} for e in rep.errors]})
        trip.estimated_duration_min, trip.ends_at = duration, end
        add_event(db, trip, "updated", actor.id,
                  meta={"fields": ["estimated_duration_min"], "by": "routing_estimate"})
        applied = True
    await audit.record(db, action="routing.estimate", actor=actor, resource_type="trip",
                       resource_id=trip.id, request=request, meta={"applied": applied})
    await db.commit()
    return EstimateOut(drive_minutes=drive, distance_km=round((metres or 0) / 1000, 2),
                       dwell_minutes=2 * dwell, estimated_duration_min=duration,
                       traffic_factor=factor, source=m.source, appointment=appointment, applied=applied)


async def route_geometry(db: AsyncSession, actor: User, route_id: UUID) -> GeometryOut:
    route = await get_or_404(db, Route, route_id, actor.company_id, "Route")
    stops = (await db.scalars(select(RouteStop).where(RouteStop.route_id == route.id)
                              .order_by(RouteStop.sequence))).all()
    try:
        path = await get_router().path([Point(s.latitude, s.longitude) for s in stops])
    except RoutingError as exc:
        raise _http(503, f"Routing service unavailable: {exc}") from None
    return GeometryOut(coordinates=path.coordinates, distance_km=round(path.distance_m / 1000, 2),
                       duration_min=round(path.duration_s / 60, 1), source=path.source)


# silence unused-import warnings
_ = (decrypt_field,)