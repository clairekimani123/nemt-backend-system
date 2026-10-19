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
from app.modules.billing.models import Claim, ClaimLine, ClaimStatus, PayerType, RatePlan
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
from fastapi import Query
from fastapi.responses import PlainTextResponse
from app.modules.billing.schemas import SummaryOut

from app.modules.billing.formats.edi import (
    BillingProvider, Payer as EDIPayer, Subscriber as EDISubscriber, Submitter, Receiver, generate_837p,
)
from app.modules.billing.formats.cms1500 import render_cms1500
from app.modules.patients.models import Patient
from app.modules.tenants.models import Company
from app.modules.users.models import User

from fastapi import Response
from fastapi.responses import FileResponse, PlainTextResponse


router = APIRouter(prefix="/billing", tags=["billing"])
READ = Depends(require_permission(Permission.BILLING_READ))
WRITE = Depends(require_permission(Permission.BILLING_WRITE))


async def _billing_config(db: AsyncSession, company_id: UUID):
    """Load the company's billing identity (submitter, provider). In a real system
    these come from a billing_settings table. Here we synthesize from the company row."""
    company = await db.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "Company not found")
    submitter = Submitter(
        name=(company.name or "COMPANY")[:35].upper(),
        id="MEDIRECTS",                          # placeholder; real submitter ID from clearinghouse
        contact_name="Billing",
        contact_phone="5555555555",
    )
    receiver = Receiver(name="TESTCLEARING", id="TESTCLR")
    provider = BillingProvider(
        name=(company.name or "COMPANY")[:35].upper(),
        npi="1234567890",                        # placeholder; real NPI from company settings
        tax_id="123456789",                      # placeholder; real EIN
        address_line="1 Company Way",
        city="Arlington",
        state="VA",
        zip_code="22201",
    )
    return submitter, receiver, provider


async def _subscriber_for_claim(db: AsyncSession, claim: Claim) -> EDISubscriber:
    """In NEMT, the patient IS the subscriber (Medicaid). Pull their address from the trip."""
    trip = await db.scalar(select(Trip).where(Trip.id == claim.trip_id, Trip.company_id == claim.company_id))
    patient = await db.get(Patient, trip.patient_id) if trip else None
    if patient is None:
        raise HTTPException(422, "Claim has no patient data")
    return EDISubscriber(
        member_id=(trip.authorization_number or f"M-{patient.id.hex[:10].upper()}"),
        first_name=patient.first_name,
        last_name=patient.last_name,
        dob=patient.dob if hasattr(patient, "dob") else date(1950, 1, 1),
        gender="U",
        address_line="1 Home St",
        city="Arlington",
        state="VA",
        zip_code="22201",
    )


def _payer_for_claim(claim: Claim) -> EDIPayer:
    return EDIPayer(name="MEDICAID", payer_id="VA01")     # placeholder


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


@router.get("/reports/summary", response_model=SummaryOut)
async def reports_summary(
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    actor: User = READ,
    db: AsyncSession = Depends(get_db),
):
    """Revenue, denial rate, DSO, and top brokers for a date range.

    `from` and `to` are inclusive. If `to < from`, you get a 422 from Pydantic.
    """
    if to_date < from_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "'to' must be on or after 'from'")
    data = await service.summary_report(db, actor.company_id, from_date, to_date)
    return data


@router.get("/claims.csv")
async def claims_csv(
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    actor: User = READ,
    db: AsyncSession = Depends(get_db),
):
    """CSV export for the given date range, suitable for Excel or a bookkeeper."""
    if to_date < from_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "'to' must be on or after 'from'")
    claims = list((await db.scalars(select(Claim).where(
        Claim.company_id == actor.company_id,
        Claim.service_date >= from_date, Claim.service_date <= to_date,
    ).order_by(Claim.service_date, Claim.claim_number))).all())
    csv_body = service.claims_to_csv(claims)
    fname = f"claims_{from_date.isoformat()}_{to_date.isoformat()}.csv"
    return PlainTextResponse(
        csv_body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/claims/{claim_id}/837p.edi")
async def claim_837p(claim_id: UUID, actor: User = READ, db: AsyncSession = Depends(get_db)):
    """A single-claim 837P file, ready for a clearinghouse."""
    claim = await get_or_404(db, Claim, claim_id, actor.company_id, "Claim")
    lines = (await db.scalars(select(ClaimLine).where(ClaimLine.claim_id == claim.id)
                              .order_by(ClaimLine.line_number))).all()
    submitter, receiver, provider = await _billing_config(db, actor.company_id)
    subscriber = await _subscriber_for_claim(db, claim)
    payer = _payer_for_claim(claim)
    body = generate_837p(
        [(claim, list(lines))],
        submitter=submitter, receiver=receiver, billing_provider=provider,
        subscriber_for=lambda c: subscriber, payer_for=lambda c: payer,
    )
    fname = f"{claim.claim_number}.837p"
    return PlainTextResponse(body, media_type="application/edi-x12",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/claims/{claim_id}/cms1500.pdf")
async def claim_cms1500(claim_id: UUID, actor: User = READ, db: AsyncSession = Depends(get_db)):
    """A printable CMS-1500 PDF for this claim."""
    claim = await get_or_404(db, Claim, claim_id, actor.company_id, "Claim")
    lines = list((await db.scalars(select(ClaimLine).where(ClaimLine.claim_id == claim.id)
                                   .order_by(ClaimLine.line_number))).all())
    _, _, provider = await _billing_config(db, actor.company_id)
    subscriber = await _subscriber_for_claim(db, claim)
    payer = _payer_for_claim(claim)
    pdf = render_cms1500(claim, lines, subscriber=subscriber, payer=payer, billing_provider=provider)
    fname = f"{claim.claim_number}.pdf"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/claims/batch-837p")
async def batch_837p(body: BatchSubmitIn, actor: User = READ, db: AsyncSession = Depends(get_db)):
    """One 837P file containing every claim in the list, each as its own transaction set."""
    claims: list[tuple[Claim, list[ClaimLine]]] = []
    subs: dict[UUID, EDISubscriber] = {}
    for cid in body.claim_ids:
        claim = await db.scalar(select(Claim).where(Claim.id == cid, Claim.company_id == actor.company_id))
        if claim is None:
            raise HTTPException(404, f"Claim {cid} not found")
        lines = list((await db.scalars(select(ClaimLine).where(ClaimLine.claim_id == claim.id)
                                       .order_by(ClaimLine.line_number))).all())
        claims.append((claim, lines))
        subs[claim.id] = await _subscriber_for_claim(db, claim)
    submitter, receiver, provider = await _billing_config(db, actor.company_id)
    body_str = generate_837p(
        claims,
        submitter=submitter, receiver=receiver, billing_provider=provider,
        subscriber_for=lambda c: subs[c.id],
        payer_for=_payer_for_claim,
    )
    return PlainTextResponse(body_str, media_type="application/edi-x12",
                             headers={"Content-Disposition": 'attachment; filename="batch.837p"'})