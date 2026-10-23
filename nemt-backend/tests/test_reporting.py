"""Operational reporting: on-time, cancellations, no-shows, volume, utilization, leaderboard."""

from datetime import UTC, date, datetime, timedelta

from tests.helpers import (
    PW, assign, at, bearer, login, make_company, new_driver, new_facility, new_patient,
    new_trip, new_vehicle, staff_headers, world,
)


async def _company_with_trips(client, super_token, n_completed=3, n_cancelled=1, n_no_show=0):
    """Company with a mix of trip outcomes on the same day."""
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    w = await world(client, c)
    day = at(3).date()
    drv = bearer((await login(client, "alpha", w["driver"]["email"]))["access_token"])
    trips = []
    for i in range(n_completed):
        t = await new_trip(client, h, w["patient"], w["facility_id"], at(3, 9 + i))
        await assign(client, h, t["id"], w["driver"]["id"], w["vehicle_id"])
        for s in ("en_route_to_pickup", "arrived_at_pickup", "passenger_on_board",
                  "en_route_to_dropoff", "arrived_at_dropoff", "completed"):
            r = await client.post(f"/driver/trips/{t['id']}/status", headers=drv,
                                  json={"status": s, "latitude": 38.9, "longitude": -77.0})
            assert r.status_code == 200, r.text
        trips.append(t)
    for i in range(n_cancelled):
        t = await new_trip(client, h, w["patient"], w["facility_id"], at(3, 14 + i))
        await client.post(f"/trips/{t['id']}/status", headers=h,
                          json={"status": "cancelled", "reason_code": "patient_cancelled"})
        trips.append(t)
    for i in range(n_no_show):
        t = await new_trip(client, h, w["patient"], w["facility_id"], at(3, 15 + i))
        await assign(client, h, t["id"], w["driver"]["id"], w["vehicle_id"])
        for s in ("en_route_to_pickup", "arrived_at_pickup"):
            await client.post(f"/driver/trips/{t['id']}/status", headers=drv,
                              json={"status": s, "latitude": 38.9, "longitude": -77.0})
        # mark no-show (needs wait time)
        from sqlalchemy import text
        from app.core.db import open_tenant_session
        import uuid as _uuid
        async with open_tenant_session(c["company"]["id"]) as db:
            await db.execute(text(
                "UPDATE trips SET arrived_pickup_at = now() - interval '15 minutes' WHERE id = :i"
            ), {"i": _uuid.UUID(t["id"])})
            await db.commit()
        await client.post(f"/driver/trips/{t['id']}/status", headers=drv,
                          json={"status": "no_show"})
        trips.append(t)
    return c, h, w, trips, day


# ------------------------------------------------------------------ on-time
async def test_on_time_report(client, super_token):
    c, h, _, _, day = await _company_with_trips(client, super_token, n_completed=2)
    r = await client.get("/reports/on-time", headers=h,
                         params={"from": day.isoformat(), "to": day.isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall"]["total_pickups"] >= 2
    assert body["overall"]["on_time_rate_bp"] > 0
    assert len(body["by_driver"]) >= 1


async def test_on_time_empty_range(client, super_token):
    c, h, _, _, day = await _company_with_trips(client, super_token, n_completed=1)
    r = await client.get("/reports/on-time", headers=h,
                         params={"from": "2020-01-01", "to": "2020-01-31"})
    body = r.json()
    assert body["overall"]["total_pickups"] == 0
    assert body["overall"]["on_time_rate_bp"] == 0


# ------------------------------------------------------------------ cancellations
async def test_cancellations_report(client, super_token):
    c, h, _, _, day = await _company_with_trips(client, super_token,
                                                n_completed=1, n_cancelled=2)
    r = await client.get("/reports/cancellations", headers=h,
                         params={"from": day.isoformat(), "to": day.isoformat()})
    body = r.json()
    assert body["total"] == 2
    assert any(row["reason_code"] == "patient_cancelled" and row["count"] == 2
               for row in body["by_reason"])


# ------------------------------------------------------------------ no-shows
async def test_no_shows_report(client, super_token):
    c, h, _, _, day = await _company_with_trips(client, super_token,
                                                n_completed=1, n_no_show=1)
    r = await client.get("/reports/no-shows", headers=h,
                         params={"from": day.isoformat(), "to": day.isoformat()})
    body = r.json()
    assert body["total"] == 1
    assert len(body["by_driver"]) == 1


# ------------------------------------------------------------------ volume
async def test_volume_report(client, super_token):
    c, h, _, _, day = await _company_with_trips(client, super_token,
                                                n_completed=2, n_cancelled=1)
    r = await client.get("/reports/volume", headers=h,
                         params={"from": day.isoformat(), "to": day.isoformat()})
    body = r.json()
    assert body["total_trips"] == 3
    assert len(body["by_day"]) == 1
    assert body["by_day"][0]["completed"] == 2
    assert body["by_day"][0]["cancelled"] == 1
    # mobility breakdown has at least one row
    assert len(body["by_mobility"]) >= 1


# ------------------------------------------------------------------ utilization
async def test_driver_utilization(client, super_token):
    c, h, _, _, day = await _company_with_trips(client, super_token, n_completed=3)
    r = await client.get("/reports/utilization/drivers", headers=h,
                         params={"from": day.isoformat(), "to": day.isoformat()})
    body = r.json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["trips_driven"] == 3
    assert row["utilization_bp"] >= 0


async def test_vehicle_utilization(client, super_token):
    c, h, _, _, day = await _company_with_trips(client, super_token, n_completed=2)
    r = await client.get("/reports/utilization/vehicles", headers=h,
                         params={"from": day.isoformat(), "to": day.isoformat()})
    body = r.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["trips_driven"] == 2


# ------------------------------------------------------------------ revenue productivity
async def test_revenue_productivity(client, super_token):
    c, h, w, trips, day = await _company_with_trips(client, super_token, n_completed=2)
    await client.post("/billing/rate-plans", headers=h, json={
        "name": "Std", "is_default": True, "effective_from": "2020-01-01",
        "base_fare_cents": 1000, "per_mile_cents": 100})
    # query completed trips from the API — the creation-time `status` on the
    # returned objects is stale
    completed = (await client.get("/trips", headers=h,
                                  params={"status": "completed",
                                          "on_date": day.isoformat()})).json()
    assert len(completed) >= 2
    for t in completed:
        r = await client.post(f"/billing/claims/from-trip/{t['id']}", headers=h,
                              json={"payer_type": "medicaid"})
        assert r.status_code == 201, r.text
    r = await client.get("/reports/revenue-productivity", headers=h,
                         params={"from": day.isoformat(), "to": day.isoformat()})
    body = r.json()
    assert body["trips_completed"] >= 2
    assert body["billed_cents"] >= 2000
    assert body["revenue_per_trip_cents"] > 0

# ------------------------------------------------------------------ leaderboard
async def test_leaderboard(client, super_token):
    c, h, _, _, day = await _company_with_trips(client, super_token, n_completed=3)
    r = await client.get("/reports/leaderboard", headers=h,
                         params={"from": day.isoformat(), "to": day.isoformat()})
    body = r.json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["rank"] == 1
    assert row["completed_trips"] == 3
    assert row["on_time_rate_bp"] >= 0


# ------------------------------------------------------------------ permissions & isolation

async def test_reports_permissions(client, super_token):
    c = await make_company(client, super_token, "alpha")
    disp = await staff_headers(client, c, "dispatcher")
    bill = await staff_headers(client, c, "billing_staff")
    fm = await staff_headers(client, c, "fleet_manager")
    drv = await new_driver(client, c["admin_headers"])
    drv_headers = bearer((await login(client, "alpha", drv["email"]))["access_token"])

    # staff (dispatcher, billing, fleet manager) all have REPORTS_READ
    assert (await client.get("/reports/on-time", headers=disp)).status_code == 200
    assert (await client.get("/reports/on-time", headers=bill)).status_code == 200
    assert (await client.get("/reports/on-time", headers=fm)).status_code == 200
    # drivers do not — they see only their own trips
    assert (await client.get("/reports/on-time", headers=drv_headers)).status_code == 403


async def test_reports_tenant_scoped(client, super_token):
    a = await make_company(client, super_token, "alpha")
    b = await make_company(client, super_token, "bravo")
    wa = await world(client, a)
    await new_trip(client, a["admin_headers"], wa["patient"], wa["facility_id"], at(3))
    rb = (await client.get("/reports/volume", headers=b["admin_headers"])).json()
    assert rb["total_trips"] == 0


# ------------------------------------------------------------------ date validation
async def test_reports_reject_inverted_range(client, super_token):
    c = await make_company(client, super_token, "alpha")
    r = await client.get("/reports/on-time", headers=c["admin_headers"],
                         params={"from": "2026-12-01", "to": "2026-01-01"})
    assert r.status_code == 422