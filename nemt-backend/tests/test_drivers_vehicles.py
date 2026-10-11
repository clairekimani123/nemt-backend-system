import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db import open_tenant_session
from tests.helpers import (
    PW,
    bearer,
    driver_payload,
    login,
    make_company,
    staff_headers,
    vehicle_payload,
)


async def _co(client, super_token, slug="alpha"):
    return await make_company(client, super_token, slug)


# ------------------------------ drivers ------------------------------
async def test_driver_creation_makes_a_working_login(client, super_token):
    c = await _co(client, super_token)
    fleet = await staff_headers(client, c, "fleet_manager")
    r = await client.post("/drivers", headers=fleet, json=driver_payload())
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["license_number"] == "D1234567" and d["license_state"] == "VA" and d["license_last4"] == "4567"
    tokens = await login(client, "alpha", "dan@fleet.example.com")      # the new driver can sign in
    me = (await client.get("/users/me", headers=bearer(tokens["access_token"]))).json()
    assert me["role"] == "driver" and me["id"] == d["user_id"]

    disp = await staff_headers(client, c, "dispatcher")                 # may read drivers, not licence numbers
    seen = (await client.get(f"/drivers/{d['id']}", headers=disp)).json()
    assert seen["license_number"] is None and seen["license_last4"] == "4567"
    assert (await client.post("/drivers", headers=disp, json=driver_payload("x@fleet.example.com"))).status_code == 403

    async with open_tenant_session(None, bypass=True) as s:
        enc = await s.scalar(text("SELECT license_number_enc FROM drivers"))
    assert "D1234567" not in enc


async def test_driver_creation_is_atomic(client, super_token):
    c = await _co(client, super_token)
    h = c["admin_headers"]
    assert (await client.post("/drivers", headers=h, json=driver_payload())).status_code == 201
    assert (await client.post("/drivers", headers=h, json=driver_payload())).status_code == 409       # same email
    clash = await client.post("/drivers", headers=h, json=driver_payload("second@fleet.example.com"))
    assert clash.status_code == 409                                                                   # same employee no.
    # the half-created login from the failed attempt must have been rolled back
    ok = await client.post("/drivers", headers=h, json=driver_payload("second@fleet.example.com", employee_number="E-200"))
    assert ok.status_code == 201


async def test_driver_update_filters_and_archive(client, super_token):
    c = await _co(client, super_token)
    h = c["admin_headers"]
    d = (await client.post("/drivers", headers=h, json=driver_payload())).json()
    await client.post("/drivers", headers=h, json=driver_payload(
        "ann@fleet.example.com", full_name="Ann Walker", employee_number="E-101", wheelchair_certified=False))
    upd = (await client.patch(f"/drivers/{d['id']}", headers=h, json={
        "full_name": "Daniel Driver", "phone": "555-0300", "license_number": "Z9999999"})).json()
    assert upd["full_name"] == "Daniel Driver" and upd["license_last4"] == "9999"
    assert (await client.patch(f"/drivers/{d['id']}", headers=h, json={"wheelchair_certified": None})).status_code == 422
    certified = (await client.get("/drivers", headers=h, params={"wheelchair_certified": True})).json()
    assert [x["id"] for x in certified] == [d["id"]]
    assert [x["full_name"] for x in (await client.get("/drivers", headers=h, params={"q": "ann"})).json()] == ["Ann Walker"]

    tokens = await login(client, "alpha", "dan@fleet.example.com")
    assert (await client.delete(f"/drivers/{d['id']}", headers=h)).status_code == 204
    r = await client.post("/auth/login", headers={"X-Tenant": "alpha"}, json={"email": "dan@fleet.example.com", "password": PW})
    assert r.status_code == 401                                                     # login disabled
    assert (await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})).status_code == 401
    assert (await client.get("/users/me", headers=bearer(tokens["access_token"]))).status_code == 401
    assert len((await client.get("/drivers", headers=h)).json()) == 1               # archived one is hidden


# ------------------------------ vehicles ------------------------------
async def test_vehicle_validation_and_duplicates(client, super_token):
    c = await _co(client, super_token)
    h = c["admin_headers"]
    assert (await client.post("/vehicles", headers=h, json=vehicle_payload())).status_code == 201
    assert (await client.post("/vehicles", headers=h, json=vehicle_payload(vin="1HGCM82633A0043O2"))).status_code == 422  # 'O' not allowed
    no_ramp = vehicle_payload(unit_number="V-02", vin="2HGCM82633A004352", equipment="none")
    assert (await client.post("/vehicles", headers=h, json=no_ramp)).status_code == 422
    assert (await client.post("/vehicles", headers=h, json=vehicle_payload(vin="2HGCM82633A004352"))).status_code == 409   # unit no. taken
    assert (await client.post("/vehicles", headers=h, json=vehicle_payload(unit_number="V-03"))).status_code == 409        # VIN taken
    lower = vehicle_payload(unit_number="V-04", vin="3hgcm82633a004352", plate="xyz999", plate_state="md")
    made = (await client.post("/vehicles", headers=h, json=lower)).json()
    assert made["vin"] == "3HGCM82633A004352" and made["plate"] == "XYZ999" and made["plate_state"] == "MD"


async def test_vehicle_filters_update_and_retire(client, super_token):
    c = await _co(client, super_token)
    h = c["admin_headers"]
    van = (await client.post("/vehicles", headers=h, json=vehicle_payload())).json()
    sedan = (await client.post("/vehicles", headers=h, json=vehicle_payload(
        unit_number="S-01", vin="4HGCM82633A004352", vehicle_type="sedan", seat_capacity=3,
        wheelchair_capacity=0, equipment="none"))).json()
    ids = lambda r: [v["id"] for v in r.json()]  # noqa: E731
    assert ids(await client.get("/vehicles", headers=h, params={"wheelchair_capable": True})) == [van["id"]]
    assert ids(await client.get("/vehicles", headers=h, params={"wheelchair_capable": False})) == [sedan["id"]]
    assert ids(await client.get("/vehicles", headers=h, params={"vehicle_type": "sedan"})) == [sedan["id"]]

    assert (await client.patch(f"/vehicles/{van['id']}", headers=h, json={"odometer_miles": 12345, "status": "maintenance"})).json()["odometer_miles"] == 12345
    assert ids(await client.get("/vehicles", headers=h, params={"status": "maintenance"})) == [van["id"]]
    # removing the lift from a wheelchair van must be refused
    assert (await client.patch(f"/vehicles/{van['id']}", headers=h, json={"equipment": "none"})).status_code == 422
    assert (await client.patch(f"/vehicles/{van['id']}", headers=h, json={"seat_capacity": None})).status_code == 422

    assert (await client.delete(f"/vehicles/{sedan['id']}", headers=h)).status_code == 204
    assert ids(await client.get("/vehicles", headers=h)) == [van["id"]]
    retired = (await client.get(f"/vehicles/{sedan['id']}", headers=h)).json()
    assert retired["status"] == "retired" and retired["archived_at"]


async def test_vehicle_permissions_and_default_driver(client, super_token):
    c = await _co(client, super_token)
    h = c["admin_headers"]
    disp = await staff_headers(client, c, "dispatcher")
    fleet = await staff_headers(client, c, "fleet_manager")
    assert (await client.post("/vehicles", headers=disp, json=vehicle_payload())).status_code == 403
    made = await client.post("/vehicles", headers=fleet, json=vehicle_payload())
    assert made.status_code == 201
    assert (await client.get("/vehicles", headers=disp)).status_code == 200      # dispatchers may read
    drv = (await client.post("/drivers", headers=h, json=driver_payload())).json()
    ok = await client.patch(f"/vehicles/{made.json()['id']}", headers=fleet, json={"default_driver_id": drv["id"]})
    assert ok.status_code == 200 and ok.json()["default_driver_id"] == drv["id"]
    ghost = await client.patch(f"/vehicles/{made.json()['id']}", headers=fleet, json={"default_driver_id": str(uuid.uuid4())})
    assert ghost.status_code == 409


async def test_database_enforces_wheelchair_equipment_rule(client, super_token):
    c = await _co(client, super_token)
    async with open_tenant_session(uuid.UUID(c["company"]["id"])) as s:
        with pytest.raises(IntegrityError, match="wheelchair_needs_equipment"):
            await s.execute(text(
                "INSERT INTO vehicles (id, company_id, unit_number, vin, plate, plate_state, make, model, year,"
                " vehicle_type, seat_capacity, wheelchair_capacity, equipment, status)"
                " VALUES (gen_random_uuid(), :c, 'X', '5HGCM82633A004352', 'P', 'VA', 'F', 'T', 2020,"
                " 'sedan', 4, 2, 'none', 'active')"), {"c": uuid.UUID(c["company"]["id"])})