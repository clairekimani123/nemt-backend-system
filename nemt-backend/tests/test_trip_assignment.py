"""The assignment engine: who may drive what, and when."""

import asyncio

from tests.helpers import (
    assign,
    at,
    bearer,
    codes,
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


async def _setup(client, super_token, patients=3):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    f = await new_facility(client, h)
    ps = [await new_patient(client, h, n=i) for i in range(1, patients + 1)]
    return c, h, f, ps


async def test_assign_and_unassign_with_history(client, super_token):
    c, h, f, (p, *_) = await _setup(client, super_token)
    d, v = await new_driver(client, h), await new_vehicle(client, h)
    t = await new_trip(client, h, p, f, at(3))
    disp = await staff_headers(client, c, "dispatcher")

    check = await client.get(f"/trips/{t['id']}/assignment-check", headers=disp, params={"driver_id": d["id"], "vehicle_id": v})
    assert check.status_code == 200 and check.json()["ok"] is True
    assert (await client.get(f"/trips/{t['id']}", headers=h)).json()["status"] == "scheduled"   # a dry run changes nothing

    r = await assign(client, disp, t["id"], d["id"], v)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "assigned" and body["driver"]["full_name"] == "Driver 1" and body["vehicle"]["unit_number"] == "V-01"

    r = await client.post(f"/trips/{t['id']}/unassign", headers=disp)
    assert r.json()["status"] == "scheduled" and r.json()["driver"] is None and r.json()["vehicle"] is None
    events = (await client.get(f"/trips/{t['id']}/events", headers=h)).json()
    assert [e["event_type"] for e in events] == ["created", "assigned", "unassigned"]


async def test_vehicle_and_driver_must_suit_the_passenger(client, super_token):
    c, h, f, (p, *_) = await _setup(client, super_token)
    certified = await new_driver(client, h, n=1)
    plain = await new_driver(client, h, n=2, wheelchair_certified=False)
    van = await new_vehicle(client, h, n=1)                                                    # wheelchair van, lift
    sedan = await new_vehicle(client, h, n=2, vehicle_type="sedan", seat_capacity=3, wheelchair_capacity=0, equipment="none")
    stretcher_van = await new_vehicle(client, h, n=3, vehicle_type="stretcher_van", seat_capacity=2,
                                      wheelchair_capacity=0, stretcher_capacity=1, equipment="ramp")

    chair = await new_trip(client, h, p, f, at(3), mobility="wheelchair_manual")
    r = await assign(client, h, chair["id"], certified["id"], sedan)
    assert r.status_code == 409 and "vehicle_cannot_carry" in codes(r)                         # no wheelchair spot
    assert (await assign(client, h, chair["id"], plain["id"], van)).status_code == 409
    assert "driver_not_certified" in codes(await assign(client, h, chair["id"], plain["id"], van))
    assert (await assign(client, h, chair["id"], certified["id"], van)).status_code == 200

    bed = await new_trip(client, h, p, f, at(4), mobility="stretcher")
    assert "vehicle_cannot_carry" in codes(await assign(client, h, bed["id"], certified["id"], van))   # van has no stretcher spot
    assert (await assign(client, h, bed["id"], certified["id"], stretcher_van)).status_code == 200

    walker = await new_trip(client, h, p, f, at(5))                                            # walkers don't need certification
    assert (await assign(client, h, walker["id"], plain["id"], sedan)).status_code == 200


async def test_papers_must_be_valid_on_the_day_of_the_trip(client, super_token):
    c, h, f, (p, *_) = await _setup(client, super_token)
    when = at(10)
    day = local_date(when)
    iso = lambda n: (day + __import__("datetime").timedelta(days=n)).isoformat()  # noqa: E731
    expired = await new_driver(client, h, n=1, license_expires_on=iso(-1))
    soon = await new_driver(client, h, n=2, license_expires_on=iso(5))
    fine = await new_driver(client, h, n=3)
    bad_ins = await new_vehicle(client, h, n=1, insurance_expires_on=iso(-1))
    ok_veh = await new_vehicle(client, h, n=2)
    t = await new_trip(client, h, p, f, when)

    r = await assign(client, h, t["id"], expired["id"], ok_veh)
    assert r.status_code == 409 and "driver_license_expired" in codes(r)
    assert "vehicle_insurance_expired" in codes(await assign(client, h, t["id"], fine["id"], bad_ins))
    chk = (await client.get(f"/trips/{t['id']}/assignment-check", headers=h, params={"driver_id": soon["id"], "vehicle_id": ok_veh})).json()
    assert chk["ok"] is True and "driver_license_expiring" in {w["code"] for w in chk["warnings"]}   # allowed, but flagged
    assert (await assign(client, h, t["id"], fine["id"], ok_veh)).status_code == 200


async def test_nobody_is_in_two_places_at_once(client, super_token):
    c, h, f, (p1, p2, p3) = await _setup(client, super_token)
    d1, d2 = await new_driver(client, h, n=1), await new_driver(client, h, n=2)
    v1, v2 = await new_vehicle(client, h, n=1), await new_vehicle(client, h, n=2)
    a = await new_trip(client, h, p1, f, at(3, 14, 0))
    b = await new_trip(client, h, p2, f, at(3, 14, 15))                                        # overlaps a
    assert (await assign(client, h, a["id"], d1["id"], v1)).status_code == 200

    r = await assign(client, h, b["id"], d1["id"], v2)
    assert r.status_code == 409 and "driver_busy_other_vehicle" in codes(r)                    # same driver, other van
    r = await assign(client, h, b["id"], d2["id"], v1)
    assert r.status_code == 409 and "vehicle_busy_other_driver" in codes(r)                    # same van, other driver
    assert (await assign(client, h, b["id"], d2["id"], v2)).status_code == 200                 # fully separate: fine

    later = await new_trip(client, h, p3, f, at(3, 18, 0))                                     # hours later: no conflict
    assert (await assign(client, h, later["id"], d1["id"], v1)).status_code == 200


async def test_shared_rides_and_capacity(client, super_token):
    c, h, f, (p1, p2, p3) = await _setup(client, super_token)
    d = await new_driver(client, h)
    small = await new_vehicle(client, h, n=1, vehicle_type="minivan", seat_capacity=2, wheelchair_capacity=0, equipment="none")
    a = await new_trip(client, h, p1, f, at(3, 14, 0))
    b = await new_trip(client, h, p2, f, at(3, 14, 10))
    c3 = await new_trip(client, h, p3, f, at(3, 14, 20))
    assert (await assign(client, h, a["id"], d["id"], small)).status_code == 200
    r = await assign(client, h, b["id"], d["id"], small)                                       # 2 riders, 2 seats: shares
    assert r.status_code == 200
    chk = (await client.get(f"/trips/{b['id']}/assignment-check", headers=h, params={"driver_id": d["id"], "vehicle_id": small})).json()
    assert "shared_ride" in {w["code"] for w in chk["warnings"]}
    r = await assign(client, h, c3["id"], d["id"], small)                                      # a third rider: no seat left
    assert r.status_code == 409 and "vehicle_over_capacity" in codes(r)


async def test_capacity_is_checked_moment_by_moment(client, super_token):
    """A (10:00-10:45) and B (11:00-11:45) never share the van; T (10:30-11:15) meets only ONE of them at a time.
    A naive 'add up everyone who overlaps' check would count 3 riders in a 2-seat van and wrongly refuse."""
    c, h, f, (p1, p2, p3) = await _setup(client, super_token)
    d = await new_driver(client, h)
    van = await new_vehicle(client, h, n=1, vehicle_type="minivan", seat_capacity=2, wheelchair_capacity=0, equipment="none")
    a = await new_trip(client, h, p1, f, at(3, 10, 0))
    b = await new_trip(client, h, p2, f, at(3, 11, 0))
    t = await new_trip(client, h, p3, f, at(3, 10, 30))
    assert (await assign(client, h, a["id"], d["id"], van)).status_code == 200
    assert (await assign(client, h, b["id"], d["id"], van)).status_code == 200
    r = await assign(client, h, t["id"], d["id"], van)
    assert r.status_code == 200, r.text


async def test_two_dispatchers_racing_for_one_driver(client, super_token):
    c, h, f, (p1, p2, _) = await _setup(client, super_token)
    d = await new_driver(client, h)
    v1, v2 = await new_vehicle(client, h, n=1), await new_vehicle(client, h, n=2)
    a = await new_trip(client, h, p1, f, at(3, 14, 0))
    b = await new_trip(client, h, p2, f, at(3, 14, 10))
    r1, r2 = await asyncio.gather(assign(client, h, a["id"], d["id"], v1), assign(client, h, b["id"], d["id"], v2))
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]        # exactly one wins; never both


async def test_reassign_keeps_history(client, super_token):
    c, h, f, (p, *_) = await _setup(client, super_token)
    d1, d2 = await new_driver(client, h, n=1), await new_driver(client, h, n=2)
    v = await new_vehicle(client, h)
    t = await new_trip(client, h, p, f, at(3))
    await assign(client, h, t["id"], d1["id"], v)
    r = await assign(client, h, t["id"], d2["id"], v)
    assert r.status_code == 200 and r.json()["driver"]["id"] == d2["id"] and r.json()["status"] == "assigned"
    events = (await client.get(f"/trips/{t['id']}/events", headers=h)).json()
    assert [e["event_type"] for e in events] == ["created", "assigned", "reassigned"]
    assert events[-1]["meta"]["previous_driver_id"] == d1["id"]


async def test_permissions_and_foreign_resources(client, super_token):
    c, h, f, (p, *_) = await _setup(client, super_token)
    d, v = await new_driver(client, h), await new_vehicle(client, h)
    t = await new_trip(client, h, p, f, at(3))
    bill = await staff_headers(client, c, "billing_staff")
    assert (await assign(client, bill, t["id"], d["id"], v)).status_code == 403
    assert (await client.get(f"/trips/{t['id']}/assignment-check", headers=bill, params={"driver_id": d["id"], "vehicle_id": v})).status_code == 403
    other = await make_company(client, super_token, "bravo")
    od = await new_driver(client, other["admin_headers"])
    assert (await assign(client, h, t["id"], od["id"], v)).status_code == 404         # another company's driver
    assert bearer and login


async def test_inactive_resources_are_refused(client, super_token):
    c, h, f, (p, *_) = await _setup(client, super_token)
    d, v = await new_driver(client, h), await new_vehicle(client, h)
    t = await new_trip(client, h, p, f, at(3))
    await client.patch(f"/vehicles/{v}", headers=h, json={"status": "maintenance"})
    assert "vehicle_not_active" in codes(await assign(client, h, t["id"], d["id"], v))
    await client.patch(f"/vehicles/{v}", headers=h, json={"status": "active"})
    await client.delete(f"/drivers/{d['id']}", headers=h)                              # offboarded
    assert "driver_inactive" in codes(await assign(client, h, t["id"], d["id"], v))