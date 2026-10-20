"""The driver dashboard API: profile + availability, today/upcoming trips in the COMPANY's day,
statistics, handing a trip back, location rules; and a passenger's repeated ride requests reaching
the assigned driver (REST and live)."""

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from tests.helpers import (
    assign,
    at_local,
    bearer,
    local_date,
    login,
    make_company,
    new_driver,
    new_facility,
    new_vehicle,
    sql,
    staff_headers,
)
from tests.test_passenger_assignment import owner_sql
from tests.test_passenger_portal import passenger_headers
from tests.test_realtime import _auth, _sync


async def _setup(client, super_token) -> dict:
    c = await make_company(client, super_token, "alpha")
    admin = c["admin_headers"]
    fac = await new_facility(client, admin)
    d1 = await new_driver(client, admin, 1)
    d2 = await new_driver(client, admin, 2)
    v1 = await new_vehicle(client, admin, 1, color="Silver")
    v2 = await new_vehicle(client, admin, 2)
    disp = await staff_headers(client, c, "dispatcher")
    rider = await passenger_headers(client, "alpha", "rider@alpha.example.com")
    home = (await client.get("/passenger/addresses", headers=rider)).json()[0]["id"]
    d1h = bearer((await login(client, "alpha", d1["email"]))["access_token"])
    d2h = bearer((await login(client, "alpha", d2["email"]))["access_token"])
    return {"c": c, "fac": fac, "d1": d1, "d2": d2, "v1": v1, "v2": v2, "disp": disp, "rider": rider,
            "home": home, "d1h": d1h, "d2h": d2h}


async def _request_ride(client, w, when: datetime) -> dict:
    r = await client.post("/passenger/trips", headers=w["rider"], json={
        "pickup": {"patient_address_id": w["home"]}, "dropoff": {"facility_id": w["fac"]},
        "scheduled_pickup_at": when.isoformat()})
    assert r.status_code == 201, r.text
    return r.json()[0]


async def _drive_to_completion(client, w, trip_id: str) -> None:
    h = w["d1h"]
    accept = await client.post(f"/passenger/trips/{trip_id}/assignment/accept", headers=w["rider"],
                               json={"driver_id": w["d1"]["id"]})
    assert accept.status_code == 200, accept.text
    for status in ("en_route_to_pickup", "arrived_at_pickup", "passenger_on_board", "en_route_to_dropoff",
                   "arrived_at_dropoff", "completed"):
        r = await client.post(f"/driver/trips/{trip_id}/status", headers=h, json={"status": status})
        assert r.status_code == 200, (status, r.text)


async def test_profile_and_availability(client, super_token):
    w = await _setup(client, super_token)
    me = (await client.get("/driver/me", headers=w["d1h"])).json()
    assert me["full_name"] == "Driver 1" and me["company_name"] == "Alpha" and me["duty_status"] == "off_duty"
    assert me["company_timezone"] == "America/New_York" and me["on_trip"] is False
    r = await client.put("/driver/me/duty", headers=w["d1h"], json={"duty_status": "available"})
    assert r.status_code == 200 and r.json()["duty_status"] == "available"
    listed = {d["id"]: d["duty_status"] for d in (await client.get("/drivers", headers=w["disp"])).json()}
    assert listed[w["d1"]["id"]] == "available" and listed[w["d2"]["id"]] == "off_duty"
    assert (await client.put("/driver/me/duty", headers=w["d1h"], json={"duty_status": "asleep"})).status_code == 422
    # non-drivers have no driver dashboard
    assert (await client.get("/driver/me", headers=w["disp"])).status_code == 403
    assert (await client.get("/driver/me", headers=w["rider"])).status_code == 403


async def test_cannot_go_off_duty_mid_trip(client, super_token):
    w = await _setup(client, super_token)
    t = await _request_ride(client, w, datetime.now(UTC) + timedelta(hours=1))
    assert (await assign(client, w["disp"], t["id"], w["d1"]["id"], w["v1"])).status_code == 200
    await client.post(f"/passenger/trips/{t['id']}/assignment/accept", headers=w["rider"], json={"driver_id": w["d1"]["id"]})
    assert (await client.post(f"/driver/trips/{t['id']}/status", headers=w["d1h"], json={"status": "en_route_to_pickup"})).status_code == 200
    r = await client.put("/driver/me/duty", headers=w["d1h"], json={"duty_status": "off_duty"})
    assert r.status_code == 409 and "under way" in r.json()["detail"]
    assert (await client.get("/driver/me", headers=w["d1h"])).json()["on_trip"] is True


async def test_repeat_ride_requests_reach_the_driver(client, super_token):
    """The reported bug: the same passenger's NEXT ride must show up for the driver, including a
    ride on another day, and a completed ride must not block or hide anything."""
    w = await _setup(client, super_token)
    first = await _request_ride(client, w, datetime.now(UTC) + timedelta(hours=1))
    assert (await assign(client, w["disp"], first["id"], w["d1"]["id"], w["v1"])).status_code == 200
    await _drive_to_completion(client, w, first["id"])

    second = await _request_ride(client, w, at_local(1, 10, 0))   # tomorrow, company time
    third = await _request_ride(client, w, at_local(3, 10, 0))
    assert len({first["id"], second["id"], third["id"]}) == 3 and second["trip_ref"] != first["trip_ref"]
    for t in (second, third):
        assert (await assign(client, w["disp"], t["id"], w["d1"]["id"], w["v1"])).status_code == 200

    upcoming = [t["id"] for t in (await client.get("/driver/trips", headers=w["d1h"], params={"scope": "upcoming"})).json()]
    assert upcoming == [second["id"], third["id"]]                  # pickup order; the finished ride is not "upcoming"
    first_day = local_date(datetime.fromisoformat(first["scheduled_pickup_at"])).isoformat()
    that_day = {t["id"]: t["status"] for t in (await client.get("/driver/trips", headers=w["d1h"], params={"on_date": first_day})).json()}
    assert that_day[first["id"]] == "completed"                    # history keeps the finished ride
    one = (await client.get("/driver/trips", headers=w["d1h"], params={"scope": "upcoming"})).json()[0]
    assert one["requested_at"] and one["assigned_at"] and one["vehicle"]["color"] == "Silver"
    # the other driver sees none of it
    assert (await client.get("/driver/trips", headers=w["d2h"], params={"scope": "upcoming"})).json() == []
    assert (await client.get(f"/driver/trips/{second['id']}", headers=w["d2h"])).status_code == 404

    stats = (await client.get("/driver/stats", headers=w["d1h"])).json()
    me = (await client.get("/driver/me", headers=w["d1h"])).json()
    completed_today = 1 if first_day == me["today"] else 0      # "in an hour" may already be tomorrow in NY
    assert stats["today_completed"] == completed_today and stats["upcoming"] == 2  # tomorrow + in 3 days


async def test_today_is_the_company_day(client, super_token):
    w = await _setup(client, super_token)
    me = (await client.get("/driver/me", headers=w["d1h"])).json()
    t = await _request_ride(client, w, at_local(1, 0, 30))  # 00:30 tomorrow in New York
    await assign(client, w["disp"], t["id"], w["d1"]["id"], w["v1"])
    assert me["today"] == local_date(datetime.now(UTC)).isoformat()
    tomorrow = (datetime.fromisoformat(me["today"]) + timedelta(days=1)).date().isoformat()
    on_day = (await client.get("/driver/trips", headers=w["d1h"], params={"on_date": tomorrow})).json()
    assert [x["id"] for x in on_day] == [t["id"]]
    assert t["id"] not in [x["id"] for x in (await client.get("/driver/trips", headers=w["d1h"])).json()]


async def test_driver_hands_a_trip_back(client, super_token):
    w = await _setup(client, super_token)
    t = await _request_ride(client, w, at_local(2, 10, 0))
    await assign(client, w["disp"], t["id"], w["d1"]["id"], w["v1"])
    assert (await client.post(f"/driver/trips/{t['id']}/decline", headers=w["d2h"], json={"reason": "not mine"})).status_code == 404
    assert (await client.post(f"/driver/trips/{t['id']}/decline", headers=w["d1h"], json={"reason": ""})).status_code == 422
    r = await client.post(f"/driver/trips/{t['id']}/decline", headers=w["d1h"], json={"reason": "Vehicle in the shop"})
    assert r.status_code == 200
    back = (await client.get(f"/trips/{t['id']}", headers=w["disp"])).json()
    assert back["status"] == "scheduled" and back["driver"] is None and back["passenger_response"] is None
    events = [e["event_type"] for e in (await client.get(f"/trips/{t['id']}/events", headers=w["disp"])).json()]
    assert "driver_declined" in events
    # once started, it can't be handed back from the app
    await assign(client, w["disp"], t["id"], w["d1"]["id"], w["v1"])
    await client.post(f"/passenger/trips/{t['id']}/assignment/accept", headers=w["rider"], json={"driver_id": w["d1"]["id"]})
    await client.post(f"/driver/trips/{t['id']}/status", headers=w["d1h"], json={"status": "en_route_to_pickup"})
    assert (await client.post(f"/driver/trips/{t['id']}/decline", headers=w["d1h"], json={"reason": "late"})).status_code == 409


async def test_location_rules(client, super_token):
    w = await _setup(client, super_token)
    fix = {"lat": 38.88, "lng": -77.1}
    r = await client.post("/driver/location", headers=w["d1h"], json=fix)
    assert r.status_code == 409 and "off duty" in r.json()["detail"]          # off duty, no trip: refused
    await client.put("/driver/me/duty", headers=w["d1h"], json={"duty_status": "available"})
    assert (await client.post("/driver/location", headers=w["d1h"], json=fix)).status_code == 204
    assert (await client.post("/driver/location", headers=w["d1h"], json={"lat": 38.9, "lng": -77.0})).status_code == 204
    n = (await sql("SELECT count(*) FROM driver_positions")).scalar()
    assert n == 1                                                               # 2nd fix within 5 s: not stored
    for bad in ({"lat": 0, "lng": 0}, {"lat": 91, "lng": 0}, {"lat": 10, "lng": 181}, {"lat": "x", "lng": 1}):
        assert (await client.post("/driver/location", headers=w["d1h"], json=bad)).status_code == 422, bad


async def test_dispatch_live_view_marks_stale_and_missing(client, super_token):
    w = await _setup(client, super_token)
    for h in (w["d1h"], w["d2h"]):
        await client.put("/driver/me/duty", headers=h, json={"duty_status": "available"})
    await client.post("/driver/location", headers=w["d1h"], json={"lat": 38.88, "lng": -77.1})
    live = (await client.get("/dispatch/live", headers=w["disp"])).json()
    by = {d["driver_id"]: d for d in live["drivers"]}
    assert by[w["d1"]["id"]]["stale"] is False and by[w["d1"]["id"]]["lat"] == 38.88
    assert by[w["d2"]["id"]]["lat"] is None and by[w["d2"]["id"]]["at"] is None   # on duty, sharing nothing
    await owner_sql("UPDATE driver_positions SET recorded_at = now() - interval '10 minutes'")
    live = (await client.get("/dispatch/live", headers=w["disp"])).json()
    assert {d["driver_id"]: d for d in live["drivers"]}[w["d1"]["id"]]["stale"] is True
    # billing can read trips, but cannot track drivers
    billing = await staff_headers(client, w["c"], "billing_staff")
    assert (await client.get("/dispatch/live", headers=billing)).status_code == 403
    assert (await client.get("/dispatch/live", headers=w["d1h"])).status_code == 403


async def test_assignment_reaches_the_driver_live_after_commit(client, super_token):
    """The driver's socket gets 'trip.assigned' for a new assignment, and 'trip.changed' when it is
    taken away; on each signal the REST API already shows the new state (published after commit)."""
    w = await _setup(client, super_token)
    t = await _request_ride(client, w, at_local(1, 11, 0))
    d1_token = w["d1h"]["Authorization"].split()[1]
    disp = w["disp"]

    def _inner():
        with TestClient(fastapi_app) as tc:
            with tc.websocket_connect("/ws") as ws:
                _auth(ws, d1_token)
                assert tc.post(f"/trips/{t['id']}/assign", headers=disp,
                               json={"driver_id": w["d1"]["id"], "vehicle_id": w["v1"]}).status_code == 200
                evt = ws.receive_json()
                assert evt["type"] == "trip.assigned" and evt["trip_id"] == t["id"]
                mine = tc.get("/driver/trips", headers=w["d1h"], params={"scope": "upcoming"}).json()
                assert [x["id"] for x in mine] == [t["id"]]
                assert tc.post(f"/trips/{t['id']}/unassign", headers=disp).status_code == 200
                evt = ws.receive_json()
                assert evt["type"] == "trip.changed" and evt["change"] == "unassigned"
                assert tc.get("/driver/trips", headers=w["d1h"], params={"scope": "upcoming"}).json() == []
    await _sync(_inner)


async def test_concurrent_assignments_do_not_double_book(client, super_token):
    w = await _setup(client, super_token)
    when = at_local(2, 10, 0)
    a = await _request_ride(client, w, when)
    rider2 = await passenger_headers(client, "alpha", "rider2@alpha.example.com")
    home2 = (await client.get("/passenger/addresses", headers=rider2)).json()[0]["id"]
    b = (await client.post("/passenger/trips", headers=rider2, json={
        "pickup": {"patient_address_id": home2}, "dropoff": {"facility_id": w["fac"]},
        "scheduled_pickup_at": when.isoformat()})).json()[0]
    # Same driver, same time, two DIFFERENT vans: only one request may win, even when both arrive
    # together (advisory locks + the conflict check run inside one transaction). Same driver AND
    # van would be a legitimate shared ride, so that is not what this checks.
    results = await asyncio.gather(
        assign(client, w["disp"], a["id"], w["d1"]["id"], w["v1"]),
        assign(client, w["disp"], b["id"], w["d1"]["id"], w["v2"]),
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert "driver_busy_other_vehicle" in loser.text
