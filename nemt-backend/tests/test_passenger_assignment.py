"""Dispatcher assigns -> passenger sees "driver found", accepts or declines -> driver/dispatch see it.
Also: what the passenger may see about the driver (contact, vehicle, live location) and when."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.modules.routing.providers import Point, haversine_m
from tests.helpers import (
    assign,
    at,
    bearer,
    login,
    make_company,
    new_driver,
    new_facility,
    new_patient,
    new_trip,
    new_vehicle,
    sql,
    staff_headers,
)
from tests.test_avatars import PNG
from tests.test_passenger_portal import passenger_headers


async def owner_sql(statement: str, **params) -> None:
    """driver_positions is append-only for the app role, so test clocks are moved as the owner."""
    eng = create_async_engine(settings.database_owner_url, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.execute(text(statement), params)
    await eng.dispose()


async def _setup(client, super_token, slug: str = "alpha") -> dict:
    c = await make_company(client, super_token, slug)
    admin = c["admin_headers"]
    fac = await new_facility(client, admin)
    d1 = await new_driver(client, admin, 1, phone="+1 (555) 010-0001")
    d2 = await new_driver(client, admin, 2, phone="555-0002")
    v1 = await new_vehicle(client, admin, 1, color="Silver")
    v2 = await new_vehicle(client, admin, 2)  # no colour on file
    disp = await staff_headers(client, c, "dispatcher")
    rider = await passenger_headers(client, slug, f"rider@{slug}.example.com")
    home = (await client.get("/passenger/addresses", headers=rider)).json()[0]["id"]
    r = await client.post("/passenger/trips", headers=rider, json={
        "pickup": {"patient_address_id": home}, "dropoff": {"facility_id": fac},
        "scheduled_pickup_at": at(days=1, hour=15).isoformat()})
    assert r.status_code == 201, r.text
    d1h = bearer((await login(client, slug, d1["email"]))["access_token"])
    d2h = bearer((await login(client, slug, d2["email"]))["access_token"])
    return {"c": c, "admin": admin, "disp": disp, "rider": rider, "trip": r.json()[0], "fac": fac,
            "d1": d1, "d2": d2, "v1": v1, "v2": v2, "d1h": d1h, "d2h": d2h}


async def _card(client, w):
    r = await client.get(f"/passenger/trips/{w['trip']['id']}/assignment", headers=w["rider"])
    assert r.status_code == 200, r.text
    return r.json()


async def _start_today(w):
    """Move the pickup to 'now' so the driver app treats it as today's run."""
    await sql("UPDATE trips SET scheduled_pickup_at = now() + interval '20 minutes',"
              " ends_at = now() + interval '80 minutes' WHERE id = :id", id=w["trip"]["id"])


async def test_unassigned_trip_shows_a_pending_state_without_fabricated_details(client, super_token):
    w = await _setup(client, super_token)
    card = await _card(client, w)
    assert card["status"] == "scheduled" and card["driver"] is None and card["vehicle"] is None
    assert card["location"] is None and card["can_respond"] is False and card["passenger_response"] is None


async def test_driver_found_accept_flow(client, super_token):
    w = await _setup(client, super_token)
    tid = w["trip"]["id"]
    assert (await assign(client, w["disp"], tid, w["d1"]["id"], w["v1"])).status_code == 200

    # passenger: "driver found", details of THIS trip's driver + vehicle, answer pending
    card = await _card(client, w)
    assert card["status"] == "assigned" and card["passenger_response"] == "pending" and card["can_respond"]
    assert card["driver"]["display_name"] == "Driver 1" and card["driver"]["phone"] == "+1 (555) 010-0001"
    assert card["driver"]["id"] == w["d1"]["id"] and set(card["driver"]) == {
        "id", "display_name", "has_photo", "photo_updated_at", "phone", "wheelchair_certified"}
    assert card["vehicle"] == {"unit_number": "V-01", "make": "Ford", "model": "Transit", "year": 2022,
                               "color": "Silver", "plate": "PL0001", "plate_state": "VA",
                               "vehicle_type": "wheelchair_van"}
    assert card["location"] is None and "on the way" in card["location_note"]
    listed = (await client.get("/passenger/trips?scope=upcoming", headers=w["rider"])).json()[0]
    assert listed["passenger_response"] == "pending"

    # dispatch and driver see the same assignment + its pending state
    seen = (await client.get(f"/trips/{tid}", headers=w["disp"])).json()
    assert seen["passenger_response"] == "pending" and seen["vehicle"]["plate"] == "PL0001"
    assert seen["vehicle"]["color"] == "Silver" and seen["driver"]["id"] == w["d1"]["id"]
    await _start_today(w)
    mine = (await client.get("/driver/trips", headers=w["d1h"])).json()
    assert [t["id"] for t in mine] == [tid] and mine[0]["passenger_response"] == "pending"
    assert mine[0]["vehicle"]["plate"] == "PL0001" and mine[0]["vehicle"]["color"] == "Silver"

    # the driver can't set off before the passenger answers (enforced by the server)
    early = await client.post(f"/driver/trips/{tid}/status", headers=w["d1h"], json={"status": "en_route_to_pickup"})
    assert early.status_code == 409 and "accepted" in early.json()["detail"]

    # answering about a driver the passenger didn't see is refused
    wrong = await client.post(f"/passenger/trips/{tid}/assignment/accept", headers=w["rider"],
                              json={"driver_id": w["d2"]["id"]})
    assert wrong.status_code == 409
    ok = await client.post(f"/passenger/trips/{tid}/assignment/accept", headers=w["rider"],
                           json={"driver_id": w["d1"]["id"]})
    assert ok.status_code == 200 and ok.json()["passenger_response"] == "accepted"
    again = await client.post(f"/passenger/trips/{tid}/assignment/accept", headers=w["rider"],
                              json={"driver_id": w["d1"]["id"]})
    assert again.status_code == 409
    late_decline = await client.post(f"/passenger/trips/{tid}/assignment/decline", headers=w["rider"],
                                     json={"driver_id": w["d1"]["id"]})
    assert late_decline.status_code == 409
    assert (await client.get(f"/trips/{tid}", headers=w["disp"])).json()["passenger_response"] == "accepted"
    events = [e["event_type"] for e in (await client.get(f"/trips/{tid}/events", headers=w["disp"])).json()]
    assert "passenger_accepted" in events
    assert (await client.get("/driver/trips", headers=w["d1h"])).json()[0]["passenger_response"] == "accepted"

    go = await client.post(f"/driver/trips/{tid}/status", headers=w["d1h"], json={"status": "en_route_to_pickup"})
    assert go.status_code == 200, go.text

    # a vehicle swap with the same driver keeps the acceptance; the passenger sees the new vehicle
    assert (await assign(client, w["disp"], tid, w["d1"]["id"], w["v2"])).status_code == 200
    card = await _card(client, w)
    assert card["passenger_response"] == "accepted" and card["vehicle"]["plate"] == "PL0002"
    assert card["vehicle"]["color"] is None


async def test_decline_releases_the_driver_and_reassignment_needs_a_new_answer(client, super_token):
    w = await _setup(client, super_token)
    tid = w["trip"]["id"]
    await assign(client, w["disp"], tid, w["d1"]["id"], w["v1"])
    await _start_today(w)
    r = await client.post(f"/passenger/trips/{tid}/assignment/decline", headers=w["rider"],
                          json={"driver_id": w["d1"]["id"]})
    assert r.status_code == 200 and r.json()["status"] == "scheduled"
    assert r.json()["passenger_response"] == "declined" and r.json()["driver_name"] is None

    seen = (await client.get(f"/trips/{tid}", headers=w["disp"])).json()
    assert seen["status"] == "scheduled" and seen["driver"] is None and seen["passenger_response"] == "declined"
    assert tid in [t["id"] for t in (await client.get("/trips?unassigned=true", headers=w["disp"])).json()]
    assert (await client.get("/driver/trips", headers=w["d1h"])).json() == []   # released
    card = await _card(client, w)
    assert card["driver"] is None and card["can_respond"] is False

    # nothing to answer now; and the old driver can't be accepted after the fact
    stale = await client.post(f"/passenger/trips/{tid}/assignment/accept", headers=w["rider"],
                              json={"driver_id": w["d1"]["id"]})
    assert stale.status_code == 409

    # dispatch picks someone else -> a fresh "driver found" for the new driver only
    assert (await assign(client, w["disp"], tid, w["d2"]["id"], w["v2"])).status_code == 200
    card = await _card(client, w)
    assert card["passenger_response"] == "pending" and card["driver"]["display_name"] == "Driver 2"
    old = await client.post(f"/passenger/trips/{tid}/assignment/accept", headers=w["rider"],
                            json={"driver_id": w["d1"]["id"]})
    assert old.status_code == 409
    assert (await client.post(f"/passenger/trips/{tid}/assignment/accept", headers=w["rider"],
                              json={"driver_id": w["d2"]["id"]})).status_code == 200

    # reassigning to a DIFFERENT driver after acceptance asks the passenger again
    assert (await assign(client, w["disp"], tid, w["d1"]["id"], w["v1"])).status_code == 200
    assert (await _card(client, w))["passenger_response"] == "pending"


async def test_dispatch_unassign_and_cancel_clear_the_card(client, super_token):
    w = await _setup(client, super_token)
    tid = w["trip"]["id"]
    await assign(client, w["disp"], tid, w["d1"]["id"], w["v1"])
    assert (await client.post(f"/trips/{tid}/unassign", headers=w["disp"])).status_code == 200
    card = await _card(client, w)
    assert card["driver"] is None and card["passenger_response"] is None
    await assign(client, w["disp"], tid, w["d1"]["id"], w["v1"])
    assert (await client.post(f"/passenger/trips/{tid}/cancel", headers=w["rider"])).status_code == 200
    card = await _card(client, w)
    assert card["status"] == "cancelled" and card["driver"] is None and card["vehicle"] is None
    assert (await client.post(f"/passenger/trips/{tid}/assignment/accept", headers=w["rider"],
                              json={"driver_id": w["d1"]["id"]})).status_code == 409


async def test_patients_without_a_login_need_no_answer(client, super_token):
    w = await _setup(client, super_token)
    p = await new_patient(client, w["admin"], 7)                      # dispatcher-entered, no portal login
    trip = await new_trip(client, w["disp"], p, w["fac"], at(days=1, hour=19))
    await assign(client, w["disp"], trip["id"], w["d1"]["id"], w["v1"])
    seen = (await client.get(f"/trips/{trip['id']}", headers=w["disp"])).json()
    assert seen["passenger_response"] is None
    await sql("UPDATE trips SET scheduled_pickup_at = now() + interval '20 minutes',"
              " ends_at = now() + interval '80 minutes' WHERE id = :id", id=trip["id"])
    go = await client.post(f"/driver/trips/{trip['id']}/status", headers=w["d1h"], json={"status": "en_route_to_pickup"})
    assert go.status_code == 200, go.text


async def test_dispatch_can_override_a_pending_answer(client, super_token):
    w = await _setup(client, super_token)
    tid = w["trip"]["id"]
    await assign(client, w["disp"], tid, w["d1"]["id"], w["v1"])
    r = await client.post(f"/trips/{tid}/status", headers=w["disp"], json={"status": "en_route_to_pickup"})
    assert r.status_code == 200 and r.json()["passenger_response"] == "pending"


async def test_live_location_distance_and_staleness(client, super_token):
    w = await _setup(client, super_token)
    tid = w["trip"]["id"]
    await assign(client, w["disp"], tid, w["d1"]["id"], w["v1"])
    await _start_today(w)
    # a fix sent BEFORE the driver set off for this pickup is never shown to the passenger
    assert (await client.post("/driver/location", headers=w["d1h"],
                              json={"lat": 38.9, "lng": -77.0})).status_code == 204
    await client.post(f"/passenger/trips/{tid}/assignment/accept", headers=w["rider"], json={"driver_id": w["d1"]["id"]})
    await owner_sql("UPDATE driver_positions SET recorded_at = now() - interval '2 minutes'")
    await client.post(f"/driver/trips/{tid}/status", headers=w["d1h"], json={"status": "en_route_to_pickup"})
    card = await _card(client, w)
    assert card["location"] is None and "hasn't shared" in card["location_note"]

    lat, lng = 38.88, -77.10
    r = await client.post("/driver/location", headers=w["d1h"],
                          json={"lat": lat, "lng": lng, "trip_id": tid, "accuracy_m": 8})
    assert r.status_code == 204, r.text
    card = await _card(client, w)
    loc = card["location"]
    assert loc["latitude"] == lat and loc["longitude"] == lng and loc["stale"] is False
    expected = haversine_m(Point(lat, lng), Point(card["pickup_latitude"], card["pickup_longitude"]))
    assert abs(loc["straight_line_m"] - expected) < max(50, expected * 0.01)   # geodesic vs sphere
    assert loc["eta_seconds"] is None and loc["road_distance_m"] is None      # no road router configured

    await owner_sql("UPDATE driver_positions SET recorded_at = recorded_at - interval '10 minutes'")
    await sql("UPDATE trips SET en_route_at = now() - interval '15 minutes' WHERE id = :t", t=tid)
    stale = (await _card(client, w))["location"]
    assert stale["stale"] is True and stale["age_seconds"] >= 590

    # once the passenger is on board / the trip is over, the location is no longer shared
    await client.post(f"/driver/trips/{tid}/status", headers=w["d1h"], json={"status": "arrived_at_pickup"})
    assert (await _card(client, w))["location"] is not None
    await client.post(f"/driver/trips/{tid}/status", headers=w["d1h"], json={"status": "passenger_on_board"})
    card = await _card(client, w)
    assert card["location"] is None and card["driver"]["phone"] is not None
    for s in ("en_route_to_dropoff", "arrived_at_dropoff", "completed"):
        assert (await client.post(f"/driver/trips/{tid}/status", headers=w["d1h"], json={"status": s})).status_code == 200
    card = await _card(client, w)
    assert card["driver"] is None and card["location"] is None and card["vehicle"] is None
    assert (await client.get(f"/passenger/trips/{tid}/driver-photo", headers=w["rider"])).status_code == 404


async def test_location_reports_are_validated(client, super_token):
    w = await _setup(client, super_token)
    tid = w["trip"]["id"]
    await assign(client, w["disp"], tid, w["d1"]["id"], w["v1"])
    for bad in ({"lat": 91, "lng": 0}, {"lat": 0, "lng": -181}, {"lat": "x", "lng": 0}, {"lng": 0},
                {"lat": 1, "lng": 1, "speed_mps": -3}):
        assert (await client.post("/driver/location", headers=w["d1h"], json=bad)).status_code == 422, bad
    old = await client.post("/driver/location", headers=w["d1h"],
                            json={"lat": 1, "lng": 1, "recorded_at": "2020-01-01T00:00:00Z"})
    assert old.status_code == 422
    # another driver can't tag positions onto this trip
    other = await client.post("/driver/location", headers=w["d2h"], json={"lat": 1, "lng": 1, "trip_id": tid})
    assert other.status_code == 404
    # only drivers report locations
    for h in (w["rider"], w["disp"]):
        assert (await client.post("/driver/location", headers=h, json={"lat": 1, "lng": 1})).status_code == 403


async def test_another_passenger_cannot_see_or_answer_the_assignment(client, super_token):
    w = await _setup(client, super_token)
    tid = w["trip"]["id"]
    await assign(client, w["disp"], tid, w["d1"]["id"], w["v1"])
    d1_user = (await client.get("/users/me", headers=w["d1h"]))
    await client.put("/users/me/avatar", headers=w["d1h"], files={"file": ("d.png", PNG, "image/png")})
    assert d1_user.status_code == 200

    other = await passenger_headers(client, "alpha", "nosy@alpha.example.com", full_name="Nosy Parker")
    for method, path, body in (
        ("GET", f"/passenger/trips/{tid}/assignment", None),
        ("GET", f"/passenger/trips/{tid}/driver-photo", None),
        ("POST", f"/passenger/trips/{tid}/assignment/accept", {"driver_id": w["d1"]["id"]}),
        ("POST", f"/passenger/trips/{tid}/assignment/decline", {"driver_id": w["d1"]["id"]}),
    ):
        r = await client.request(method, path, headers=other, json=body)
        assert r.status_code == 404, (path, r.status_code)
    # a passenger in another company gets nothing either
    c2 = await make_company(client, super_token, "beta")
    beta_rider = await passenger_headers(client, "beta", "rider@beta.example.com")
    assert c2["slug"] == "beta"
    assert (await client.get(f"/passenger/trips/{tid}/assignment", headers=beta_rider)).status_code == 404
    # the owner can see the photo of their assigned driver
    photo = await client.get(f"/passenger/trips/{tid}/driver-photo", headers=w["rider"])
    assert photo.status_code == 200 and photo.content == PNG
    card = await _card(client, w)
    assert card["driver"]["has_photo"] is True
    # untouched by the nosy attempts
    assert card["passenger_response"] == "pending"
    # drivers and staff can't use the passenger answer endpoints
    for h in (w["d1h"], w["disp"]):
        r = await client.post(f"/passenger/trips/{tid}/assignment/accept", headers=h, json={"driver_id": w["d1"]["id"]})
        assert r.status_code == 403


async def test_decline_is_refused_once_the_driver_has_arrived(client, super_token):
    w = await _setup(client, super_token)
    tid = w["trip"]["id"]
    await assign(client, w["disp"], tid, w["d1"]["id"], w["v1"])
    for st in ("en_route_to_pickup", "arrived_at_pickup"):   # dispatch moved it on (override)
        assert (await client.post(f"/trips/{tid}/status", headers=w["disp"], json={"status": st})).status_code == 200
    r = await client.post(f"/passenger/trips/{tid}/assignment/decline", headers=w["rider"],
                          json={"driver_id": w["d1"]["id"]})
    assert r.status_code == 409
    seen = (await client.get(f"/trips/{tid}", headers=w["disp"])).json()
    assert seen["status"] == "arrived_at_pickup" and seen["driver"]["id"] == w["d1"]["id"]
    # accepting is still possible
    ok = await client.post(f"/passenger/trips/{tid}/assignment/accept", headers=w["rider"],
                           json={"driver_id": w["d1"]["id"]})
    assert ok.status_code == 200 and ok.json()["passenger_response"] == "accepted"
