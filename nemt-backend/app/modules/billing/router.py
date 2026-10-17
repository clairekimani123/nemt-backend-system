"""Billing endpoints: rate plans, billable trips, claims, lifecycle, aging."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud import get_or_404
from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.modules.audit import service as audit
from app.modules.billing import service
from app.modules.billing.models import Claim, ClaimStatus, PayerType, RatePlan
from app.modules.billing.schemas import (
    AgingBucketOut, BillableTripOut, ClaimCreateFromTrip, ClaimDetailOut, ClaimOut,
    PostPaymentIn, RatePlanIn, RatePlanOut, RatePlanUpdate, SubmitIn,
)
from app.modules.patients.models import Patient
from app.modules.trips.models import Trip
from app.modules.trips.service import company_tz, day_bounds
from app.modules.users.models import User
from app.modules.billing.models import RateRule
from app.modules.billing.schemas import RateRuleIn, RateRuleOut
from fastapi import HTTPException, Response
from app.modules.billing.schemas import (
    BatchOut, BatchPaymentIn, BatchSubmitIn, BatchItemOutcome, FromTripsIn,
)

router = APIRouter(prefix="/billing", tags=["billing"])
READ = Depends(require_permission(Permission.BILLING_READ))
WRITE = Depends(require_permission(Permission.BILLING_WRITE))


# ------------------------------------------------------------------ rate plans
@router.get("/rate-plans", response_model=list[RatePlanOut])
async def list_rate_plans(broker_id: UUID | None = None, actor: User = READ,
                          db: AsyncSession = Depends(get_db)):
    stmt = select(RatePlan).where(RatePlan.company_id == actor.company_id)
    if broker_id is not None:
        stmt = stmt.where(RatePlan.broker_id == broker_id)
    return (await db.scalars(stmt.order_by(RatePlan.effective_from.desc()))).all()


@router.post("/rate-plans", response_model=RatePlanOut, status_code=status.HTTP_201_CREATED)
async def create_rate_plan(body: RatePlanIn, request: Request, actor: User = WRITE,
                           db: AsyncSession = Depends(get_db)):
    if body.is_default:
        await db.execute(service.clear_default_rate_plan_stmt(actor.company_id))
    plan = RatePlan(company_id=actor.company_id, **body.model_dump())
    db.add(plan)
    await db.flush()
    await audit.record(db, action="billing.rate_plan_created", actor=actor,
                       resource_type="rate_plan", resource_id=plan.id, request=request)
    await db.commit()
    await db.refresh(plan)
    return plan


@router.patch("/rate-plans/{plan_id}", response_model=RatePlanOut)
async def update_rate_plan(plan_id: UUID, body: RatePlanUpdate, request: Request,
                           actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    plan = await get_or_404(db, RatePlan, plan_id, actor.company_id, "Rate plan")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("is_default") is True:
        await db.execute(service.clear_default_rate_plan_stmt(actor.company_id))
    for k, v in changes.items():
        setattr(plan, k, v)
    await audit.record(db, action="billing.rate_plan_updated", actor=actor,
                       resource_type="rate_plan", resource_id=plan.id, request=request,
                       meta={"fields": sorted(changes)})
    await db.commit()
    await db.refresh(plan)
    return plan


# ------------------------------------------------------------------ billable trips
@router.get("/billable-trips", response_model=list[BillableTripOut])
async def list_billable_trips(
    on_date: date | None = None,
    limit: int = Query(100, ge=1, le=500),
    actor: User = READ,
    db: AsyncSession = Depends(get_db),
):
    """Completed trips that have not yet been turned into a claim."""
    stmt = (
        select(Trip, Patient)
        .join(Patient, and_(Patient.id == Trip.patient_id, Patient.company_id == Trip.company_id))
        .outerjoin(Claim, and_(Claim.trip_id == Trip.id, Claim.company_id == Trip.company_id))
        .where(Trip.company_id == actor.company_id,
               Trip.status == "completed",
               Claim.id.is_(None))
        .order_by(Trip.scheduled_pickup_at.desc())
        .limit(limit)
    )
    if on_date is not None:
        tz = await company_tz(db, actor.company_id)
        start, end = day_bounds(on_date, tz)
        stmt = stmt.where(Trip.scheduled_pickup_at >= start, Trip.scheduled_pickup_at < end)
    rows = (await db.execute(stmt)).all()
    out: list[BillableTripOut] = []
    for trip, patient in rows:
        item = service.billable_trip_out(trip).model_copy(update={
            "patient_first_name": patient.first_name,
            "patient_last_name": patient.last_name,
        })
        out.append(item)
    return out


# ------------------------------------------------------------------ claims
@router.post("/claims/from-trip/{trip_id}", response_model=ClaimOut, status_code=status.HTTP_201_CREATED)
async def create_claim_from_trip(trip_id: UUID, body: ClaimCreateFromTrip, request: Request,
                                 actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    return await service.create_claim_from_trip(db, actor, trip_id, body.payer_type,
                                                body.rate_plan_id, request)


@router.get("/claims", response_model=list[ClaimOut])
async def list_claims(
    status_: ClaimStatus | None = Query(default=None, alias="status"),
    broker_id: UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    actor: User = READ,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Claim).where(Claim.company_id == actor.company_id)
    if status_:
        stmt = stmt.where(Claim.status == status_)
    if broker_id:
        stmt = stmt.where(Claim.broker_id == broker_id)
    return (await db.scalars(stmt.order_by(Claim.service_date.desc()).limit(limit))).all()


@router.get("/claims/{claim_id}", response_model=ClaimDetailOut)
async def get_claim(claim_id: UUID, actor: User = READ, db: AsyncSession = Depends(get_db)):
    return await service.claim_detail(db, actor.company_id, claim_id)


@router.post("/claims/{claim_id}/submit", response_model=ClaimOut)
async def submit_claim(claim_id: UUID, body: SubmitIn, request: Request,
                       actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    return await service.submit_claim(db, actor, claim_id, body.reference, request)


@router.post("/claims/{claim_id}/payments", response_model=ClaimOut)
async def post_payment(claim_id: UUID, body: PostPaymentIn, request: Request,
                       actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    return await service.post_payment(db, actor, claim_id, body.kind, body.amount_cents,
                                      body.reference, body.note, body.denial_code, request)


@router.post("/claims/{claim_id}/write-off", response_model=ClaimOut)
async def write_off(claim_id: UUID, request: Request, note: str | None = None,
                    actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    return await service.write_off(db, actor, claim_id, note, request)


# ------------------------------------------------------------------ aging
@router.get("/aging", response_model=list[AgingBucketOut])
async def aging(actor: User = READ, db: AsyncSession = Depends(get_db)):
    return await service.aging(db, actor.company_id)


@router.get("/rate-plans/{plan_id}/rules", response_model=list[RateRuleOut])
async def list_rules(plan_id: UUID, actor: User = READ, db: AsyncSession = Depends(get_db)):
    await get_or_404(db, RatePlan, plan_id, actor.company_id, "Rate plan")
    return (await db.scalars(select(RateRule).where(
        RateRule.company_id == actor.company_id, RateRule.rate_plan_id == plan_id)
        .order_by(RateRule.priority.desc(), RateRule.id))).all()


@router.post("/rate-plans/{plan_id}/rules", response_model=RateRuleOut,
             status_code=status.HTTP_201_CREATED)
async def create_rule(plan_id: UUID, body: RateRuleIn, request: Request,
                      actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    await get_or_404(db, RatePlan, plan_id, actor.company_id, "Rate plan")
    rule = RateRule(company_id=actor.company_id, rate_plan_id=plan_id, **body.model_dump())
    db.add(rule)
    await db.flush()
    await audit.record(db, action="billing.rate_rule_created", actor=actor,
                       resource_type="rate_rule", resource_id=rule.id, request=request)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete("/rate-plans/{plan_id}/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(plan_id: UUID, rule_id: UUID, request: Request,
                      actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    rule = await db.scalar(select(RateRule).where(
        RateRule.id == rule_id, RateRule.company_id == actor.company_id,
        RateRule.rate_plan_id == plan_id))
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    await db.delete(rule)
    await audit.record(db, action="billing.rate_rule_deleted", actor=actor,
                       resource_type="rate_rule", resource_id=rule_id, request=request)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/claims/from-trips", response_model=BatchOut, status_code=status.HTTP_200_OK)
async def create_claims_from_trips(body: FromTripsIn, request: Request,
                                   actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    """Batch turn completed trips into draft claims. Reports per-item outcomes."""
    raw = await service.create_claims_from_trips(db, actor, body.trip_ids, body.payer_type,
                                                 body.rate_plan_id, request)
    outcomes = [BatchItemOutcome(**r) for r in raw]
    succeeded = sum(1 for o in outcomes if o.ok)
    return BatchOut(requested=len(outcomes), succeeded=succeeded,
                    failed=len(outcomes) - succeeded, outcomes=outcomes)


@router.post("/claims/batch-submit", response_model=BatchOut)
async def batch_submit(body: BatchSubmitIn, request: Request,
                       actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    raw = await service.submit_claims_batch(db, actor, body.claim_ids, body.reference, request)
    outcomes = [BatchItemOutcome(**r) for r in raw]
    succeeded = sum(1 for o in outcomes if o.ok)
    return BatchOut(requested=len(outcomes), succeeded=succeeded,
                    failed=len(outcomes) - succeeded, outcomes=outcomes)


@router.post("/claims/batch-pay", response_model=BatchOut)
async def batch_pay(body: BatchPaymentIn, request: Request,
                    actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    raw = await service.post_payment_batch(db, actor, body.claim_ids, body.kind,
                                           body.amount_cents, body.reference, body.note,
                                           body.denial_code, request)
    outcomes = [BatchItemOutcome(**r) for r in raw]
    succeeded = sum(1 for o in outcomes if o.ok)
    return BatchOut(requested=len(outcomes), succeeded=succeeded,
                    failed=len(outcomes) - succeeded, outcomes=outcomes)