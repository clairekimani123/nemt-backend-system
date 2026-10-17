"""Billing: rate plans, pricing rules, claims, lifecycle, aging, tenant isolation."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from app.core.db import open_tenant_session
from app.modules.billing.models import ClaimStatus
from tests.helpers import (
    PW, assign, at, bearer, login, make_company, new_driver, new_facility, new_patient,
    new_trip, new_vehicle, staff_headers, world,
)


async def _completed_trip(client, super_token, **trip_kw):
    """A company with one completed, assigned trip. Returns (company, headers, trip)."""
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    w = await world(client, c)
    trip = await new_trip(client, h, w["patient"], w["facility_id"], at(3), **trip_kw)
    await assign(client, h, trip["id"], w["driver"]["id"], w["vehicle_id"])
    drv = bearer((await login(client, "alpha", w["driver"]["email"]))["access_token"])
    for s in ("en_route_to_pickup", "arrived_at_pickup", "passenger_on_board",
              "en_route_to_dropoff", "arrived_at_dropoff", "completed"):
        r = await client.post(f"/driver/trips/{trip['id']}/status", headers=drv,
                              json={"status": s, "latitude": 38.9, "longitude": -77.0})
        assert r.status_code == 200, r.text
    return c, h, trip, w


# ------------------------------------------------------------------ rate plans


async def test_rate_plan_crud_and_default_swap(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    r = await client.post("/billing/rate-plans", headers=h, json={
        "name": "Medicaid 2026", "is_default": True, "effective_from": "2026-01-01",
        "base_fare_cents": 1200, "per_mile_cents": 250, "per_minute_cents": 30,
    })
    assert r.status_code == 201, r.text
    plan_id = r.json()["id"]
    # second default replaces the first
    r2 = await client.post("/billing/rate-plans", headers=h, json={
        "name": "Private 2026", "is_default": True, "effective_from": "2026-06-01",
        "base_fare_cents": 2000,
    })
    assert r2.status_code == 201
    plans = (await client.get("/billing/rate-plans", headers=h)).json()
    defaults = [p for p in plans if p["is_default"]]
    assert len(defaults) == 1 and defaults[0]["name"] == "Private 2026"
    # patch works
    r3 = await client.patch(f"/billing/rate-plans/{plan_id}", headers=h,
                            json={"base_fare_cents": 1500})
    assert r3.status_code == 200 and r3.json()["base_fare_cents"] == 1500


async def test_rate_plan_permissions(client, super_token):
    c = await make_company(client, super_token, "alpha")
    disp = await staff_headers(client, c, "dispatcher")
    bill = await staff_headers(client, c, "billing_staff")
    r = await client.post("/billing/rate-plans", headers=disp, json={
        "name": "X", "effective_from": "2026-01-01"})
    assert r.status_code == 403                                # dispatcher cannot write
    r = await client.post("/billing/rate-plans", headers=bill, json={
        "name": "Y", "effective_from": "2026-01-01"})
    assert r.status_code == 201                                # billing staff can


# ------------------------------------------------------------------ billable + claim creation


async def test_billable_trips_and_claim_creation(client, super_token):
    c, h, trip, _ = await _completed_trip(client, super_token)
    # no rate plan yet → billable works but claim creation fails
    billable = (await client.get("/billing/billable-trips", headers=h)).json()
    assert len(billable) == 1
    assert billable[0]["trip_ref"] == "T-000001"

    # add a rate plan
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000, "per_mile_cents": 200, "per_minute_cents": 50,
    })
    r = await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                          json={"payer_type": "medicaid"})
    assert r.status_code == 201, r.text
    claim = r.json()
    assert claim["status"] == "draft"
    assert claim["payer_type"] == "medicaid"
    assert claim["billed_cents"] > 0
    assert claim["claim_number"].startswith("C-")

    # billing the same trip twice is refused
    r2 = await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                           json={"payer_type": "medicaid"})
    assert r2.status_code == 409


async def test_billable_only_shows_completed_unbilled(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    w = await world(client, c)
    # a scheduled-but-not-completed trip
    await new_trip(client, h, w["patient"], w["facility_id"], at(3))
    billable = (await client.get("/billing/billable-trips", headers=h)).json()
    assert billable == []


# ------------------------------------------------------------------ lifecycle


async def test_claim_lifecycle_paid_in_full(client, super_token):
    c, h, trip, _ = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 5000,
    })
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()
    assert claim["billed_cents"] == 5000

    s = await client.post(f"/billing/claims/{claim['id']}/submit", headers=h,
                          json={"reference": "BATCH-1"})
    assert s.status_code == 200 and s.json()["status"] == "submitted"

    p = await client.post(f"/billing/claims/{claim['id']}/payments", headers=h, json={
        "kind": "payment", "amount_cents": 5000, "reference": "ERA-001"})
    assert p.status_code == 200 and p.json()["status"] == "paid"
    assert p.json()["paid_cents"] == 5000


async def test_claim_partial_then_denied(client, super_token):
    c, h, trip, _ = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 10000,
    })
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()
    await client.post(f"/billing/claims/{claim['id']}/submit", headers=h, json={})
    p = await client.post(f"/billing/claims/{claim['id']}/payments", headers=h, json={
        "kind": "payment", "amount_cents": 6000})
    assert p.json()["status"] == "partial"
    assert p.json()["paid_cents"] == 6000


async def test_claim_denial(client, super_token):
    c, h, trip, _ = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000,
    })
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()
    await client.post(f"/billing/claims/{claim['id']}/submit", headers=h, json={})
    d = await client.post(f"/billing/claims/{claim['id']}/payments", headers=h, json={
        "kind": "denial", "amount_cents": 0, "denial_code": "27", "note": "Missing auth"})
    assert d.status_code == 200
    body = d.json()
    assert body["status"] == "denied"
    assert body["denial_code"] == "27"


async def test_claim_write_off_and_void(client, super_token):
    c, h, trip, _ = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000,
    })
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()
    w = await client.post(f"/billing/claims/{claim['id']}/write-off", headers=h,
                          params={"note": "small balance"})
    assert w.status_code == 200 and w.json()["status"] == "written_off"
    # a written-off claim refuses payments
    p = await client.post(f"/billing/claims/{claim['id']}/payments", headers=h, json={
        "kind": "payment", "amount_cents": 100})
    assert p.status_code == 409


# ------------------------------------------------------------------ detail + aging


async def test_claim_detail_includes_lines_and_payments(client, super_token):
    c, h, trip, _ = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 2000,
    })
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()
    await client.post(f"/billing/claims/{claim['id']}/submit", headers=h, json={})
    await client.post(f"/billing/claims/{claim['id']}/payments", headers=h, json={
        "kind": "payment", "amount_cents": 500})
    detail = (await client.get(f"/billing/claims/{claim['id']}", headers=h)).json()
    assert len(detail["lines"]) == 1
    assert detail["lines"][0]["charge_cents"] == 2000
    assert len(detail["payments"]) == 1
    assert detail["payments"][0]["amount_cents"] == 500


async def test_aging_buckets(client, super_token):
    c, h, trip, _ = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 3000,
    })
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()
    await client.post(f"/billing/claims/{claim['id']}/submit", headers=h, json={})
    # backdate to force a 61-90 bucket
    async with open_tenant_session(c["company"]["id"]) as db:
        await db.execute(text(
            "UPDATE claims SET submitted_at = now() - interval '75 days' WHERE id = :i"
        ), {"i": claim["id"]})
        await db.commit()
    buckets = (await client.get("/billing/aging", headers=h)).json()
    by_name = {b["bucket"]: b for b in buckets}
    assert by_name["61-90"]["count"] == 1
    assert by_name["61-90"]["outstanding_cents"] == 3000


# ------------------------------------------------------------------ isolation


async def test_claims_are_tenant_scoped(client, super_token):
    a, ha, trip, _ = await _completed_trip(client, super_token)   # creates alpha for us
    b = await make_company(client, super_token, "bravo")
    # add a rate plan for alpha, create a claim
    await client.post("/billing/rate-plans", headers=ha, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000,
    })
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=ha,
                               json={"payer_type": "medicaid"})).json()
    # bravo cannot see it
    r = await client.get(f"/billing/claims/{claim['id']}", headers=b["admin_headers"])
    assert r.status_code == 404
# ------------------------------------------------------------------ pricing unit


def test_compute_charge_basic_and_after_hours():
    from app.modules.billing.models import RatePlan
    from app.modules.billing.service import compute_charge_cents

    plan = RatePlan(
        name="x", company_id=None, base_fare_cents=1000, per_mile_cents=200,
        per_minute_cents=50, wheelchair_cents=500, stretcher_cents=0,
        after_hours_multiplier_bp=15000, wait_per_minute_cents=100,
        no_show_fee_cents=2000, late_cancel_fee_cents=1500,
        effective_from=date(2020, 1, 1),
    )
    # daytime: no multiplier
    noon = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    cents = compute_charge_cents(plan, distance_km=5.0, duration_min=20,
                                 mobility="ambulatory", pickup_at=noon)
    assert cents == 1000 + round(200 * 5) + 50 * 20

    # wheelchair adds a surcharge
    cents = compute_charge_cents(plan, distance_km=5.0, duration_min=20,
                                 mobility="wheelchair_manual", pickup_at=noon)
    assert cents == 1000 + round(200 * 5) + 50 * 20 + 500

    # 22:00 hits after-hours
    night = datetime(2026, 10, 15, 22, 0, tzinfo=UTC)
    cents = compute_charge_cents(plan, distance_km=0, duration_min=0,
                                 mobility="ambulatory", pickup_at=night)
    assert cents == 1000 * 15000 // 10000

    # no-show short-circuits
    assert compute_charge_cents(plan, distance_km=5, duration_min=20,
                                mobility="ambulatory", pickup_at=noon, no_show=True) == 2000



    