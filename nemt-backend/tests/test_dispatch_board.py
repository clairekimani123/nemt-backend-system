from datetime import datetime, timedelta

from tests.helpers import (
    NY,
    assign,
    at_local,
    bearer,
    local_date,
    login,
    make_company,
    new_driver,
    new_facility,
    new_patient,
    new_trip,
    new_vehicle,
    staff_headers,
)


async def test_board_groups_the_day_into_lanes(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    f, p = await new_facility(client, h), await new_patient(client, h)
    d, v = await new_driver(client, h), await new_vehicle(client, h)
    trips = [await new_trip(client, h, p, f, at_local(1, hour)) for hour in (8, 10, 12, 14)]   # tomorrow, local times
    await assign(client, h, trips[0]["id"], d["id"], v)
    await client.post(f"/trips/{trips[1]['id']}/status", headers=h, json={"status": "cancelled", "reason_code": "weather"})
    day = (datetime.now(NY).date() + timedelta(days=1)).isoformat()

    b = (await client.get("/dispatch/board", headers=h, params={"on_date": day})).json()
    assert b["counts"] == {"unassigned": 2, "assigned": 1, "in_progress": 0, "completed": 0, "cancelled": 1, "no_show": 0}
    assert [t["id"] for t in b["lanes"]["assigned"]] == [trips[0]["id"]]
    assert {t["id"] for t in b["lanes"]["unassigned"]} == {trips[2]["id"], trips[3]["id"]}
    assert [t["pickup"]["line1"] for t in b["lanes"]["assigned"]] == ["1 Home St"]
    empty = (await client.get("/dispatch/board", headers=h, params={"on_date": (datetime.now(NY).date() + timedelta(days=9)).isoformat()})).json()
    assert sum(empty["counts"].values()) == 0

    filtered = (await client.get("/trips", headers=h, params={"on_date": day, "unassigned": True})).json()
    assert len(filtered) == 2
    by_driver = (await client.get("/trips", headers=h, params={"driver_id": d["id"]})).json()
    assert [t["id"] for t in by_driver] == [trips[0]["id"]]


async def test_board_permissions_and_isolation(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    f, p = await new_facility(client, h), await new_patient(client, h)
    await new_trip(client, h, p, f, at_local(1, 9))
    day = (datetime.now(NY).date() + timedelta(days=1)).isoformat()
    bill = await staff_headers(client, c, "billing_staff")
    fleet = await staff_headers(client, c, "fleet_manager")
    drv = await new_driver(client, h)
    hd = bearer((await login(client, "alpha", drv["email"]))["access_token"])
    assert (await client.get("/dispatch/board", headers=bill, params={"on_date": day})).status_code == 200
    assert (await client.get("/dispatch/board", headers=fleet, params={"on_date": day})).status_code == 403
    assert (await client.get("/dispatch/board", headers=hd, params={"on_date": day})).status_code == 403
    other = await make_company(client, super_token, "bravo")
    theirs = (await client.get("/dispatch/board", headers=other["admin_headers"], params={"on_date": day})).json()
    assert sum(theirs["counts"].values()) == 0
    assert local_date