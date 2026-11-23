"""Passenger self-registration -> visible to dispatch/admin; the passenger portal is own-data only."""

from tests.helpers import (
    PW,
    assign,
    at,
    bearer,
    login,
    make_company,
    new_driver,
    new_facility,
    new_vehicle,
    staff_headers,
)

HOME = {"line1": "1 Home St", "city": "Arlington", "state": "VA", "postal_code": "22201"}


async def register(client, slug: str, email: str, **kw) -> dict:
    body = {"email": email, "password": PW, "full_name": "Sam Rider", "phone": "555-0101",
            "date_of_birth": "1960-02-03", "home_address": HOME} | kw
    r = await client.post("/auth/register", headers={"X-Tenant": slug}, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def passenger_headers(client, slug: str, email: str, **kw) -> dict[str, str]:
    await register(client, slug, email, **kw)
    return bearer((await login(client, slug, email))["access_token"])


async def test_registration_is_visible_to_dispatch_and_admin(client, super_token):
    c = await make_company(client, super_token, "alpha")
    disp = await staff_headers(client, c, "dispatcher")
    user = await register(client, "alpha", "sam@example.com")
    assert user["role"] == "passenger"

    for h in (disp, c["admin_headers"]):
        patients = (await client.get("/patients", headers=h)).json()
        mine = [p for p in patients if p["user_id"] == user["id"]]
        assert len(mine) == 1 and mine[0]["first_name"] == "Sam" and mine[0]["last_name"] == "Rider"
        addrs = (await client.get(f"/patients/{mine[0]['id']}/addresses", headers=h)).json()
        assert addrs[0]["label"] == "home" and addrs[0]["is_primary"]

    summary = (await client.get("/stats/summary", headers=disp)).json()
    assert summary["patients"] == 1 and summary["self_registered_patients"] == 1
    assert summary["users_by_role"]["passenger"] == 1
    admin_list = (await client.get("/users?role=passenger", headers=c["admin_headers"])).json()
    assert [u["email"] for u in admin_list] == ["sam@example.com"]


async def test_registration_is_atomic_and_rejects_bad_input(client, super_token):
    await make_company(client, super_token, "alpha")
    await register(client, "alpha", "sam@example.com")
    dup = await client.post("/auth/register", headers={"X-Tenant": "alpha"}, json={
        "email": "SAM@example.com", "password": PW, "full_name": "Again", "date_of_birth": "1960-02-03"})
    assert dup.status_code == 409
    for bad in ({"date_of_birth": "2999-01-01"}, {"date_of_birth": None}, {"full_name": "   "}):
        body = {"email": "new@example.com", "password": PW, "full_name": "N", "date_of_birth": "1960-02-03"} | bad
        r = await client.post("/auth/register", headers={"X-Tenant": "alpha"}, json=body)
        assert r.status_code == 422, bad
    # nothing half-created by the failures: the email is still free
    await register(client, "alpha", "new@example.com")


async def test_passenger_profile_and_addresses(client, super_token):
    await make_company(client, super_token, "alpha")
    h = await passenger_headers(client, "alpha", "sam@example.com")
    prof = (await client.get("/passenger/profile", headers=h)).json()
    assert prof["date_of_birth"] == "1960-02-03" and prof["mobility"] == "ambulatory"
    r = await client.patch("/passenger/profile", headers=h, json={"full_name": "Samuel Q Rider", "mobility": "walker"})
    assert r.status_code == 200 and r.json()["first_name"] == "Samuel Q" and r.json()["last_name"] == "Rider"
    assert (await client.patch("/passenger/profile", headers=h, json={"mobility": None})).status_code == 422
    me = (await client.get("/users/me", headers=h)).json()
    assert me["full_name"] == "Samuel Q Rider"

    added = await client.post("/passenger/addresses", headers=h, json={
        "label": "work", "line1": "9 Office Rd", "city": "Arlington", "state": "va", "postal_code": "22202"})
    assert added.status_code == 201
    addrs = (await client.get("/passenger/addresses", headers=h)).json()
    assert {a["label"] for a in addrs} == {"home", "work"}
    assert (await client.delete(f"/passenger/addresses/{added.json()['id']}", headers=h)).status_code == 204


async def test_ride_request_flows_to_dispatch_and_back(client, super_token):
    c = await make_company(client, super_token, "alpha")
    admin = c["admin_headers"]
    fac = await new_facility(client, admin)
    driver, vehicle = await new_driver(client, admin), await new_vehicle(client, admin)
    disp = await staff_headers(client, c, "dispatcher")
    h = await passenger_headers(client, "alpha", "sam@example.com")

    dests = (await client.get("/passenger/destinations", headers=h)).json()
    assert [d["id"] for d in dests] == [fac] and "billing_notes" not in dests[0]
    home = (await client.get("/passenger/addresses", headers=h)).json()[0]["id"]
    when = at(days=2, hour=15)
    r = await client.post("/passenger/trips", headers=h, json={
        "pickup": {"patient_address_id": home}, "dropoff": {"facility_id": fac},
        "scheduled_pickup_at": when.isoformat(), "special_instructions": "Ring twice"})
    assert r.status_code == 201, r.text
    trip = r.json()[0]
    assert trip["status"] == "scheduled" and trip["source"] == "passenger_portal"

    # dispatch sees the request in the unassigned queue and assigns it
    queue = (await client.get("/trips?unassigned=true", headers=disp)).json()
    assert [t["id"] for t in queue] == [trip["id"]] and queue[0]["source"] == "passenger_portal"
    assert (await client.get("/stats/summary", headers=disp)).json()["passenger_requests_pending"] == 1
    assert (await assign(client, disp, trip["id"], driver["id"], vehicle)).status_code == 200

    mine = (await client.get("/passenger/trips?scope=upcoming", headers=h)).json()
    assert mine[0]["status"] == "assigned" and mine[0]["driver_name"] == "Driver 1"
    cancelled = await client.post(f"/passenger/trips/{trip['id']}/cancel", headers=h)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert (await client.post(f"/passenger/trips/{trip['id']}/cancel", headers=h)).status_code == 409
    past = (await client.get("/passenger/trips?scope=past", headers=h)).json()
    assert [t["id"] for t in past] == [trip["id"]]
    seen = (await client.get(f"/trips/{trip['id']}", headers=disp)).json()
    assert seen["status"] == "cancelled" and seen["cancel_reason_code"] == "patient_cancelled"


async def test_passenger_cannot_reach_other_passengers_or_staff_apis(client, super_token):
    c = await make_company(client, super_token, "alpha")
    fac = await new_facility(client, c["admin_headers"])
    a = await passenger_headers(client, "alpha", "a@example.com")
    b = await passenger_headers(client, "alpha", "b@example.com", full_name="Bea B")
    a_home = (await client.get("/passenger/addresses", headers=a)).json()[0]["id"]
    trip = (await client.post("/passenger/trips", headers=a, json={
        "pickup": {"patient_address_id": a_home}, "dropoff": {"facility_id": fac},
        "scheduled_pickup_at": at(days=2).isoformat()})).json()[0]

    assert (await client.get(f"/passenger/trips/{trip['id']}", headers=b)).status_code == 404
    assert (await client.post(f"/passenger/trips/{trip['id']}/cancel", headers=b)).status_code == 404
    assert (await client.get("/passenger/trips", headers=b)).json() == []
    stolen = await client.post("/passenger/trips", headers=b, json={    # someone else's saved address
        "pickup": {"patient_address_id": a_home}, "dropoff": {"facility_id": fac},
        "scheduled_pickup_at": at(days=3).isoformat()})
    assert stolen.status_code == 422
    assert (await client.delete(f"/passenger/addresses/{a_home}", headers=b)).status_code == 404
    forged = await client.post("/passenger/trips", headers=b, json={    # patient_id is not theirs to pick
        "patient_id": "00000000-0000-0000-0000-000000000000", "pickup": {"address": HOME},
        "dropoff": {"facility_id": fac}, "scheduled_pickup_at": at(days=4).isoformat()})
    assert forged.status_code == 201
    assert (await client.get("/passenger/trips", headers=a)).json()[0]["id"] == trip["id"]  # still only A's own

    for method, path in (("GET", "/patients"), ("GET", "/trips"), ("GET", "/users"), ("GET", "/drivers"),
                         ("GET", "/platform/companies"), ("GET", "/stats/summary"), ("GET", "/audit-logs"),
                         ("POST", "/platform/system/reset"), ("GET", "/driver/trips")):
        r = await client.request(method, path, headers=a, json={} if method == "POST" else None)
        assert r.status_code == 403, (method, path, r.status_code)

    staff = await staff_headers(client, c, "dispatcher")      # and staff can't use the passenger portal
    assert (await client.get("/passenger/trips", headers=staff)).status_code == 403
