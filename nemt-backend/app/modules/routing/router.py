from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud import get_or_404
from app.core.deps import get_db, require_permission
from app.core.permissions import Permission, has_permission
from app.modules.audit import service as audit
from app.modules.routing import service
from app.modules.routing.models import Route, RoutePlan, RouteStatus
from app.modules.routing.schemas import (
    ApplyOut, EstimateIn, EstimateOut, GeometryOut, PlanOut, PlanRequest, RouteOut,
)
from app.modules.users.models import User

router = APIRouter(prefix="/routing", tags=["route optimisation"])
READ = Depends(require_permission(Permission.TRIPS_READ))
DISPATCH = Depends(require_permission(Permission.DISPATCH))


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def make_plan(body: PlanRequest, request: Request, actor: User = DISPATCH,
                    db: AsyncSession = Depends(get_db)):
    """Ask the optimiser for a proposal. NOTHING in the real world changes until you apply it."""
    plan = await service.create_plan(db, actor, body, request)
    return await service.plan_out(db, actor.company_id, plan)


@router.get("/plans/{plan_id}", response_model=PlanOut)
async def get_plan(plan_id: UUID, request: Request, actor: User = READ,
                   db: AsyncSession = Depends(get_db)):
    plan = await get_or_404(db, RoutePlan, plan_id, actor.company_id, "Plan")
    await audit.record(db, action="routing.read", actor=actor, resource_type="route_plan",
                       resource_id=plan.id, request=request)
    await db.commit()
    return await service.plan_out(db, actor.company_id, plan)


@router.post("/plans/{plan_id}/apply", response_model=ApplyOut)
async def apply_plan(plan_id: UUID, request: Request, actor: User = DISPATCH,
                     db: AsyncSession = Depends(get_db)):
    """Accept the proposal: assigns every trip and publishes the routes. All-or-nothing."""
    return await service.apply_plan(db, actor, plan_id, request)


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_plan(plan_id: UUID, request: Request, actor: User = DISPATCH,
                       db: AsyncSession = Depends(get_db)):
    await service.discard_plan(db, actor, plan_id, request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/routes", response_model=list[RouteOut])
async def list_routes(
    request: Request,
    on_date: date,
    status_: RouteStatus = Query(default=RouteStatus.PUBLISHED, alias="status"),
    driver_id: UUID | None = None,
    actor: User = READ,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Route).where(Route.company_id == actor.company_id,
                               Route.route_date == on_date, Route.status == status_)
    if driver_id:
        stmt = stmt.where(Route.driver_id == driver_id)
    routes = list((await db.scalars(stmt.order_by(Route.planned_start_at))).all())
    await audit.record(db, action="routing.read", actor=actor, resource_type="route",
                       request=request, meta={"count": len(routes)})
    await db.commit()
    return await service.routes_out(db, actor.company_id, routes)


@router.get("/routes/{route_id}", response_model=RouteOut)
async def get_route(route_id: UUID, request: Request, actor: User = READ,
                    db: AsyncSession = Depends(get_db)):
    route = await get_or_404(db, Route, route_id, actor.company_id, "Route")
    await audit.record(db, action="routing.read", actor=actor, resource_type="route",
                       resource_id=route.id, request=request)
    await db.commit()
    return (await service.routes_out(db, actor.company_id, [route]))[0]


@router.get("/routes/{route_id}/geometry", response_model=GeometryOut)
async def route_geometry(route_id: UUID, actor: User = READ, db: AsyncSession = Depends(get_db)):
    """The line to draw on the dispatcher's map."""
    return await service.route_geometry(db, actor, route_id)


@router.post("/trips/{trip_id}/estimate", response_model=EstimateOut)
async def estimate(trip_id: UUID, body: EstimateIn, request: Request, actor: User = READ,
                   db: AsyncSession = Depends(get_db)):
    """How long will this ride take, and can it make its appointment?"""
    if body.apply and not has_permission(actor.role, Permission.TRIPS_WRITE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
    return await service.estimate_trip(db, actor, trip_id, body.apply, request)