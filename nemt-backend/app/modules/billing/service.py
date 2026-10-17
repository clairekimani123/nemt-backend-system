"""Billing service: rate resolution, charge computation, claim lifecycle.

Pricing is deliberately simple in 7.1: pick the applicable rate plan (broker-specific
beats generic; explicit beats default; newest effective_from wins), compute base +
distance + time + mobility surcharge, apply an after-hours multiplier, round to cents.
Contract carve-outs, per-procedure codes, and modifiers come in 7.2 as new rules on top
of this function, not by rewriting it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base import new_id
from app.core.crud import get_or_404
from app.modules.audit import service as audit
from app.modules.billing.models import (
    Claim, ClaimLine, ClaimPayment, ClaimStatus, PayerType, PaymentKind, RatePlan,
)
from app.modules.billing.schemas import BillableTripOut, ClaimDetailOut, ClaimOut
from app.modules.trips.models import Trip
from app.modules.trips.state import TripStatus
from app.modules.users.models import User
from sqlalchemy import text as _text
from app.modules.billing.models import RateRule, RuleKind 


def _http(code: int, detail: object) -> HTTPException:
    return HTTPException(code, detail)


# ------------------------------------------------------------------ rate plans
def clear_default_rate_plan_stmt(company_id: UUID):
    return update(RatePlan).where(RatePlan.company_id == company_id).values(is_default=False)


async def resolve_rate_plan(db: AsyncSession, company_id: UUID, broker_id: UUID | None,
                            service_date: date, explicit: UUID | None = None) -> RatePlan | None:
    """Pick the rate plan that applies to a trip on a given date.

    Order of preference:
      1. an explicitly named plan (if supplied)
      2. a plan tied to the trip's broker
      3. the company's default plan
    Each candidate must be in effect on `service_date`.
    """
    if explicit is not None:
        plan = await db.scalar(select(RatePlan).where(RatePlan.id == explicit,
                                                      RatePlan.company_id == company_id))
        if plan is None:
            raise _http(404, "Rate plan not found")
        return plan

    window = and_(RatePlan.effective_from <= service_date,
                  or_(RatePlan.effective_to.is_(None), RatePlan.effective_to >= service_date))
    if broker_id is not None:
        plan = await db.scalar(
            select(RatePlan).where(RatePlan.company_id == company_id,
                                   RatePlan.broker_id == broker_id, window)
            .order_by(RatePlan.effective_from.desc()))
        if plan is not None:
            return plan
    return await db.scalar(
        select(RatePlan).where(RatePlan.company_id == company_id,
                               RatePlan.is_default.is_(True), window)
        .order_by(RatePlan.effective_from.desc()))


def compute_charge_cents(plan: RatePlan, *, distance_km: float, duration_min: int,
                         mobility: str, pickup_at: datetime, wait_minutes: int = 0,
                         late_cancel: bool = False, no_show: bool = False) -> int:
    """The billable amount for one trip, in cents.

    Kept as a pure function so it can be unit-tested without a DB and reused by 7.2's
    contract-specific rules without changing this call site.
    """
    cents = plan.base_fare_cents
    cents += round(plan.per_mile_cents * distance_km)
    cents += plan.per_minute_cents * max(0, duration_min)
    cents += plan.wheelchair_cents if mobility in ("wheelchair_manual", "wheelchair_power") else 0
    cents += plan.stretcher_cents if mobility == "stretcher" else 0
    cents += plan.wait_per_minute_cents * max(0, wait_minutes)

    if no_show:
        return plan.no_show_fee_cents
    if late_cancel:
        return plan.late_cancel_fee_cents

    # after-hours multiplier: 10000 bp == 1.0x (no-op)
    local_hour = pickup_at.hour       # the caller normalises to company local time
    if local_hour < 6 or local_hour >= 20:
        cents = cents * plan.after_hours_multiplier_bp // 10_000
    return max(0, cents)


# ------------------------------------------------------------------ billable trips
def billable_trip_out(t: Trip) -> BillableTripOut:
    distance_km = 0.0
    if t.pickup_latitude is not None and t.dropoff_latitude is not None:
        # cheaper than PostGIS for a single row; the reader can swap to ST_Distance if they need
        from math import asin, cos, radians, sin, sqrt
        r = 6371.0
        p1, p2 = radians(t.pickup_latitude), radians(t.dropoff_latitude)
        dp = p2 - p1
        dl = radians(t.dropoff_longitude - t.pickup_longitude)
        h = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
        distance_km = 2 * r * asin(sqrt(h))
    duration_min = t.estimated_duration_min
    return BillableTripOut(
        trip_id=t.id,
        trip_ref=f"T-{t.trip_number:06d}",
        service_date=t.scheduled_pickup_at.date(),
        patient_first_name="",          # filled by the router with a joined read
        patient_last_name="",
        mobility=t.mobility.value,
        broker_id=t.broker_id,
        authorization_number=t.authorization_number,
        distance_km=round(distance_km, 2),
        duration_minutes=duration_min,
        completed_at=t.completed_at,
    )


# ------------------------------------------------------------------ claim creation
async def _next_claim_number(db: AsyncSession, company_id: UUID) -> str:
    """A per-company monotonic claim number with a C- prefix. Same locking trick as trips."""
    
    value = await db.scalar(_text("""
        INSERT INTO counters (company_id, name, value) VALUES (:c, 'claim', 1)
        ON CONFLICT (company_id, name) DO UPDATE SET value = counters.value + 1
        RETURNING value
    """), {"c": company_id})
    return f"C-{value:07d}"


async def create_claim_from_trip(db: AsyncSession, actor: User, trip_id: UUID,
                                 payer_type: PayerType, rate_plan_id: UUID | None,
                                 request: Request) -> Claim:
    cid = actor.company_id
    trip = await db.scalar(select(Trip).where(Trip.id == trip_id, Trip.company_id == cid)
                           .with_for_update())
    if trip is None:
        raise _http(404, "Trip not found")
    if trip.status != TripStatus.COMPLETED:
        raise _http(409, "Only completed trips can be billed")
    existing = await db.scalar(select(Claim).where(Claim.company_id == cid, Claim.trip_id == trip.id))
    if existing is not None:
        raise _http(409, f"Trip already billed as {existing.claim_number}")

    service_date = trip.scheduled_pickup_at.date()
    plan = await resolve_rate_plan(db, cid, trip.broker_id, service_date, rate_plan_id)
    if plan is None:
        raise _http(422, "No applicable rate plan; create one first or pass rate_plan_id")

    charge = await compute_charge_for_trip(db, cid, trip, plan)

    claim = Claim(
        id=new_id(), company_id=cid, trip_id=trip.id, broker_id=trip.broker_id,
        rate_plan_id=plan.id, claim_number=await _next_claim_number(db, cid),
        status=ClaimStatus.DRAFT, payer_type=payer_type, service_date=service_date,
        billed_cents=charge,
    )
    db.add(claim)
    await db.flush()
    db.add(ClaimLine(
        id=new_id(), company_id=cid, claim_id=claim.id, line_number=1,
        service_date=service_date, procedure_code="A0120",       # NEMT ambulatory, placeholder
        units=1, charge_cents=charge,
    ))
    await audit.record(db, action="billing.claim_created", actor=actor, resource_type="claim",
                       resource_id=claim.id, request=request,
                       meta={"trip_id": str(trip.id), "billed_cents": charge})
    await db.commit()
    await db.refresh(claim)
    return claim


def _distance_km(t: Trip) -> float:
    if None in (t.pickup_latitude, t.pickup_longitude, t.dropoff_latitude, t.dropoff_longitude):
        return 0.0
    from math import asin, cos, radians, sin, sqrt
    r = 6371.0
    p1, p2 = radians(t.pickup_latitude), radians(t.dropoff_latitude)
    dp = p2 - p1
    dl = radians(t.dropoff_longitude - t.pickup_longitude)
    h = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * r * asin(sqrt(h))


# ------------------------------------------------------------------ lifecycle
async def submit_claim(db: AsyncSession, actor: User, claim_id: UUID, reference: str | None,
                       request: Request) -> Claim:
    claim = await get_or_404(db, Claim, claim_id, actor.company_id, "Claim")
    if claim.status != ClaimStatus.DRAFT:
        raise _http(409, f"A {claim.status.value} claim cannot be submitted")
    claim.status = ClaimStatus.SUBMITTED
    claim.submitted_at = datetime.now(UTC)
    await audit.record(db, action="billing.claim_submitted", actor=actor, resource_type="claim",
                       resource_id=claim.id, request=request, meta={"reference": reference})
    await db.commit()
    await db.refresh(claim)
    return claim


async def post_payment(db: AsyncSession, actor: User, claim_id: UUID, kind: PaymentKind,
                       amount_cents: int, reference: str | None, note: str | None,
                       denial_code: str | None, request: Request) -> Claim:
    claim = await get_or_404(db, Claim, claim_id, actor.company_id, "Claim")
    if claim.status in (ClaimStatus.VOID, ClaimStatus.WRITTEN_OFF):
        raise _http(409, f"A {claim.status.value} claim cannot receive payments")
    db.add(ClaimPayment(
        company_id=claim.company_id, claim_id=claim.id, kind=kind,
        amount_cents=amount_cents, reference=reference, note=note,
    ))
    if kind == PaymentKind.PAYMENT:
        claim.paid_cents += amount_cents
        if claim.paid_cents >= claim.billed_cents:
            claim.status = ClaimStatus.PAID
            claim.resolved_at = datetime.now(UTC)
        else:
            claim.status = ClaimStatus.PARTIAL
    elif kind == PaymentKind.ADJUSTMENT:
        # a write-down counts against the outstanding balance but does not resolve the claim
        claim.allowed_cents = max(0, claim.billed_cents + amount_cents)
    elif kind == PaymentKind.DENIAL:
        claim.status = ClaimStatus.DENIED
        claim.denial_code = denial_code
        claim.denial_note = note
        claim.resolved_at = datetime.now(UTC)
    await audit.record(db, action="billing.payment_posted", actor=actor, resource_type="claim",
                       resource_id=claim.id, request=request,
                       meta={"kind": kind.value, "amount_cents": amount_cents})
    await db.commit()
    await db.refresh(claim)
    return claim


async def write_off(db: AsyncSession, actor: User, claim_id: UUID, note: str | None,
                    request: Request) -> Claim:
    claim = await get_or_404(db, Claim, claim_id, actor.company_id, "Claim")
    if claim.status in (ClaimStatus.PAID, ClaimStatus.VOID):
        raise _http(409, f"A {claim.status.value} claim cannot be written off")
    claim.status = ClaimStatus.WRITTEN_OFF
    claim.resolved_at = datetime.now(UTC)
    await audit.record(db, action="billing.claim_written_off", actor=actor, resource_type="claim",
                       resource_id=claim.id, request=request, meta={"note": note})
    await db.commit()
    await db.refresh(claim)
    return claim


# ------------------------------------------------------------------ detail assembly
async def claim_detail(db: AsyncSession, company_id: UUID, claim_id: UUID) -> ClaimDetailOut:
    claim = await get_or_404(db, Claim, claim_id, company_id, "Claim")
    lines = (await db.scalars(select(ClaimLine).where(ClaimLine.claim_id == claim.id)
                              .order_by(ClaimLine.line_number))).all()
    payments = (await db.scalars(select(ClaimPayment).where(ClaimPayment.claim_id == claim.id)
                                 .order_by(ClaimPayment.posted_at))).all()
    out = ClaimOut.model_validate(claim).model_dump()
    return ClaimDetailOut(**out,
                          lines=[line for line in lines],
                          payments=[p for p in payments])


# ------------------------------------------------------------------ aging
async def aging(db: AsyncSession, company_id: UUID) -> list[dict]:
    """Outstanding claims grouped by how long they've been waiting since submission.

    Buckets are 0-30, 31-60, 61-90, 91-120, 120+. A claim that has never been submitted
    is grouped under '0-30' at its created_at date — that's the operator's choice to make,
    but it keeps unsubmitted drafts visible.
    """
    rows = (await db.execute(select(Claim).where(
        Claim.company_id == company_id,
        Claim.status.in_({ClaimStatus.SUBMITTED, ClaimStatus.PARTIAL, ClaimStatus.DENIED}),
    ))).scalars().all()
    now = datetime.now(UTC)
    buckets = {"0-30": [0, 0], "31-60": [0, 0], "61-90": [0, 0], "91-120": [0, 0], "120+": [0, 0]}
    for c in rows:
        anchor = c.submitted_at or c.created_at
        days = (now - anchor).days
        key = ("0-30" if days <= 30 else "31-60" if days <= 60
               else "61-90" if days <= 90 else "91-120" if days <= 120 else "120+")
        outstanding = max(0, c.billed_cents - c.paid_cents)
        buckets[key][0] += 1
        buckets[key][1] += outstanding
    return [{"bucket": k, "count": v[0], "billed_cents": v[1], "outstanding_cents": v[1]}
            for k, v in buckets.items()]



def _weekday_matches(mask: int | None, d: date) -> bool:
    """weekday_mask bit 0 = Monday. `date.weekday()` returns 0 = Monday too, so they align."""
    if mask is None:
        return True
    return bool(mask & (1 << d.weekday()))


def _rule_matches(rule: RateRule, *, broker_id: UUID | None, procedure_code: str | None,
                  mobility: str, service_date: date) -> bool:
    if rule.broker_id is not None and rule.broker_id != broker_id:
        return False
    if rule.procedure_code is not None and rule.procedure_code != procedure_code:
        return False
    if rule.mobility is not None and rule.mobility != mobility:
        return False
    return _weekday_matches(rule.weekday_mask, service_date)


def apply_rules(cents: int, rules: list[RateRule], *, broker_id: UUID | None,
                procedure_code: str | None, mobility: str, service_date: date) -> int:
    """Walk rules in priority order (highest first) and apply the ones that match."""
    ordered = sorted(rules, key=lambda r: (-r.priority, str(r.id)))
    for rule in ordered:
        if not _rule_matches(rule, broker_id=broker_id, procedure_code=procedure_code,
                             mobility=mobility, service_date=service_date):
            continue
        if rule.kind == RuleKind.FLAT:
            cents = rule.amount_cents or 0
        elif rule.kind == RuleKind.MULTIPLIER:
            cents = cents * (rule.multiplier_bp or 10_000) // 10_000
        if rule.exclusive:
            break
    return max(0, cents)


async def compute_charge_for_trip(db: AsyncSession, company_id: UUID, trip: Trip,
                                  plan: RatePlan, procedure_code: str | None = "A0120") -> int:
    """Full pricing pipeline: base computation + rule overrides."""
    cents = compute_charge_cents(
        plan,
        distance_km=_distance_km(trip),
        duration_min=trip.estimated_duration_min,
        mobility=trip.mobility.value,
        pickup_at=trip.scheduled_pickup_at,
        no_show=False,
        late_cancel=False,
    )
    rules = list((await db.scalars(select(RateRule).where(
        RateRule.company_id == company_id, RateRule.rate_plan_id == plan.id))).all())
    if not rules:
        return cents
    return apply_rules(cents, rules, broker_id=trip.broker_id, procedure_code=procedure_code,
                       mobility=trip.mobility.value, service_date=trip.scheduled_pickup_at.date())


 # ------------------------------------------------------------------ batch operations
async def create_claims_from_trips(db: AsyncSession, actor: User, trip_ids: list[UUID],
                                   payer_type: PayerType, rate_plan_id: UUID | None,
                                   request: Request) -> list[dict]:
    """Create claims for each trip. Never aborts the batch on a single failure.

    Returns one dict per input (in input order) with shape:
        {"index": int, "ok": bool, "id": UUID | None, "error": str | None}

    The whole batch is committed at the end — partial successes inside one call are still
    persisted, because the operator asked for all of them and expects at least the ones
    that could succeed to stick.
    """
    outcomes: list[dict] = []
    for i, trip_id in enumerate(trip_ids):
        try:
            claim = await _create_one_claim_no_commit(db, actor, trip_id, payer_type,
                                                      rate_plan_id, request)
            outcomes.append({"index": i, "ok": True, "id": claim.id, "error": None})
        except HTTPException as exc:
            # roll the failed item back to the savepoint so the session stays usable
            await db.rollback()
            outcomes.append({"index": i, "ok": False, "id": None,
                             "error": _flatten_http_detail(exc.detail)})
        except Exception as exc:
            await db.rollback()
            outcomes.append({"index": i, "ok": False, "id": None, "error": str(exc)[:300]})
    await db.commit()
    return outcomes


def _flatten_http_detail(detail: object) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        return detail.get("message") or str(detail)
    return str(detail)


async def _create_one_claim_no_commit(db: AsyncSession, actor: User, trip_id: UUID,
                                      payer_type: PayerType, rate_plan_id: UUID | None,
                                      request: Request) -> Claim:
    """Same body as `create_claim_from_trip`, minus the `db.commit()` and `db.refresh()`.

    Keeping the pricing and claim-creation logic in one place means the batch and the
    single-trip endpoint can never drift apart.
    """
    cid = actor.company_id
    trip = await db.scalar(select(Trip).where(Trip.id == trip_id, Trip.company_id == cid)
                           .with_for_update())
    if trip is None:
        raise _http(404, "Trip not found")
    if trip.status != TripStatus.COMPLETED:
        raise _http(409, f"Trip {trip.trip_number} is not completed")
    existing = await db.scalar(select(Claim).where(Claim.company_id == cid, Claim.trip_id == trip.id))
    if existing is not None:
        raise _http(409, f"Trip {trip.trip_number} already billed as {existing.claim_number}")
    service_date = trip.scheduled_pickup_at.date()
    plan = await resolve_rate_plan(db, cid, trip.broker_id, service_date, rate_plan_id)
    if plan is None:
        raise _http(422, "No applicable rate plan for this trip")
    charge = await compute_charge_for_trip(db, cid, trip, plan)
    claim = Claim(
        id=new_id(), company_id=cid, trip_id=trip.id, broker_id=trip.broker_id,
        rate_plan_id=plan.id, claim_number=await _next_claim_number(db, cid),
        status=ClaimStatus.DRAFT, payer_type=payer_type, service_date=service_date,
        billed_cents=charge,
    )
    db.add(claim)
    await db.flush()
    db.add(ClaimLine(
        id=new_id(), company_id=cid, claim_id=claim.id, line_number=1,
        service_date=service_date, procedure_code="A0120", units=1, charge_cents=charge,
    ))
    await audit.record(db, action="billing.claim_created", actor=actor, resource_type="claim",
                       resource_id=claim.id, request=request,
                       meta={"trip_id": str(trip.id), "billed_cents": charge})
    return claim


async def submit_claims_batch(db: AsyncSession, actor: User, claim_ids: list[UUID],
                              reference: str | None, request: Request) -> list[dict]:
    outcomes: list[dict] = []
    now = datetime.now(UTC)
    for i, cid in enumerate(claim_ids):
        try:
            claim = await db.scalar(select(Claim).where(Claim.id == cid,
                                                        Claim.company_id == actor.company_id)
                                    .with_for_update())
            if claim is None:
                raise _http(404, "Claim not found")
            if claim.status != ClaimStatus.DRAFT:
                raise _http(409, f"Claim {claim.claim_number} is {claim.status.value}")
            claim.status = ClaimStatus.SUBMITTED
            claim.submitted_at = now
            await audit.record(db, action="billing.claim_submitted", actor=actor,
                               resource_type="claim", resource_id=claim.id, request=request,
                               meta={"reference": reference, "batch": True})
            outcomes.append({"index": i, "ok": True, "id": claim.id, "error": None})
        except HTTPException as exc:
            await db.rollback()
            outcomes.append({"index": i, "ok": False, "id": None,
                             "error": _flatten_http_detail(exc.detail)})
        except Exception as exc:
            await db.rollback()
            outcomes.append({"index": i, "ok": False, "id": None, "error": str(exc)[:300]})
    await db.commit()
    return outcomes


async def post_payment_batch(db: AsyncSession, actor: User, claim_ids: list[UUID],
                             kind: PaymentKind, amount_cents: int, reference: str | None,
                             note: str | None, denial_code: str | None,
                             request: Request) -> list[dict]:
    outcomes: list[dict] = []
    for i, cid in enumerate(claim_ids):
        try:
            claim = await db.scalar(select(Claim).where(Claim.id == cid,
                                                        Claim.company_id == actor.company_id)
                                    .with_for_update())
            if claim is None:
                raise _http(404, "Claim not found")
            if claim.status in (ClaimStatus.VOID, ClaimStatus.WRITTEN_OFF):
                raise _http(409, f"Claim {claim.claim_number} is {claim.status.value}")
            db.add(ClaimPayment(
                company_id=claim.company_id, claim_id=claim.id, kind=kind,
                amount_cents=amount_cents, reference=reference, note=note,
            ))
            if kind == PaymentKind.PAYMENT:
                claim.paid_cents += amount_cents
                if claim.paid_cents >= claim.billed_cents:
                    claim.status = ClaimStatus.PAID
                    claim.resolved_at = datetime.now(UTC)
                else:
                    claim.status = ClaimStatus.PARTIAL
            elif kind == PaymentKind.ADJUSTMENT:
                claim.allowed_cents = max(0, claim.billed_cents + amount_cents)
            elif kind == PaymentKind.DENIAL:
                claim.status = ClaimStatus.DENIED
                claim.denial_code = denial_code
                claim.denial_note = note
                claim.resolved_at = datetime.now(UTC)
            await audit.record(db, action="billing.payment_posted", actor=actor,
                               resource_type="claim", resource_id=claim.id, request=request,
                               meta={"kind": kind.value, "amount_cents": amount_cents, "batch": True})
            outcomes.append({"index": i, "ok": True, "id": claim.id, "error": None})
        except HTTPException as exc:
            await db.rollback()
            outcomes.append({"index": i, "ok": False, "id": None,
                             "error": _flatten_http_detail(exc.detail)})
        except Exception as exc:
            await db.rollback()
            outcomes.append({"index": i, "ok": False, "id": None, "error": str(exc)[:300]})
    await db.commit()
    return outcomes                      