"""The routing API end to end: plan, apply, discard, routes, geometry, estimate, driver manifest."""

from datetime import UTC, date, datetime, timedelta

from tests.helpers import (
    assign,
    at,
    bearer,
    codes,
    login,
    make_company,
    new_driver,
    new_facility,
    new_patient,
    new_trip,
    new_vehicle,
    staff_headers,
    world,
)


async def _company_with_trips(client, super_token, n_trips=3, patients=3):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    f = await new_facility(client, h)
    ps = [await new_patient(client, h, n=i) for i in range(1, patients + 1)]
    trips = [await new_trip(client, h, ps[i], f, at(3, 9 + i)) for i in range(n_trips)]
    d = await new_driver(client, h)
    v = await new_vehicle(client, h)
    # wire driver as the vehicle's default driver so the plan can auto-discover them
    await client.patch(f"/vehicles/{v}", headers=h, json={"default_driver_id": d["id"]})
    return c, h, f, ps, trips, d, v


async def test_plan_produces_a_proposal_without_changing_anything(client, super_token):
    c, h, f, ps, trips, d, v = await _company_with_trips(client, super_token)
    day = at(3).date().isoformat()

    r = await client.post("/routing/plans", headers=h, json={"on_date": day})
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["status"] == "proposed"
    assert plan["metrics"]["trips_requested"] == 3
    assert plan["metrics"]["trips_planned"] == 3
    assert plan["metrics"]["vehicles_used"] == 1
    assert plan["metrics"]["routing_source"] in {"haversine", "osrm"}
    assert len(plan["routes"]) == 1
    route = plan["routes"][0]
    assert len(route["stops"]) == 6  # 3 pickups + 3 dropoffs

    # nothing has changed on the trips yet
    for t in trips:
        got = (await client.get(f"/trips/{t['id']}", headers=h)).json()
        assert got["status"] == "scheduled"
        assert got["driver"] is None


async def test_apply_assigns_every_trip_and_publishes_the_route(client, super_token):
    c, h, f, ps, trips, d, v = await _company_with_trips(client, super_token)
    day_date = at(3).date()
    day = day_date.isoformat()

    plan = (await client.post("/routing/plans", headers=h, json={"on_date": day})).json()
    apply = await client.post(f"/routing/plans/{plan['id']}/apply", headers=h)
    assert apply.status_code == 200
    out = apply.json()
    assert out["trips_assigned"] == 3
    assert out["routes_published"] == 1

    # now the trips are assigned to the driver
    for t in trips:
        got = (await client.get(f"/trips/{t['id']}", headers=h)).json()
        assert got["status"] == "assigned"
        assert got["driver"]["id"] == d["id"]

    # the plan is marked applied
    plan_after = (await client.get(f"/routing/plans/{plan['id']}", headers=h)).json()
    assert plan_after["status"] == "applied"


async def test_apply_is_all_or_nothing_when_a_trip_was_reassigned_elsewhere(client, super_token):
    c, h, f, ps, trips, d, v = await _company_with_trips(client, super_token)
    day = at(3).date().isoformat()

    plan = (await client.post("/routing/plans", headers=h, json={"on_date": day})).json()

    # between planning and applying, someone else reassigns a trip to a different driver
    other = await new_driver(client, h, n=9)
    other_v = await new_vehicle(client, h, n=9)
    assert (await assign(client, h, trips[0]["id"], other["id"], other_v)).status_code == 200

    r = await client.post(f"/routing/plans/{plan['id']}/apply", headers=h)
    assert r.status_code == 409
    body = r.json()["detail"]
    assert "out of date" in body["message"]
    # and the plan did not change anything
    plan_after = (await client.get(f"/routing/plans/{plan['id']}", headers=h)).json()
    assert plan_after["status"] == "proposed"


async def test_discard_leaves_nothing_behind(client, super_token):
    c, h, f, ps, trips, d, v = await _company_with_trips(client, super_token)
    day = at(3).date().isoformat()
    plan = (await client.post("/routing/plans", headers=h, json={"on_date": day})).json()

    r = await client.delete(f"/routing/plans/{plan['id']}", headers=h)
    assert r.status_code == 204

    plan_after = (await client.get(f"/routing/plans/{plan['id']}", headers=h)).json()
    assert plan_after["status"] == "discarded"
    # trips were never touched
    for t in trips:
        assert (await client.get(f"/trips/{t['id']}", headers=h)).json()["status"] == "scheduled"
    # discarding twice is refused
    assert (await client.delete(f"/routing/plans/{plan['id']}", headers=h)).status_code == 409


async def test_routes_listing_and_geometry(client, super_token):
    c, h, f, ps, trips, d, v = await _company_with_trips(client, super_token)
    day = at(3).date().isoformat()
    plan = (await client.post("/routing/plans", headers=h, json={"on_date": day})).json()
    await client.post(f"/routing/plans/{plan['id']}/apply", headers=h)

    routes = (await client.get("/routing/routes", headers=h, params={"on_date": day})).json()
    assert len(routes) == 1
    assert routes[0]["status"] == "published"
    assert routes[0]["drive_minutes"] > 0

    route_id = routes[0]["id"]
    one = (await client.get(f"/routing/routes/{route_id}", headers=h)).json()
    assert one["id"] == route_id
    assert [s["sequence"] for s in one["stops"]] == [1, 2, 3, 4, 5, 6]

    geom = (await client.get(f"/routing/routes/{route_id}/geometry", headers=h)).json()
    assert geom["type"] == "LineString"
    assert len(geom["coordinates"]) == 6
    assert geom["distance_km"] > 0


async def test_estimate_returns_eta_and_appointment_slack(client, super_token):
    c, h, f, ps, trips, d, v = await _company_with_trips(client, super_token)
    t = trips[0]
    r = await client.post(f"/routing/trips/{t['id']}/estimate", headers=h, json={})
    assert r.status_code == 200
    out = r.json()
    assert out["drive_minutes"] > 0
    assert out["distance_km"] > 0
    assert out["dwell_minutes"] >= 0
    assert out["estimated_duration_min"] >= 10
    assert out["applied"] is False


async def test_estimate_apply_updates_the_trip(client, super_token):
    c, h, f, ps, trips, d, v = await _company_with_trips(client, super_token)
    t = trips[0]
    r = await client.post(f"/routing/trips/{t['id']}/estimate", headers=h, json={"apply": True})
    assert r.status_code == 200
    assert r.json()["applied"] is True
    t_after = (await client.get(f"/trips/{t['id']}", headers=h)).json()
    assert t_after["estimated_duration_min"] == r.json()["estimated_duration_min"]


async def test_plan_respects_permissions(client, super_token):
    c, h, f, ps, trips, d, v = await _company_with_trips(client, super_token)
    day = at(3).date().isoformat()
    disp = await staff_headers(client, c, "dispatcher")
    bill = await staff_headers(client, c, "billing_staff")
    fleet = await staff_headers(client, c, "fleet_manager")

    assert (await client.post("/routing/plans", headers=bill, json={"on_date": day})).status_code == 403
    assert (await client.post("/routing/plans", headers=fleet, json={"on_date": day})).status_code == 403
    r = await client.post("/routing/plans", headers=disp, json={"on_date": day})
    assert r.status_code == 201
    plan_id = r.json()["id"]
    # billing can read
    assert (await client.get(f"/routing/plans/{plan_id}", headers=bill)).status_code == 200
    # fleet cannot
    assert (await client.get(f"/routing/plans/{plan_id}", headers=fleet)).status_code == 403


async def test_plan_is_tenant_scoped(client, super_token):
    a = await make_company(client, super_token, "alpha")
    b = await make_company(client, super_token, "bravo")
    ha, hb = a["admin_headers"], b["admin_headers"]
    fa = await new_facility(client, ha)
    pa = await new_patient(client, ha)
    da = await new_driver(client, ha)
    va = await new_vehicle(client, ha)
    await client.patch(f"/vehicles/{va}", headers=ha, json={"default_driver_id": da["id"]})
    await new_trip(client, ha, pa, fa, at(3))

    plan_a = (await client.post("/routing/plans", headers=ha,
                                json={"on_date": at(3).date().isoformat()})).json()
    # bravo cannot see it
    assert (await client.get(f"/routing/plans/{plan_a['id']}", headers=hb)).status_code == 404
    # bravo cannot apply it
    assert (await client.post(f"/routing/plans/{plan_a['id']}/apply", headers=hb)).status_code == 404


async def test_driver_route_endpoint(client, super_token):
    c, h, f, ps, trips, d, v = await _company_with_trips(client, super_token)
    day = at(3).date().isoformat()
    plan = (await client.post("/routing/plans", headers=h, json={"on_date": day})).json()
    await client.post(f"/routing/plans/{plan['id']}/apply", headers=h)

    dt = bearer((await login(client, "alpha", d["email"]))["access_token"])
    r = await client.get("/driver/route", headers=dt, params={"on_date": day})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["route_date"] == day
    assert out["vehicle_unit"] == "V-01"
    assert len(out["stops"]) == 6
    # a driver sees no PHI
    for stop in out["stops"]:
        for forbidden in ("date_of_birth", "medicaid_id", "notes", "broker_id"):
            assert forbidden not in stop


async def test_driver_route_404_when_nothing_published(client, super_token):
    c, h, f, ps, trips, d, v = await _company_with_trips(client, super_token)
    dt = bearer((await login(client, "alpha", d["email"]))["access_token"])
    # no plan applied yet
    r = await client.get("/driver/route", headers=dt,
                         params={"on_date": at(3).date().isoformat()})
    assert r.status_code == 404


async def test_plan_validation_errors(client, super_token):
    c, h, f, ps, trips, d, v = await _company_with_trips(client, super_token)
    day = at(3).date().isoformat()
    # shift_start without shift_end
    r = await client.post("/routing/plans", headers=h,
                          json={"on_date": day, "shift_start": "06:00:00"})
    assert r.status_code == 422
    # shift_end <= shift_start
    r = await client.post("/routing/plans", headers=h,
                          json={"on_date": day, "shift_start": "06:00:00", "shift_end": "05:00:00"})
    assert r.status_code == 422
    # no usable resources
    # (there is one usable resource by default; pass resources explicitly to make it worse)
    bogus = {"driver_id": "00000000-0000-0000-0000-000000000000",
             "vehicle_id": v}
    r = await client.post("/routing/plans", headers=h,
                          json={"on_date": day, "resources": [bogus]})
    assert r.status_code == 404


async def test_plan_with_explicit_resources(client, super_token):
    c, h, f, ps, trips, d, v = await _company_with_trips(client, super_token)
    day = at(3).date().isoformat()
    r = await client.post("/routing/plans", headers=h, json={
        "on_date": day, "resources": [{"driver_id": d["id"], "vehicle_id": v}],
    })
    assert r.status_code == 201
    assert r.json()["metrics"]["vehicles_used"] == 1


async def test_unassigned_trips_appear_in_the_plan(client, super_token):
    """A trip nobody can serve (no compatible vehicle) shows up in `unassigned`, not silently dropped."""
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    f = await new_facility(client, h)
    p = await new_patient(client, h, n=1, mobility="stretcher")   # needs stretcher capacity
    await new_trip(client, h, p, f, at(3))
    d = await new_driver(client, h, n=1, wheelchair_certified=False)
    v = await new_vehicle(client, h, n=1)   # wheelchair van, no stretcher
    await client.patch(f"/vehicles/{v}", headers=h, json={"default_driver_id": d["id"]})

    day = at(3).date().isoformat()
    plan = (await client.post("/routing/plans", headers=h, json={"on_date": day})).json()
    assert plan["metrics"]["trips_planned"] == 0
    assert len(plan["unassigned"]) == 1
    assert "vehicle_cannot_carry" in plan["unassigned"][0]["reasons"] or \
           "driver_not_certified" in plan["unassigned"][0]["reasons"]