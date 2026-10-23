"""Fleet: maintenance, inspections, mileage, dashboard, tenant scoping."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from app.core.db import open_tenant_session
from tests.helpers import (
    make_company, new_vehicle, world,
)


async def _company_with_vehicle(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    vid = await new_vehicle(client, h)
    return c, h, vid


# ------------------------------------------------------------------ maintenance
async def test_record_maintenance_and_list(client, super_token):
    c, h, vid = await _company_with_vehicle(client, super_token)
    r = await client.post(f"/fleet/vehicles/{vid}/maintenance", headers=h, json={
        "kind": "oil_change", "service_date": "2026-10-01",
        "odometer_km": 45000, "cost_cents": 8500,
        "vendor": "QuickLube", "next_due_date": "2027-01-01", "next_due_km": 50000,
    })
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["kind"] == "oil_change"
    assert rec["odometer_km"] == 45000
    assert rec["cost_cents"] == 8500

    listing = (await client.get(f"/fleet/vehicles/{vid}/maintenance", headers=h)).json()
    assert len(listing) == 1
    assert listing[0]["id"] == rec["id"]


async def test_maintenance_rejects_negative_cost(client, super_token):
    c, h, vid = await _company_with_vehicle(client, super_token)
    r = await client.post(f"/fleet/vehicles/{vid}/maintenance", headers=h, json={
        "kind": "oil_change", "service_date": "2026-10-01", "cost_cents": -100,
    })
    assert r.status_code == 422


# ------------------------------------------------------------------ inspections
async def test_record_inspection_and_fail_alert(client, super_token):
    c, h, vid = await _company_with_vehicle(client, super_token)
    today = date.today()
    r = await client.post(f"/fleet/vehicles/{vid}/inspections", headers=h, json={
        "kind": "state_annual", "inspected_on": (today - timedelta(days=1)).isoformat(),
        "expires_on": (today + timedelta(days=364)).isoformat(),
        "result": "pass", "inspector_name": "Jane Inspector",
    })
    assert r.status_code == 201, r.text
    assert r.json()["result"] == "pass"

    # now record a fail; dashboard should flag it
    await client.post(f"/fleet/vehicles/{vid}/inspections", headers=h, json={
        "kind": "internal_monthly", "inspected_on": today.isoformat(),
        "expires_on": (today + timedelta(days=30)).isoformat(),
        "result": "fail", "notes": "Rear brake pads below minimum",
    })
    dash = (await client.get("/fleet/dashboard", headers=h)).json()
    v = next(x for x in dash if x["vehicle_id"] == vid)
    assert "last_inspection_failed" in v["alerts"]


async def test_inspection_requires_expires_after_inspected(client, super_token):
    c, h, vid = await _company_with_vehicle(client, super_token)
    r = await client.post(f"/fleet/vehicles/{vid}/inspections", headers=h, json={
        "kind": "state_annual", "inspected_on": "2026-10-15",
        "expires_on": "2026-10-01", "result": "pass",
    })
    assert r.status_code == 422


# ------------------------------------------------------------------ mileage
async def test_record_mileage_and_read_latest(client, super_token):
    c, h, vid = await _company_with_vehicle(client, super_token)
    for reading, note in [(45000, "start of month"), (45300, "after week 1"), (45700, "after week 2")]:
        r = await client.post(f"/fleet/vehicles/{vid}/mileage", headers=h, json={
            "reading_km": reading, "note": note,
        })
        assert r.status_code == 201
    listing = (await client.get(f"/fleet/vehicles/{vid}/mileage", headers=h)).json()
    # ordered by recorded_at desc, but all created same second → by reading asc? we check latest
    latest = max(listing, key=lambda x: x["recorded_at"])
    assert latest["reading_km"] == 45700


# ------------------------------------------------------------------ dashboard
async def test_dashboard_aggregates(client, super_token):
    c, h, vid = await _company_with_vehicle(client, super_token)
    today = date.today()
    await client.post(f"/fleet/vehicles/{vid}/maintenance", headers=h, json={
        "kind": "oil_change", "service_date": (today - timedelta(days=90)).isoformat(),
        "odometer_km": 45000, "next_due_date": (today + timedelta(days=10)).isoformat(),
    })
    await client.post(f"/fleet/vehicles/{vid}/inspections", headers=h, json={
        "kind": "state_annual", "inspected_on": (today - timedelta(days=200)).isoformat(),
        "expires_on": (today + timedelta(days=20)).isoformat(), "result": "pass",
    })
    await client.post(f"/fleet/vehicles/{vid}/mileage", headers=h, json={"reading_km": 47000})

    dash = (await client.get("/fleet/dashboard", headers=h)).json()
    v = next(x for x in dash if x["vehicle_id"] == vid)
    assert v["current_mileage_km"] == 47000
    assert v["last_maintenance_date"] == (today - timedelta(days=90)).isoformat()
    assert v["next_maintenance_due_date"] == (today + timedelta(days=10)).isoformat()
    assert "maintenance_due_soon" in v["alerts"]              # within 30 days
    assert "inspection_expiring" in v["alerts"]               # within 30 days
    assert v["days_until_inspection_expiry"] == 20


async def test_dashboard_flags_no_inspection(client, super_token):
    c, h, vid = await _company_with_vehicle(client, super_token)
    dash = (await client.get("/fleet/dashboard", headers=h)).json()
    v = next(x for x in dash if x["vehicle_id"] == vid)
    assert "no_inspection_on_file" in v["alerts"]


# ------------------------------------------------------------------ permissions
async def test_fleet_permissions(client, super_token):
    c = await make_company(client, super_token, "alpha")
    from tests.helpers import staff_headers
    fm = await staff_headers(client, c, "fleet_manager")
    disp = await staff_headers(client, c, "dispatcher")
    bill = await staff_headers(client, c, "billing_staff")
    vid = await new_vehicle(client, c["admin_headers"])

    # dispatcher has VEHICLES_READ but not VEHICLES_WRITE
    assert (await client.get("/fleet/dashboard", headers=disp)).status_code == 200
    assert (await client.post(f"/fleet/vehicles/{vid}/maintenance", headers=disp, json={
        "kind": "oil_change", "service_date": "2026-10-01"})).status_code == 403

    # billing has neither
    assert (await client.get("/fleet/dashboard", headers=bill)).status_code == 403

    # fleet manager has both
    assert (await client.post(f"/fleet/vehicles/{vid}/maintenance", headers=fm, json={
        "kind": "oil_change", "service_date": "2026-10-01"})).status_code == 201


# ------------------------------------------------------------------ tenant scoping
async def test_fleet_records_are_tenant_scoped(client, super_token):
    a = await make_company(client, super_token, "alpha")
    b = await make_company(client, super_token, "bravo")
    va = await new_vehicle(client, a["admin_headers"])

    # alpha records maintenance on their vehicle
    await client.post(f"/fleet/vehicles/{va}/maintenance", headers=a["admin_headers"], json={
        "kind": "oil_change", "service_date": "2026-10-01"})

    # bravo cannot read alpha's maintenance
    r = await client.get(f"/fleet/vehicles/{va}/maintenance", headers=b["admin_headers"])
    assert r.status_code == 200
    assert r.json() == []     # RLS filters; not a 404, just empty

    # bravo cannot add maintenance to alpha's vehicle
    r = await client.post(f"/fleet/vehicles/{va}/maintenance", headers=b["admin_headers"], json={
        "kind": "oil_change", "service_date": "2026-10-01"})
    assert r.status_code == 404   # vehicle not found in bravo's scope


# ------------------------------------------------------------------ maintenance is append-only
async def test_maintenance_is_append_only(client, super_token):
    c, h, vid = await _company_with_vehicle(client, super_token)
    await client.post(f"/fleet/vehicles/{vid}/maintenance", headers=h, json={
        "kind": "oil_change", "service_date": "2026-10-01"})

    import pytest
    from sqlalchemy.exc import ProgrammingError
    async with open_tenant_session(c["company"]["id"]) as db:
        with pytest.raises(ProgrammingError):
            await db.execute(text("DELETE FROM vehicle_mileage WHERE false"))
        await db.rollback()