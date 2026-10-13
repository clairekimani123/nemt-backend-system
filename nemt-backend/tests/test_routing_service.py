"""Service-level tests for the routing engine with real DB rows."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.db import open_tenant_session
from tests.helpers import (
    at,
    make_company,
    new_driver,
    new_facility,
    new_patient,
    new_trip,
    new_vehicle,
    world,
)


async def test_plan_persists_routes_and_stops(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    w = await world(client, c)
    await client.patch(f"/vehicles/{w['vehicle_id']}", headers=h,
                       json={"default_driver_id": w["driver"]["id"]})
    await new_trip(client, h, w["patient"], w["facility_id"], at(3, 9))
    await new_trip(client, h, w["patient"], w["facility_id"], at(3, 15))

    day = at(3).date().isoformat()
    plan = (await client.post("/routing/plans", headers=h, json={"on_date": day})).json()
                                                                

    async with open_tenant_session(c["company"]["id"]) as db:
        routes = await db.scalar(text("SELECT count(*) FROM routes WHERE plan_id = :p"), {"p": plan["id"]})
        stops = await db.scalar(text("SELECT count(*) FROM route_stops rs JOIN routes r ON r.id = rs.route_id"
                                     " WHERE r.plan_id = :p"), {"p": plan["id"]})
    assert routes == 1
    assert stops == 4   # 2 trips × (pickup + dropoff)


async def test_plan_uses_rls(client, super_token):
    """A plan created in alpha is invisible from bravo at the DB level."""
    a = await make_company(client, super_token, "alpha")
    b = await make_company(client, super_token, "bravo")
    for co in (a, b):
        w = await world(client, co)
        await client.patch(f"/vehicles/{w['vehicle_id']}", headers=co["admin_headers"],
                           json={"default_driver_id": w["driver"]["id"]})
        await new_trip(client, co["admin_headers"], w["patient"], w["facility_id"], at(3, 9))

    day = at(3).date().isoformat()
    pa = (await client.post("/routing/plans", headers=a["admin_headers"], json={"on_date": day})).json()
    pb = (await client.post("/routing/plans", headers=b["admin_headers"], json={"on_date": day})).json()

    async with open_tenant_session(a["company"]["id"]) as db:
        visible = await db.scalar(text("SELECT count(*) FROM route_plans WHERE id = :p"), {"p": pb["id"]})
    assert visible == 0   # bravo's plan is invisible from alpha's session


async def test_route_stops_are_append_only(client, super_token):
    from sqlalchemy.exc import ProgrammingError
    import pytest

    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    w = await world(client, c)
    await client.patch(f"/vehicles/{w['vehicle_id']}", headers=h,
                       json={"default_driver_id": w["driver"]["id"]})
    await new_trip(client, h, w["patient"], w["facility_id"], at(3, 9))
    day = at(3).date().isoformat()
    await client.post("/routing/plans", headers=h, json={"on_date": day})

    async with open_tenant_session(c["company"]["id"]) as db:
        with pytest.raises(ProgrammingError):
            await db.execute(text("UPDATE route_stops SET sequence = 99 WHERE false"))
        await db.rollback()
        with pytest.raises(ProgrammingError):
            await db.execute(text("DELETE FROM route_stops WHERE false"))


async def test_applying_a_second_plan_supersedes_the_first(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    w = await world(client, c)
    await client.patch(f"/vehicles/{w['vehicle_id']}", headers=h,
    json={"default_driver_id": w["driver"]["id"]})
    await new_trip(client, h, w["patient"], w["facility_id"], at(3, 9))
    day_date = at(3).date()
    day = day_date.isoformat()

    first = (await client.post("/routing/plans", headers=h, json={"on_date": day})).json()
    await client.post(f"/routing/plans/{first['id']}/apply", headers=h)

    # unassign so the second plan has something to do
    trips = (await client.get("/trips", headers=h, params={"on_date": day})).json()
    for t in trips:
        await client.post(f"/trips/{t['id']}/unassign", headers=h)

    second = (await client.post("/routing/plans", headers=h,
                                json={"on_date": day, "include_assigned": True})).json()
    await client.post(f"/routing/plans/{second['id']}/apply", headers=h)

    async with open_tenant_session(c["company"]["id"]) as db:
        rows = (await db.execute(text(
            "SELECT status, count(*) FROM routes WHERE route_date = :d GROUP BY status"
        ), {"d": day_date})).all()
    by_status = {r[0]: r[1] for r in rows}
    assert by_status.get("superseded", 0) >= 1
    assert by_status.get("published", 0) == 1