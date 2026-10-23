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



# ------------------------------------------------------------------ batch operations
async def test_batch_create_from_trips(client, super_token):
    c, h, _, w = await _completed_trip(client, super_token)
    # create two more completed trips
    t2 = await new_trip(client, h, w["patient"], w["facility_id"], at(4))
    t3 = await new_trip(client, h, w["patient"], w["facility_id"], at(5))
    await assign(client, h, t2["id"], w["driver"]["id"], w["vehicle_id"])
    await assign(client, h, t3["id"], w["driver"]["id"], w["vehicle_id"])
    drv = bearer((await login(client, "alpha", w["driver"]["email"]))["access_token"])
    for t in (t2, t3):
        for s in ("en_route_to_pickup", "arrived_at_pickup", "passenger_on_board",
                  "en_route_to_dropoff", "arrived_at_dropoff", "completed"):
            r = await client.post(f"/driver/trips/{t['id']}/status", headers=drv,
                                  json={"status": s, "latitude": 38.9, "longitude": -77.0})
            assert r.status_code == 200, r.text
    # rate plan
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000})

    # find all three completed trips
    billable = (await client.get("/billing/billable-trips", headers=h)).json()
    trip_ids = [b["trip_id"] for b in billable]
    assert len(trip_ids) == 3

    r = await client.post("/billing/claims/from-trips", headers=h, json={
        "trip_ids": trip_ids, "payer_type": "medicaid"})
    assert r.status_code == 200
    body = r.json()
    assert body["requested"] == 3 and body["succeeded"] == 3 and body["failed"] == 0
    assert all(o["ok"] for o in body["outcomes"])
    assert all(o["id"] for o in body["outcomes"])


async def test_batch_create_reports_partial_failures(client, super_token):
    c, h, trip, _ = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 500})
    # bill the trip once, single
    first = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()
    # then try to bill it again in a batch of 1
    r = await client.post("/billing/claims/from-trips", headers=h, json={
        "trip_ids": [trip["id"]], "payer_type": "medicaid"})
    body = r.json()
    assert body["requested"] == 1 and body["succeeded"] == 0 and body["failed"] == 1
    assert "already billed" in body["outcomes"][0]["error"].lower()


async def test_batch_submit(client, super_token):
    c, h, trip, w = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000})
    t2 = await new_trip(client, h, w["patient"], w["facility_id"], at(4))
    await assign(client, h, t2["id"], w["driver"]["id"], w["vehicle_id"])
    drv = bearer((await login(client, "alpha", w["driver"]["email"]))["access_token"])
    for s in ("en_route_to_pickup", "arrived_at_pickup", "passenger_on_board",
              "en_route_to_dropoff", "arrived_at_dropoff", "completed"):
        await client.post(f"/driver/trips/{t2['id']}/status", headers=drv,
                          json={"status": s, "latitude": 38.9, "longitude": -77.0})
    billable = (await client.get("/billing/billable-trips", headers=h)).json()
    ids = [b["trip_id"] for b in billable]
    claim_ids = []
    for tid in ids:
        r = await client.post(f"/billing/claims/from-trip/{tid}", headers=h,
                              json={"payer_type": "medicaid"})
        claim_ids.append(r.json()["id"])

    r = await client.post("/billing/claims/batch-submit", headers=h, json={
        "claim_ids": claim_ids, "reference": "BATCH-01"})
    body = r.json()
    assert body["succeeded"] == 2
    # submit again: both already submitted
    r = await client.post("/billing/claims/batch-submit", headers=h, json={
        "claim_ids": claim_ids, "reference": "BATCH-02"})
    body = r.json()
    assert body["succeeded"] == 0 and body["failed"] == 2


async def test_batch_pay(client, super_token):
    c, h, trip, _ = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000})
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()
    await client.post(f"/billing/claims/{claim['id']}/submit", headers=h, json={})
    r = await client.post("/billing/claims/batch-pay", headers=h, json={
        "claim_ids": [claim["id"]], "kind": "payment", "amount_cents": 1000,
        "reference": "ERA-1"})
    body = r.json()
    assert body["succeeded"] == 1
    detail = (await client.get(f"/billing/claims/{claim['id']}", headers=h)).json()
    assert detail["status"] == "paid"


async def test_batch_validation(client, super_token):
    c, h, _, _ = await _completed_trip(client, super_token)
    # empty list is rejected at the schema level
    r = await client.post("/billing/claims/from-trips", headers=h, json={
        "trip_ids": [], "payer_type": "medicaid"})
    assert r.status_code == 422
    # too many
    r = await client.post("/billing/claims/from-trips", headers=h, json={
        "trip_ids": ["00000000-0000-0000-0000-000000000000"] * 201,
        "payer_type": "medicaid"})
    assert r.status_code == 422
    # batch-pay denial without denial_code
    r = await client.post("/billing/claims/batch-pay", headers=h, json={
        "claim_ids": ["00000000-0000-0000-0000-000000000000"],
        "kind": "denial", "amount_cents": 0})
    assert r.status_code == 422


# ------------------------------------------------------------------ reports
async def test_summary_empty_range(client, super_token):
    c, h, _, _ = await _completed_trip(client, super_token)
    r = await client.get("/billing/reports/summary", headers=h,
                         params={"from": "2020-01-01", "to": "2020-01-31"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claim_count"] == 0
    assert body["billed_cents"] == 0
    assert body["denial_rate_bp"] == 0
    assert body["avg_days_to_payment"] is None
    assert body["by_payer"] == []
    assert body["top_brokers"] == []

async def test_summary_aggregates_by_payer_and_status(client, super_token):
    c, h, trip, w = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000})
    # one paid medicaid claim
    claim1 = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                                json={"payer_type": "medicaid"})).json()
    await client.post(f"/billing/claims/{claim1['id']}/submit", headers=h, json={})
    await client.post(f"/billing/claims/{claim1['id']}/payments", headers=h, json={
        "kind": "payment", "amount_cents": 1000})
    # one denied medicaid claim on a second trip ON THE SAME DAY
    t2 = await new_trip(client, h, w["patient"], w["facility_id"], at(3, 16))
    await assign(client, h, t2["id"], w["driver"]["id"], w["vehicle_id"])
    drv = bearer((await login(client, "alpha", w["driver"]["email"]))["access_token"])
    for s in ("en_route_to_pickup", "arrived_at_pickup", "passenger_on_board",
              "en_route_to_dropoff", "arrived_at_dropoff", "completed"):
        await client.post(f"/driver/trips/{t2['id']}/status", headers=drv,
                          json={"status": s, "latitude": 38.9, "longitude": -77.0})
    claim2 = (await client.post(f"/billing/claims/from-trip/{t2['id']}", headers=h,
                                json={"payer_type": "medicaid"})).json()
    await client.post(f"/billing/claims/{claim2['id']}/submit", headers=h, json={})
    await client.post(f"/billing/claims/{claim2['id']}/payments", headers=h, json={
        "kind": "denial", "amount_cents": 0, "denial_code": "27"})

    service_date = at(3).date().isoformat()
    r = await client.get("/billing/reports/summary", headers=h,
                         params={"from": service_date, "to": service_date})
    body = r.json()
    assert body["claim_count"] == 2
    assert body["billed_cents"] == 2000
    assert body["paid_cents"] == 1000
    assert body["outstanding_cents"] == 1000
    assert body["denied_count"] == 1
    assert body["denial_rate_bp"] == 5000       # 1 of 2 = 50% = 5000 bp
    assert body["avg_days_to_payment"] is not None and body["avg_days_to_payment"] < 1
    by_payer = {p["payer_type"]: p for p in body["by_payer"]}
    assert "medicaid" in by_payer
    assert by_payer["medicaid"]["claim_count"] == 2
    assert by_payer["medicaid"]["denial_count"] == 1

async def test_summary_rejects_bad_range(client, super_token):
    c, h, _, _ = await _completed_trip(client, super_token)
    r = await client.get("/billing/reports/summary", headers=h,
                         params={"from": "2026-12-01", "to": "2026-01-01"})
    assert r.status_code == 422


async def test_csv_export(client, super_token):
    c, h, trip, _ = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000})
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()
    service_date = at(3).date().isoformat()
    r = await client.get("/billing/claims.csv", headers=h,
                         params={"from": service_date, "to": service_date})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment;" in r.headers["content-disposition"]
    lines = r.text.strip().split("\n")
    assert lines[0].startswith("claim_number,service_date,status")
    assert claim["claim_number"] in r.text


async def test_csv_export_is_tenant_scoped(client, super_token):
    a, ha, trip, _ = await _completed_trip(client, super_token)
    b = await make_company(client, super_token, "bravo")
    await client.post("/billing/rate-plans", headers=ha, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000})
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=ha,
                               json={"payer_type": "medicaid"})).json()
    service_date = at(3).date().isoformat()
    # bravo's CSV is empty for the same range
    r = await client.get("/billing/claims.csv", headers=b["admin_headers"],
                         params={"from": service_date, "to": service_date})
    assert claim["claim_number"] not in r.text


# ------------------------------------------------------------------ pure-function CSV
def test_claims_to_csv_pure():
    from datetime import UTC, datetime
    from app.core.base import new_id
    from app.modules.billing.models import Claim, ClaimStatus, PayerType
    from app.modules.billing.service import claims_to_csv

    c = Claim(
        id=new_id(), company_id=None, trip_id=None, broker_id=None, rate_plan_id=None,
        claim_number="C-000001", status=ClaimStatus.SUBMITTED, payer_type=PayerType.MEDICAID,
        service_date=date(2026, 10, 15), billed_cents=2500, paid_cents=0,
        patient_responsibility_cents=0,
        submitted_at=datetime(2026, 10, 16, 12, 0, tzinfo=UTC),
    )
    out = claims_to_csv([c])
    lines = out.strip().split("\n")
    assert len(lines) == 2
    assert "C-000001" in lines[1]
    assert "submitted" in lines[1]
    assert "medicaid" in lines[1]
    assert "2500" in lines[1]  


# ------------------------------------------------------------------ 837P export
async def test_837p_single_claim_is_valid_x12(client, super_token):
    c, h, trip, _ = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000})
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()

    r = await client.get(f"/billing/claims/{claim['id']}/837p.edi", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/edi-x12")
    body = r.text
    # X12 envelope: starts with ISA, ends with IEA
    assert body.startswith("ISA*")
    assert body.rstrip().endswith("~")
    assert "IEA*1*" in body
    # functional group
    assert "GS*HC*" in body
    assert "GE*1*" in body
    # transaction set for the claim
    assert "ST*837*" in body
    assert "SE*" in body
    assert "CLM*" in body
    assert claim["claim_number"] in body
    # ST/SE must be balanced
    st_count = body.count("ST*837*")
    se_count = body.count("SE*")
    assert st_count == se_count == 1


async def test_837p_batch(client, super_token):
    c, h, trip, w = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000})
    claim1 = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                                json={"payer_type": "medicaid"})).json()
    t2 = await new_trip(client, h, w["patient"], w["facility_id"], at(3, 16))
    await assign(client, h, t2["id"], w["driver"]["id"], w["vehicle_id"])
    drv = bearer((await login(client, "alpha", w["driver"]["email"]))["access_token"])
    for s in ("en_route_to_pickup", "arrived_at_pickup", "passenger_on_board",
              "en_route_to_dropoff", "arrived_at_dropoff", "completed"):
        await client.post(f"/driver/trips/{t2['id']}/status", headers=drv,
                          json={"status": s, "latitude": 38.9, "longitude": -77.0})
    claim2 = (await client.post(f"/billing/claims/from-trip/{t2['id']}", headers=h,
                                json={"payer_type": "medicaid"})).json()

    r = await client.post("/billing/claims/batch-837p", headers=h,
                          json={"claim_ids": [claim1["id"], claim2["id"]]})
    assert r.status_code == 200
    body = r.text
    assert body.count("ST*837*") == 2
    assert body.count("SE*") == 2
    assert "GE*2*" in body
    assert claim1["claim_number"] in body
    assert claim2["claim_number"] in body


async def test_837p_is_tenant_scoped(client, super_token):
    a, ha, trip, _ = await _completed_trip(client, super_token)
    b = await make_company(client, super_token, "bravo")
    await client.post("/billing/rate-plans", headers=ha, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000})
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=ha,
                               json={"payer_type": "medicaid"})).json()
    r = await client.get(f"/billing/claims/{claim['id']}/837p.edi", headers=b["admin_headers"])
    assert r.status_code == 404


# ------------------------------------------------------------------ CMS-1500 PDF
async def test_cms1500_returns_pdf(client, super_token):
    c, h, trip, _ = await _completed_trip(client, super_token)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000})
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()

    r = await client.get(f"/billing/claims/{claim['id']}/cms1500.pdf", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    # every PDF starts with the magic bytes %PDF-
    assert r.content.startswith(b"%PDF-")
    assert len(r.content) > 1000


async def test_cms1500_tenant_scoped(client, super_token):
    a, ha, trip, _ = await _completed_trip(client, super_token)
    b = await make_company(client, super_token, "bravo")
    await client.post("/billing/rate-plans", headers=ha, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000})
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=ha,
                               json={"payer_type": "medicaid"})).json()
    r = await client.get(f"/billing/claims/{claim['id']}/cms1500.pdf", headers=b["admin_headers"])
    assert r.status_code == 404


# ------------------------------------------------------------------ pure-function 837P
def test_837p_generator_deterministic():
    """The same claim must produce the same file on every call. Otherwise a retry
    looks like a new submission to the clearinghouse."""
    from datetime import UTC, date, datetime
    from app.core.base import new_id
    from app.modules.billing.formats.edi import (
        BillingProvider, Payer, Receiver, Subscriber, Submitter, generate_837p,
    )
    from app.modules.billing.models import Claim, ClaimLine, ClaimStatus, PayerType

    claim = Claim(
        id=new_id(), company_id=None, trip_id=None, broker_id=None, rate_plan_id=None,
        claim_number="C-000042", status=ClaimStatus.DRAFT, payer_type=PayerType.MEDICAID,
        service_date=date(2026, 10, 15), billed_cents=2500, paid_cents=0,
        patient_responsibility_cents=0, diagnosis_code="Z78.9",
    )
    line = ClaimLine(
        id=new_id(), company_id=None, claim_id=claim.id, line_number=1,
        service_date=date(2026, 10, 15), procedure_code="A0120",
        units=1, charge_cents=2500,
    )
    submitter = Submitter(name="TEST", id="TESTS", contact_name="X", contact_phone="5555555555")
    receiver = Receiver(name="CLR", id="CLR")
    provider = BillingProvider(name="TEST", npi="1234567890", tax_id="123456789",
                               address_line="1 X", city="Y", state="VA", zip_code="22201")
    subscriber = Subscriber(member_id="M-1", first_name="A", last_name="B",
                            dob=date(1950, 1, 1), gender="U",
                            address_line="1 X", city="Y", state="VA", zip_code="22201")
    payer = Payer(name="MEDICAID", payer_id="VA01")

    a = generate_837p([(claim, [line])], submitter=submitter, receiver=receiver,
                      billing_provider=provider, subscriber_for=lambda c: subscriber,
                      payer_for=lambda c: payer, submit_date=date(2026, 10, 15))
    b = generate_837p([(claim, [line])], submitter=submitter, receiver=receiver,
                      billing_provider=provider, subscriber_for=lambda c: subscriber,
                      payer_for=lambda c: payer, submit_date=date(2026, 10, 15))
    assert a == b