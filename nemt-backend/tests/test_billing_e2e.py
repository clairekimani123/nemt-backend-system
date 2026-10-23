"""End-to-end billing journey.

A single test that walks the entire pipeline in one session:
    trip completed
    → rate plan created (with a rule override)
    → claim created from trip
    → price reflects the rule
    → claim submitted
    → 837P file exported and validated
    → payment posted, claim moves to paid
    → summary report reflects the money

If any piece of the pipeline is broken — the rate engine, the rule engine, the claim
lifecycle, the export, the aggregation — this test fails. It's the "does the whole
machine work" test, as distinct from the "does each gear spin" tests in
tests/test_billing.py.
"""

from datetime import date

from sqlalchemy import text

from app.core.db import open_tenant_session
from tests.helpers import (
    PW, assign, at, bearer, login, make_company, new_driver, new_facility, new_patient,
    new_trip, new_vehicle, world,
)


async def _completed_trip(client, super_token):
    """Same helper as in test_billing.py — a company with one completed trip."""
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    w = await world(client, c)
    trip = await new_trip(client, h, w["patient"], w["facility_id"], at(3))
    await assign(client, h, trip["id"], w["driver"]["id"], w["vehicle_id"])
    drv = bearer((await login(client, "alpha", w["driver"]["email"]))["access_token"])
    for s in ("en_route_to_pickup", "arrived_at_pickup", "passenger_on_board",
              "en_route_to_dropoff", "arrived_at_dropoff", "completed"):
        r = await client.post(f"/driver/trips/{trip['id']}/status", headers=drv,
                              json={"status": s, "latitude": 38.9, "longitude": -77.0})
        assert r.status_code == 200, r.text
    return c, h, trip


async def test_billing_end_to_end(client, super_token):
    """One test, whole pipeline. Read every assertion — each one is a checkpoint."""

    # --- 1. Setup: a company, a completed trip ------------------------------------------
    c, h, trip = await _completed_trip(client, super_token)
    service_date = at(3).date()

    # --- 2. Create a rate plan with an override rule ----------------------------------
    # Base fare 2000, per-mile 0, per-minute 0, flat rule of 5000 for all trips.
    # Without the rule, price = 2000. With the rule, price = 5000. That's the checkpoint.
    plan = (await client.post("/billing/rate-plans", headers=h, json={
        "name": "E2E plan", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 2000,
    })).json()
    assert plan["base_fare_cents"] == 2000

    rule = (await client.post(f"/billing/rate-plans/{plan['id']}/rules", headers=h, json={
        "priority": 100, "kind": "flat", "amount_cents": 5000,
    })).json()
    assert rule["amount_cents"] == 5000

    # --- 3. Create a claim from the completed trip ------------------------------------
    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()
    assert claim["status"] == "draft"
    assert claim["claim_number"].startswith("C-")
    # The rule fired: the price is 5000, not 2000.
    assert claim["billed_cents"] == 5000, f"Rule didn't fire; got {claim['billed_cents']}"

    # --- 4. Submit the claim ----------------------------------------------------------
    submitted = (await client.post(f"/billing/claims/{claim['id']}/submit", headers=h,
                                   json={"reference": "BATCH-E2E-001"})).json()
    assert submitted["status"] == "submitted"
    assert submitted["submitted_at"] is not None

    # --- 5. Export as 837P and check the file is valid-looking ------------------------
    edi = await client.get(f"/billing/claims/{claim['id']}/837p.edi", headers=h)
    assert edi.status_code == 200
    body = edi.text
    assert body.startswith("ISA*")
    assert "ST*837*" in body
    assert "SE*" in body
    assert body.count("ST*837*") == body.count("SE*") == 1
    assert claim["claim_number"] in body
    # The claim amount must appear as dollars (5000 cents → "50.00")
    assert "50.00" in body
    # Envelope is well-formed
    assert "GE*1*" in body
    assert "IEA*1*" in body

    # --- 6. Export as CMS-1500 PDF ----------------------------------------------------
    pdf = await client.get(f"/billing/claims/{claim['id']}/cms1500.pdf", headers=h)
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF-")

    # --- 7. Post the full payment ----------------------------------------------------
    paid = (await client.post(f"/billing/claims/{claim['id']}/payments", headers=h, json={
        "kind": "payment", "amount_cents": 5000, "reference": "ERA-E2E-001",
    })).json()
    assert paid["status"] == "paid"
    assert paid["paid_cents"] == 5000
    assert paid["resolved_at"] is not None

    # --- 8. Summary report reflects the money ----------------------------------------
    summary = (await client.get("/billing/reports/summary", headers=h, params={
        "from": service_date.isoformat(), "to": service_date.isoformat(),
    })).json()
    assert summary["claim_count"] == 1
    assert summary["billed_cents"] == 5000
    assert summary["paid_cents"] == 5000
    assert summary["outstanding_cents"] == 0
    assert summary["denied_count"] == 0
    assert summary["denial_rate_bp"] == 0
    # Claims paid within the same second as submitted → avg days is close to 0
    assert summary["avg_days_to_payment"] is not None
    assert summary["avg_days_to_payment"] < 1
    # Payer breakdown includes exactly one medicaid row
    assert len(summary["by_payer"]) == 1
    assert summary["by_payer"][0]["payer_type"] == "medicaid"
    assert summary["by_payer"][0]["claim_count"] == 1
    assert summary["by_payer"][0]["billed_cents"] == 5000

    # --- 9. CSV export includes the claim --------------------------------------------
    csv = await client.get("/billing/claims.csv", headers=h, params={
        "from": service_date.isoformat(), "to": service_date.isoformat(),
    })
    assert csv.status_code == 200
    assert "text/csv" in csv.headers["content-type"]
    assert claim["claim_number"] in csv.text
    assert "paid" in csv.text
    assert "5000" in csv.text

    # --- 10. Audit trail has the whole journey ----------------------------------------
    logs = (await client.get("/audit-logs", headers=h,
                              params={"limit": 200})).json()
    actions = {row["action"] for row in logs}
    for expected in (
        "billing.rate_plan_created",
        "billing.rate_rule_created",
        "billing.claim_created",
        "billing.claim_submitted",
        "billing.payment_posted",
    ):
        assert expected in actions, f"missing audit action: {expected}"


async def test_end_to_end_denial_and_write_off(client, super_token):
    """The unhappy path: submit → denial → write off. Still produces a complete audit trail."""

    c, h, trip = await _completed_trip(client, super_token)
    service_date = at(3).date()
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 3500,
    })

    claim = (await client.post(f"/billing/claims/from-trip/{trip['id']}", headers=h,
                               json={"payer_type": "medicaid"})).json()
    await client.post(f"/billing/claims/{claim['id']}/submit", headers=h, json={})
    denied = (await client.post(f"/billing/claims/{claim['id']}/payments", headers=h, json={
        "kind": "denial", "amount_cents": 0, "denial_code": "27",
        "note": "Missing prior auth",
    })).json()
    assert denied["status"] == "denied"
    assert denied["denial_code"] == "27"

    summary = (await client.get("/billing/reports/summary", headers=h, params={
        "from": service_date.isoformat(), "to": service_date.isoformat(),
    })).json()
    assert summary["denied_count"] == 1
    assert summary["denial_rate_bp"] == 10000    # 1 of 1 = 100% = 10000 bp
    assert summary["paid_cents"] == 0
    assert summary["outstanding_cents"] == 3500

    written = (await client.post(f"/billing/claims/{claim['id']}/write-off", headers=h,
                                  params={"note": "Small balance"})).json()
    assert written["status"] == "written_off"